using ShortTermRental.Api;
using Microsoft.AspNetCore.Diagnostics;
using Microsoft.AspNetCore.RateLimiting;
using Npgsql;
using StackExchange.Redis;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddSingleton(_ => NpgsqlDataSource.Create(
    builder.Configuration.GetConnectionString("Postgres")
    ?? throw new InvalidOperationException("ConnectionStrings:Postgres ausente.")));
builder.Services.AddSingleton<IConnectionMultiplexer>(_ => ConnectionMultiplexer.Connect(
    builder.Configuration.GetConnectionString("Redis")
    ?? throw new InvalidOperationException("ConnectionStrings:Redis ausente.")));
builder.Services.AddSingleton<BookingStore>();
builder.Services.AddSingleton<PropertyCache>();
builder.Services.AddHostedService<OutboxPublisher>();
builder.Services.AddHostedService<StatisticsConsumer>();
builder.Services.AddProblemDetails();
builder.Services.AddRateLimiter(options =>
{
    options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    options.AddConcurrencyLimiter("reads", limit =>
    {
        limit.PermitLimit = 256;
        limit.QueueLimit = 0;
    });
    options.AddConcurrencyLimiter("writes", limit =>
    {
        limit.PermitLimit = 128;
        limit.QueueLimit = 0;
    });
});

var app = builder.Build();
app.UseExceptionHandler(error => error.Run(async context =>
{
    var ex = context.Features.Get<IExceptionHandlerFeature>()!.Error;
    var (status, title) = ex switch
    {
        BookingConflictException => (409, ex.Message),
        ResourceNotFoundException => (404, ex.Message),
        PostgresException { SqlState: PostgresErrorCodes.ExclusionViolation }
            => (409, "O imovel ja possui uma reserva que sobrepoe essas datas."),
        PostgresException { SqlState: PostgresErrorCodes.ForeignKeyViolation }
            => (404, "Imovel nao encontrado."),
        NpgsqlException => (503, "PostgreSQL indisponivel ou ocupado; repita com a mesma Idempotency-Key."),
        _ => (500, "Erro interno; consulte os logs.")
    };
    await Results.Problem(statusCode: status, title: title).ExecuteAsync(context);
}));
app.UseRateLimiter();

app.MapGet("/", () => Results.Ok(new { name = "ShortTermRental", purpose = "Simulador educacional de reservas" }));
app.MapGet("/health/live", () => Results.Ok(new { status = "alive" }));
app.MapGet("/health/ready", async (NpgsqlDataSource source, CancellationToken ct) =>
{
    await using var cmd = source.CreateCommand("SELECT 1");
    await cmd.ExecuteScalarAsync(ct);
    return Results.Ok(new { status = "ready", authoritativeDatabase = "postgres" });
});

app.MapGet("/properties", async (int? limit, int? offset, BookingStore store, CancellationToken ct) =>
{
    var take = limit ?? 100;
    var skip = offset ?? 0;
    if (take is < 1 or > 1000 || skip is < 0 or > 100000)
        return Results.Problem(statusCode: 400, title: "limit: 1..1000; offset: 0..100000.");
    return Results.Ok(await store.ListProperties(take, skip, ct));
}).RequireRateLimiting("reads");

app.MapGet("/properties/{id:guid}", async (Guid id, PropertyCache cache, CancellationToken ct) =>
    await cache.Get(id, ct) is { } property ? Results.Ok(property) : Results.NotFound())
    .RequireRateLimiting("reads");

app.MapGet("/properties/{id:guid}/availability", async (Guid id, DateOnly checkIn, DateOnly checkOut,
    BookingStore store, CancellationToken ct) =>
{
    if (BookingRules.ValidateDates(checkIn, checkOut) is { } error)
        return Results.Problem(statusCode: 400, title: error);
    return Results.Ok(new Availability(id, checkIn, checkOut,
        await store.IsAvailable(id, checkIn, checkOut, ct)));
}).RequireRateLimiting("reads");

app.MapPost("/bookings", async (BookingRequest request, HttpContext context,
    BookingStore store, CancellationToken ct) =>
{
    if (BookingRules.Validate(request) is { } error)
        return Results.Problem(statusCode: 400, title: error);
    if (!Guid.TryParse(context.Request.Headers["Idempotency-Key"], out var key) || key == Guid.Empty)
        return Results.Problem(statusCode: 400, title: "Idempotency-Key obrigatoria: UUID nao vazio.");
    var result = await store.Create(request, key, ct);
    context.Response.Headers["Idempotency-Replayed"] = result.Replayed ? "true" : "false";
    return result.Replayed ? Results.Ok(result.Booking)
        : Results.Created($"/bookings/{result.Booking.Id}", result.Booking);
}).RequireRateLimiting("writes");

app.MapGet("/bookings/{id:guid}", async (Guid id, BookingStore store, CancellationToken ct) =>
    await store.GetBooking(id, ct) is { } booking ? Results.Ok(booking) : Results.NotFound())
    .RequireRateLimiting("reads");

app.MapPost("/bookings/{id:guid}/cancel", async (Guid id, BookingStore store, CancellationToken ct) =>
    Results.Ok(await store.Cancel(id, ct))).RequireRateLimiting("writes");

app.MapGet("/diagnostics", async (NpgsqlDataSource source, CancellationToken ct) =>
{
    await using var cmd = source.CreateCommand("""
        SELECT (SELECT count(*) FROM bookings WHERE status = 'confirmed'),
               (SELECT count(*) FROM outbox WHERE published_at IS NULL),
               (SELECT count(*) FROM processed_events),
               (SELECT coalesce(sum(active_bookings), 0)::bigint FROM booking_statistics),
               (SELECT count(*) FROM bookings a JOIN bookings b
                ON a.property_id = b.property_id AND a.id < b.id AND a.stay && b.stay
                WHERE a.status = 'confirmed' AND b.status = 'confirmed'),
               (SELECT count(*) FROM outbox)
        """);
    await using var reader = await cmd.ExecuteReaderAsync(ct);
    await reader.ReadAsync(ct);
    return Results.Ok(new
    {
        confirmedBookings = reader.GetInt64(0), pendingEvents = reader.GetInt64(1),
        processedEvents = reader.GetInt64(2), projectedActiveBookings = reader.GetInt64(3),
        overlappingPairs = reader.GetInt64(4), totalEvents = reader.GetInt64(5)
    });
}).RequireRateLimiting("reads");

app.Run();
