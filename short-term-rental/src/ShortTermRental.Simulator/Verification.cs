using System.Collections.Concurrent;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using Npgsql;
using RabbitMQ.Client;

namespace ShortTermRental.Simulator;

public sealed class Verification(HttpClient client, Property[] properties, Options options, HttpClient? secondClient)
{
    private readonly ConcurrentBag<Guid> created = [];
    private readonly DateOnly start = DateOnly.FromDateTime(DateTime.UtcNow)
        .AddDays(Random.Shared.Next(10000, 60000));

    public async Task Run()
    {
        try
        {
            var property = properties[0];
            var request = new Request(property.Id, Guid.NewGuid(), start, start.AddDays(5));
            Require((await Available(request)).Available, "Janela de teste deve estar livre.");
            var race = new ConcurrentBag<(HttpStatusCode Status, Booking? Booking, Guid Key, Request Request)>();
            await Parallel.ForEachAsync(Enumerable.Range(0, options.Attempts),
                new ParallelOptions { MaxDegreeOfParallelism = options.Concurrency }, async (index, _) =>
                {
                    var key = Guid.NewGuid();
                    var competitor = request with { GuestId = Guid.NewGuid() };
                    using var response = await Api.Post(ClientFor(index), competitor, key);
                    Booking? booking = null;
                    if (response.StatusCode == HttpStatusCode.Created)
                    {
                        booking = await ReadBooking(response);
                        created.Add(booking.Id);
                    }
                    race.Add((response.StatusCode, booking, key, competitor));
                });
            var winners = race.Where(result => result.Status == HttpStatusCode.Created).ToArray();
            var statusSummary = string.Join(", ", race.GroupBy(result => (int)result.Status)
                .OrderBy(group => group.Key).Select(group => $"{group.Key}={group.Count()}"));
            Require(winners.Length == 1, $"Exatamente 1 vencedor; encontrados {winners.Length}.");
            Require(race.Count(result => result.Status == HttpStatusCode.Conflict) == options.Attempts - 1,
                $"Todos os outros guests devem receber 409, nao 429/500. Recebido: {statusSummary}.");
            await AssertRejectedKeysRolledBack(race.Where(result => result.Status == HttpStatusCode.Conflict)
                .Select(result => result.Key).ToArray());
            Pass($"Disputa: {options.Attempts} tentativas, 1 confirmacao, {options.Attempts - 1} conflitos.");
            var winner = winners[0];
            var winningBooking = winner.Booking ?? throw new InvalidOperationException("Vencedor sem reserva.");
            Require(!(await Available(request)).Available, "Disponibilidade deve mudar depois do commit.");

            await Parallel.ForEachAsync(Enumerable.Range(0, 32),
                new ParallelOptions { MaxDegreeOfParallelism = options.Concurrency }, async (index, _) =>
                {
                    using var response = await Api.Post(ClientFor(index), winner.Request, winner.Key);
                    Require(response.StatusCode == HttpStatusCode.OK, "Replay deve retornar 200.");
                    Require(response.Headers.GetValues("Idempotency-Replayed").Single() == "true",
                        "Replay deve ser identificado no header.");
                    Require((await ReadBooking(response)).Id == winningBooking.Id,
                        "Retries devem retornar o mesmo bookingId.");
                });
            using (var changed = await Api.Post(client, winner.Request with { GuestId = Guid.NewGuid() }, winner.Key))
                Require(changed.StatusCode == HttpStatusCode.Conflict, "Mesma chave + payload diferente deve dar 409.");
            Pass("Idempotencia: 32 retries concorrentes; payload diferente rejeitado.");

            var sameKey = Guid.NewGuid();
            var sameRequest = request with { CheckIn = start.AddDays(20), CheckOut = start.AddDays(22) };
            var sameKeyIds = new ConcurrentBag<Guid>();
            var sameKeyStatuses = new ConcurrentBag<HttpStatusCode>();
            await Parallel.ForEachAsync(Enumerable.Range(0, 32),
                new ParallelOptions { MaxDegreeOfParallelism = options.Concurrency }, async (index, _) =>
                {
                    using var response = await Api.Post(ClientFor(index), sameRequest, sameKey);
                    Require(response.StatusCode is HttpStatusCode.Created or HttpStatusCode.OK,
                        "Disputa pela mesma chave deve criar ou reexecutar, nunca conflitar.");
                    var booking = await ReadBooking(response);
                    created.Add(booking.Id);
                    sameKeyIds.Add(booking.Id);
                    sameKeyStatuses.Add(response.StatusCode);
                });
            Require(sameKeyIds.Distinct().Count() == 1 &&
                sameKeyStatuses.Count(code => code == HttpStatusCode.Created) == 1,
                "Mesma chave nova deve gerar uma unica reserva e um unico 201.");
            Pass("Chave nova simultanea: 1 criacao + 31 replays.");

            foreach (var range in new[] { (1, 3), (-1, 1), (4, 6), (-1, 6) })
            {
                using var response = await Api.Post(client,
                    request with { CheckIn = start.AddDays(range.Item1), CheckOut = start.AddDays(range.Item2) },
                    Guid.NewGuid());
                Require(response.StatusCode == HttpStatusCode.Conflict, "Sobreposicao parcial/contida deve dar 409.");
            }
            await Create(request with { CheckIn = start.AddDays(-2), CheckOut = start });
            await Create(request with { CheckIn = start.AddDays(5), CheckOut = start.AddDays(7) });
            Pass("Intervalos: contencao em overlaps; check-out/check-in adjacentes permitidos.");
            await AssertDirectDatabaseConstraint(request);
            Pass("Escrita SQL direta tambem rejeitada pela constraint (SQLSTATE 23P01).");

            using (var invalid = await Api.Post(client, request with { CheckOut = start }, Guid.NewGuid()))
                Require(invalid.StatusCode == HttpStatusCode.BadRequest, "Estadia vazia deve dar 400.");
            using (var missingKey = await client.PostAsJsonAsync("/bookings", request))
                Require(missingKey.StatusCode == HttpStatusCode.BadRequest, "Sem chave deve dar 400.");
            using (var unknown = await Api.Post(client, request with { PropertyId = Guid.NewGuid() }, Guid.NewGuid()))
                Require(unknown.StatusCode == HttpStatusCode.NotFound, "Imovel desconhecido deve dar 404.");
            Pass("Validacao HTTP: datas, chave obrigatoria e imovel inexistente.");

            await Cancel(winningBooking.Id);
            await Cancel(winningBooking.Id);
            Require((await Available(request)).Available, "Cancelamento deve liberar o intervalo.");
            using (var replay = await Api.Post(client, winner.Request, winner.Key))
            {
                Require(replay.StatusCode == HttpStatusCode.OK, "Replay de cancelada deve dar 200.");
                Require((await ReadBooking(replay)).Status == "cancelled", "Replay nao pode ressuscitar reserva.");
            }
            await Create(request);
            Pass("Cancelamento idempotente, liberacao das datas e replay sem ressurreicao.");

            await Parallel.ForEachAsync(properties.Skip(1).Take(24),
                new ParallelOptions { MaxDegreeOfParallelism = options.Concurrency }, async (other, _) =>
                    await Create(request with { PropertyId = other.Id }));
            Pass("24 imoveis diferentes confirmados em paralelo, sem lock global.");

            await WaitForProjection();
            await AssertDuplicateAndDeadLetter(winningBooking.Id);
            Pass("Outbox drenada, projecao consistente, evento duplicado deduplicado e poison em DLQ.");
        }

        finally
        {
            // Cancel only bookings created by this execution; preserve unrelated reservations.
            foreach (var id in created.Distinct())
                await Cancel(id);
        }
        var final = await WaitForProjection();
        Require(final.OverlappingPairs == 0, "Banco deve conter zero pares confirmados sobrepostos.");
        Console.WriteLine($"PASS: verificacao completa; zero overlaps; eventos processados={final.ProcessedEvents}.");
    }

    private HttpClient ClientFor(int index) => secondClient is not null && index % 2 == 1 ? secondClient : client;

    private async Task<Booking> Create(Request request)
    {
        using var response = await Api.Post(client, request, Guid.NewGuid());
        Require(response.StatusCode == HttpStatusCode.Created,
            $"Reserva independente deve dar 201; recebido {(int)response.StatusCode}: {await response.Content.ReadAsStringAsync()}");
        var booking = await ReadBooking(response);
        created.Add(booking.Id);
        return booking;
    }

    private async Task Cancel(Guid id)
    {
        using var response = await client.PostAsync($"/bookings/{id}/cancel", null);
        response.EnsureSuccessStatusCode();
    }

    private async Task<Availability> Available(Request request) =>
        await client.GetFromJsonAsync<Availability>(
            $"/properties/{request.PropertyId}/availability?checkIn={request.CheckIn:yyyy-MM-dd}&checkOut={request.CheckOut:yyyy-MM-dd}")
        ?? throw new InvalidOperationException("Disponibilidade ausente.");

    private static async Task<Booking> ReadBooking(HttpResponseMessage response)
    {
        var booking = await response.Content.ReadFromJsonAsync<Booking>()
            ?? throw new InvalidOperationException("Reserva ausente.");
        Require(booking.Id.Version == 7, "Novas reservas devem usar UUID v7, inclusive nos replays.");
        return booking;
    }

    private async Task<Diagnostics> WaitForProjection()
    {
        for (var i = 0; i < 120; i++)
        {
            var data = await client.GetFromJsonAsync<Diagnostics>("/diagnostics")
                ?? throw new InvalidOperationException("Diagnostico ausente.");
            if (data.PendingEvents == 0 && data.ProcessedEvents == data.TotalEvents &&
                data.ProjectedActiveBookings == data.ConfirmedBookings && data.OverlappingPairs == 0)
                return data;
            await Task.Delay(250);
        }
        throw new InvalidOperationException("Outbox/projecao nao convergiu em 30s; consulte os logs.");
    }

    private static async Task AssertRejectedKeysRolledBack(Guid[] keys)
    {
        await using var connection = LabDatabase.Connect();
        await connection.OpenAsync();
        await using var cmd = new NpgsqlCommand(
            "SELECT count(*) FROM booking_requests WHERE idempotency_key = ANY(@keys)", connection);
        cmd.Parameters.AddWithValue("keys", keys);
        Require((long)(await cmd.ExecuteScalarAsync()
            ?? throw new InvalidOperationException("Contagem ausente.")) == 0,
            "Requests rejeitados por overlap nao podem persistir chaves; rollback deve ser atomico.");
    }

    private static async Task AssertDirectDatabaseConstraint(Request request)
    {
        await using var connection = LabDatabase.Connect();
        await connection.OpenAsync();
        await using var tx = await connection.BeginTransactionAsync();
        var key = Guid.NewGuid();
        await using var cmd = new NpgsqlCommand("""
            INSERT INTO booking_requests(idempotency_key, property_id, guest_id, check_in, check_out)
            VALUES (@key, @property, @guest, @start, @end);
            INSERT INTO bookings(id, idempotency_key, property_id, guest_id, check_in, check_out, status)
            VALUES (@id, @key, @property, @guest, @start, @end, 'confirmed');
            """, connection, tx);
        cmd.Parameters.AddWithValue("key", key);
        cmd.Parameters.AddWithValue("id", Guid.NewGuid());
        cmd.Parameters.AddWithValue("property", request.PropertyId);
        cmd.Parameters.AddWithValue("guest", Guid.NewGuid());
        cmd.Parameters.AddWithValue("start", request.CheckIn);
        cmd.Parameters.AddWithValue("end", request.CheckOut);
        try
        {
            await cmd.ExecuteNonQueryAsync();
            throw new InvalidOperationException("BUG: SQL direto conseguiu overbooking.");
        }
        catch (PostgresException ex) when (ex.SqlState == PostgresErrorCodes.ExclusionViolation)
        {
            await tx.RollbackAsync();
        }
    }

    private async Task AssertDuplicateAndDeadLetter(Guid bookingId)
    {
        await using var connection = LabDatabase.Connect();
        await connection.OpenAsync();
        await using var cmd = new NpgsqlCommand(
            "SELECT event_id, payload::text FROM outbox WHERE booking_id = @id AND event_type = 'booking.confirmed'", connection);
        cmd.Parameters.AddWithValue("id", bookingId);
        Guid eventId;
        string payload;
        await using (var reader = await cmd.ExecuteReaderAsync())
        {
            Require(await reader.ReadAsync(), "Evento original nao encontrado.");
            eventId = reader.GetGuid(0);
            payload = reader.GetString(1);
        }
        var deliveriesBefore = await Deliveries();
        var before = await WaitForProjection();
        var factory = new ConnectionFactory
        {
            HostName = Environment.GetEnvironmentVariable("SHORT_TERM_RENTAL_RABBIT_HOST") ?? "localhost",
            Port = int.Parse(Environment.GetEnvironmentVariable("SHORT_TERM_RENTAL_RABBIT_PORT") ?? "55672"),
            UserName = Environment.GetEnvironmentVariable("SHORT_TERM_RENTAL_RABBIT_USER") ?? "short-term-rental",
            Password = Environment.GetEnvironmentVariable("SHORT_TERM_RENTAL_RABBIT_PASSWORD") ?? "local-development-only"
        };
        await using var rabbit = await factory.CreateConnectionAsync();
        await using var channel = await rabbit.CreateChannelAsync(
            new CreateChannelOptions(publisherConfirmationsEnabled: true, publisherConfirmationTrackingEnabled: true));
        var dlqBefore = await channel.QueueDeclarePassiveAsync("booking-statistics-dead-letter");
        await Publish(payload);
        await Publish("{}");
        for (var i = 0; i < 120; i++)
        {
            var queue = await channel.QueueDeclarePassiveAsync("booking-statistics");
            var dlqAfter = await channel.QueueDeclarePassiveAsync("booking-statistics-dead-letter");
            if (queue.MessageCount == 0 && dlqAfter.MessageCount == dlqBefore.MessageCount + 1 &&
                await Deliveries() > deliveriesBefore)
            {
                var after = await WaitForProjection();
                Require(after.ProcessedEvents == before.ProcessedEvents &&
                    after.ProjectedActiveBookings == before.ProjectedActiveBookings,
                    "Evento duplicado nao deve alterar inbox nem projecao.");
                return;
            }
            await Task.Delay(250);
        }
        throw new InvalidOperationException("Duplicata/poison nao processados em 30s.");

        async Task<long> Deliveries()
        {
            await using var count = new NpgsqlCommand(
                "SELECT deliveries FROM processed_events WHERE event_id = @id", connection);
            count.Parameters.AddWithValue("id", eventId);
            return (long)(await count.ExecuteScalarAsync()
                ?? throw new InvalidOperationException("Evento ainda nao consta na inbox."));
        }

        async Task Publish(string body)
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await channel.BasicPublishAsync("booking-events", "booking.changed", true,
                new BasicProperties { Persistent = true, ContentType = "application/json" },
                Encoding.UTF8.GetBytes(body), timeout.Token);
        }
    }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static void Pass(string message) => Console.WriteLine($"PASS: {message}");
}
