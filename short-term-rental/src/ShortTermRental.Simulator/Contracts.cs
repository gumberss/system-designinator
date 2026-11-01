using System.Net.Http.Json;

namespace ShortTermRental.Simulator;

public sealed record Property(Guid Id, string Name, string City);
public sealed record Request(Guid PropertyId, Guid GuestId, DateOnly CheckIn, DateOnly CheckOut);
public sealed record Booking(Guid Id, Guid PropertyId, Guid GuestId, DateOnly CheckIn,
    DateOnly CheckOut, string Status, DateTime CreatedAt);
public sealed record Availability(Guid PropertyId, DateOnly CheckIn, DateOnly CheckOut, bool Available);
public sealed record Diagnostics(long ConfirmedBookings, long PendingEvents, long ProcessedEvents,
    long ProjectedActiveBookings, long OverlappingPairs, long TotalEvents);

public static class Api
{
    public static async Task<HttpResponseMessage> Post(HttpClient client, Request body, Guid key)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "/bookings");
        request.Headers.Add("Idempotency-Key", key.ToString());
        request.Content = JsonContent.Create(body);
        return await client.SendAsync(request);
    }
}

public sealed record Options(string Command, string Url, int Concurrency, int Attempts,
    int Duration, int ReadPercent, int HotspotPercent, string ReadMode, string? SecondUrl)
{
    public static Options Parse(string[] args)
    {
        const string usage = """
            Uso:
              verify [--url http://localhost:58080] [--second-url http://localhost:58081]
                     [--attempts 200] [--concurrency 64]
              load [--url http://localhost:58080] [--duration 20] [--concurrency 32 (1..256)]
                   [--read-percent 90] [--hotspot-percent 10] [--read-mode catalog|availability]
            verify/load usam PostgreSQL local; verify tambem usa RabbitMQ (variaveis SHORT_TERM_RENTAL_*; veja README).
            """;
        if (args.Length == 0 || args[0] is not ("verify" or "load"))
            throw new ArgumentException(usage);
        var values = new Dictionary<string, string>();
        var allowed = args[0] == "verify"
            ? new[] { "--url", "--second-url", "--attempts", "--concurrency" }
            : new[] { "--url", "--duration", "--concurrency", "--read-percent", "--hotspot-percent", "--read-mode" };
        for (var i = 1; i < args.Length; i += 2)
        {
            if (i + 1 >= args.Length || !allowed.Contains(args[i]) || !values.TryAdd(args[i], args[i + 1]))
                throw new ArgumentException($"Opcao invalida ou duplicada: {args[i]}\n{usage}");
        }
        var url = values.GetValueOrDefault("--url", "http://localhost:58080");
        ValidateUrl(url);
        var secondUrl = values.GetValueOrDefault("--second-url");
        if (secondUrl is not null) ValidateUrl(secondUrl);
        var readMode = values.GetValueOrDefault("--read-mode", "catalog");
        if (readMode is not ("catalog" or "availability"))
            throw new ArgumentException("--read-mode deve ser catalog ou availability.");
        return new(args[0], url, Number("--concurrency", args[0] == "verify" ? 64 : 32,
                1, args[0] == "verify" ? 128 : 256),
            Number("--attempts", 200, 2, 10000), Number("--duration", 20, 1, 300),
            Number("--read-percent", 90, 0, 100), Number("--hotspot-percent", 10, 0, 100), readMode, secondUrl);

        static void ValidateUrl(string value)
        {
            if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https") ||
                uri.AbsolutePath != "/" || uri.Query.Length != 0)
                throw new ArgumentException("URL deve ser uma origem HTTP(S), sem caminho ou query.");
        }

        int Number(string name, int fallback, int min, int max)
        {
            if (!values.TryGetValue(name, out var text)) return fallback;
            if (!int.TryParse(text, out var number) || number < min || number > max)
                throw new ArgumentException($"{name} deve estar entre {min} e {max}.");
            return number;
        }
    }
}
