using Npgsql;

namespace ShortTermRental.Simulator;

internal static class LabDatabase
{
    public static NpgsqlConnection Connect() => new(
        Environment.GetEnvironmentVariable("SHORT_TERM_RENTAL_POSTGRES") ??
        "Host=localhost;Port=55432;Database=short-term-rental;Username=short-term-rental;Password=local-development-only;GSS Encryption Mode=Disable");

    public static async Task<DateOnly> NextLoadWindow()
    {
        // Fixtures for sequential local load runs, not a distributed allocation protocol.
        await using var connection = Connect();
        await connection.OpenAsync();
        await using var cmd = new NpgsqlCommand(
            "SELECT coalesce(max(check_out), CURRENT_DATE) + 1 FROM bookings", connection);
        await using var reader = await cmd.ExecuteReaderAsync();
        await reader.ReadAsync();
        return reader.GetFieldValue<DateOnly>(0);
    }
}
