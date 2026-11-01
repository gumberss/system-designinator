using System.Text.Json;
using Npgsql;
using NpgsqlTypes;

namespace ShortTermRental.Api;

public sealed class BookingStore(NpgsqlDataSource source)
{
    private const string Columns = "id, property_id, guest_id, check_in, check_out, status, created_at";

    public async Task<IReadOnlyList<Property>> ListProperties(int limit, int offset, CancellationToken ct)
    {
        await using var cmd = source.CreateCommand(
            "SELECT id, name, city FROM properties ORDER BY id LIMIT @limit OFFSET @offset");
        cmd.Parameters.AddWithValue("limit", limit);
        cmd.Parameters.AddWithValue("offset", offset);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<Property>();
        while (await reader.ReadAsync(ct))
            results.Add(new(reader.GetGuid(0), reader.GetString(1), reader.GetString(2)));
        return results;
    }

    public async Task<Property?> GetProperty(Guid id, CancellationToken ct)
    {
        await using var cmd = source.CreateCommand("SELECT id, name, city FROM properties WHERE id = @id");
        cmd.Parameters.AddWithValue("id", id);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct)
            ? new(reader.GetGuid(0), reader.GetString(1), reader.GetString(2)) : null;
    }

    public async Task<bool> IsAvailable(Guid propertyId, DateOnly checkIn, DateOnly checkOut, CancellationToken ct)
    {
        await using var cmd = source.CreateCommand("""
            SELECT NOT EXISTS (
                SELECT 1 FROM bookings
                WHERE property_id = @property AND status = 'confirmed'
                  AND stay && daterange(@start, @end, '[)')
            ) FROM properties WHERE id = @property
            """);
        cmd.Parameters.AddWithValue("property", propertyId);
        cmd.Parameters.AddWithValue("start", checkIn);
        cmd.Parameters.AddWithValue("end", checkOut);
        var result = await cmd.ExecuteScalarAsync(ct);
        if (result is not bool available)
            throw new ResourceNotFoundException("Imovel nao encontrado.");
        return available;
    }

    public async Task<Booking?> GetBooking(Guid id, CancellationToken ct)
    {
        await using var cmd = source.CreateCommand($"SELECT {Columns} FROM bookings WHERE id = @id");
        cmd.Parameters.AddWithValue("id", id);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? ReadBooking(reader) : null;
    }

    public async Task<BookingResult> Create(BookingRequest request, Guid key, CancellationToken ct)
    {
        await using var connection = await source.OpenConnectionAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        // A competing transaction with the same key waits on the unique index.
        await using var claim = new NpgsqlCommand("""
            INSERT INTO booking_requests(idempotency_key, property_id, guest_id, check_in, check_out)
            VALUES (@key, @property, @guest, @start, @end)
            ON CONFLICT (idempotency_key) DO NOTHING
            """, connection, transaction);
        AddRequest(claim, request, key);
        var claimed = await claim.ExecuteNonQueryAsync(ct);
        if (claimed == 0)
        {
            await using var existing = new NpgsqlCommand(
                $"SELECT {Columns} FROM bookings WHERE idempotency_key = @key", connection, transaction);
            existing.Parameters.AddWithValue("key", key);
            Booking booking;
            await using (var reader = await existing.ExecuteReaderAsync(ct))
            {
                if (!await reader.ReadAsync(ct))
                    throw new InvalidOperationException("Chave persistida sem reserva correspondente.");
                booking = ReadBooking(reader);
            }
            if (booking.PropertyId != request.PropertyId || booking.GuestId != request.GuestId ||
                booking.CheckIn != request.CheckIn || booking.CheckOut != request.CheckOut)
                throw new BookingConflictException("Idempotency-Key ja usada com outro pedido.");
            await transaction.CommitAsync(ct);
            return new(booking, true);
        }

        // NO KEY UPDATE serializes this property's writers without conflicting with FK KEY SHARE locks.
        // KEY SHARE protegeria a existência/chave do imóvel, mas permitiria
        // várias reservas avançarem simultaneamente; a FK já fornece essa proteção.
        // NO KEY UPDATE serializa novas reservas por imóvel até o fim da transação,
        // reduzindo a disputa na constraint EXCLUDE/GiST sem conflitar com KEY SHARE.
        // interessante!. 01-arquitetura.md — ADR-07, 09-locks-do-postgresql.md
        await using var admission = new NpgsqlCommand(
            "SELECT id FROM properties WHERE id = @property FOR NO KEY UPDATE", connection, transaction);
        admission.Parameters.AddWithValue("property", request.PropertyId);
        if (await admission.ExecuteScalarAsync(ct) is null)
            throw new ResourceNotFoundException("Imovel nao encontrado.");
        // A proteção contra overbooking é a constraint EXCLUDE, apoiada em GiST.
        // Ela impede reservas confirmadas do mesmo imóvel com estadias sobrepostas.
        // O banco gera stay a partir de check_in e check_out no formato [check_in, check_out).
        await using var insert = new NpgsqlCommand($"""
            INSERT INTO bookings(id, idempotency_key, property_id, guest_id, check_in, check_out, status)
            VALUES (@id, @key, @property, @guest, @start, @end, 'confirmed')
            RETURNING {Columns}
            """, connection, transaction);
        AddRequest(insert, request, key);
        insert.Parameters.AddWithValue("id", Guid.CreateVersion7());
        Booking created;
        await using (var reader = await insert.ExecuteReaderAsync(ct))
        {
            await reader.ReadAsync(ct);
            created = ReadBooking(reader);
        }
        await InsertEvent(connection, transaction, created, "booking.confirmed", ct);
        await transaction.CommitAsync(ct);
        return new(created, false);
    }

    public async Task<Booking> Cancel(Guid id, CancellationToken ct)
    {
        await using var connection = await source.OpenConnectionAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        await using var select = new NpgsqlCommand(
            $"SELECT {Columns} FROM bookings WHERE id = @id FOR UPDATE", connection, transaction);
        select.Parameters.AddWithValue("id", id);
        Booking booking;
        await using (var reader = await select.ExecuteReaderAsync(ct))
        {
            if (!await reader.ReadAsync(ct))
                throw new ResourceNotFoundException("Reserva nao encontrada.");
            booking = ReadBooking(reader);
        }
        if (booking.Status != "cancelled")
        {
            await using var update = new NpgsqlCommand("""
                UPDATE bookings SET status = 'cancelled', cancelled_at = now() WHERE id = @id
                """, connection, transaction);
            update.Parameters.AddWithValue("id", id);
            await update.ExecuteNonQueryAsync(ct);
            booking = booking with { Status = "cancelled" };
            await InsertEvent(connection, transaction, booking, "booking.cancelled", ct);
        }
        await transaction.CommitAsync(ct);
        return booking;
    }

    private static async Task InsertEvent(NpgsqlConnection connection, NpgsqlTransaction transaction,
        Booking booking, string type, CancellationToken ct)
    {
        var evt = new BookingEvent(Guid.NewGuid(), type, booking.Id, booking.PropertyId);
        await using var cmd = new NpgsqlCommand("""
            INSERT INTO outbox(event_id, booking_id, event_type, payload)
            VALUES (@event, @booking, @type, @payload)
            """, connection, transaction);
        cmd.Parameters.AddWithValue("event", evt.EventId);
        cmd.Parameters.AddWithValue("booking", evt.BookingId);
        cmd.Parameters.AddWithValue("type", evt.EventType);
        cmd.Parameters.AddWithValue("payload", NpgsqlDbType.Jsonb, JsonSerializer.Serialize(evt));
        await cmd.ExecuteNonQueryAsync(ct);
    }

    private static void AddRequest(NpgsqlCommand cmd, BookingRequest request, Guid key)
    {
        cmd.Parameters.AddWithValue("key", key);
        cmd.Parameters.AddWithValue("property", request.PropertyId);
        cmd.Parameters.AddWithValue("guest", request.GuestId);
        cmd.Parameters.AddWithValue("start", request.CheckIn);
        cmd.Parameters.AddWithValue("end", request.CheckOut);
    }

    private static Booking ReadBooking(NpgsqlDataReader reader) => new(
        reader.GetGuid(0), reader.GetGuid(1), reader.GetGuid(2),
        reader.GetFieldValue<DateOnly>(3), reader.GetFieldValue<DateOnly>(4),
        reader.GetString(5), reader.GetDateTime(6));
}
