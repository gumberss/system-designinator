using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using ShortTermRental.Simulator;

try
{
    var options = Options.Parse(args);
    using var client = new HttpClient(new SocketsHttpHandler
    {
        MaxConnectionsPerServer = options.Concurrency,
        PooledConnectionLifetime = TimeSpan.FromMinutes(2)
    }) { BaseAddress = new Uri(options.Url), Timeout = TimeSpan.FromSeconds(30) };
    using var ready = await client.GetAsync("/health/ready");
    ready.EnsureSuccessStatusCode();
    var properties = await client.GetFromJsonAsync<Property[]>("/properties?limit=1000")
        ?? throw new InvalidOperationException("API nao retornou imoveis.");
    if (properties.Length < 32)
        throw new InvalidOperationException("Sao necessarios pelo menos 32 imoveis; execute o seed.");

    if (options.Command == "verify")
    {
        using var secondClient = options.SecondUrl is null ? null : new HttpClient
        {
            BaseAddress = new Uri(options.SecondUrl), Timeout = TimeSpan.FromSeconds(30)
        };
        if (secondClient is not null)
        {
            using var secondReady = await secondClient.GetAsync("/health/ready");
            secondReady.EnsureSuccessStatusCode();
        }
        await new Verification(client, properties, options, secondClient).Run();
    }
    else
        await RunLoad(client, properties, options);
    return 0;
}
catch (Exception ex)
{
    Console.Error.WriteLine($"FALHOU: {ex}");
    return 1;
}

static async Task RunLoad(HttpClient client, Property[] properties, Options options)
{
    var start = await LabDatabase.NextLoadWindow();
    var hotProperty = properties[0];
    var latencies = new List<double>();
    var statuses = new Dictionary<string, long>();
    var gate = new object();
    long sequence = 0, reads = 0, writes = 0;
    var inFlight = 0;
    var peakInFlight = 0;
    var timer = Stopwatch.StartNew();
    using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(options.Duration));
    var workers = Enumerable.Range(0, options.Concurrency).Select(async _ =>
    {
        while (!deadline.IsCancellationRequested)
        {
            var isRead = Random.Shared.Next(100) < options.ReadPercent;
            var tick = Stopwatch.GetTimestamp();
            string status;
            try
            {
                RecordInFlight(Interlocked.Increment(ref inFlight));
                using HttpResponseMessage response = isRead
                    ? await Read()
                    : await Write();
                // Include downloading the complete response, not just receiving headers.
                await response.Content.ReadAsByteArrayAsync();
                status = $"{(isRead ? "read" : "write")}:{(int)response.StatusCode}";
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                Console.Error.WriteLine($"Erro de transporte: {ex.Message}");
                status = $"{(isRead ? "read" : "write")}:transport-error";
            }
            finally
            {
                Interlocked.Decrement(ref inFlight);
            }
            var elapsed = Stopwatch.GetElapsedTime(tick).TotalMilliseconds;
            lock (gate)
            {
                statuses[status] = statuses.GetValueOrDefault(status) + 1;
                latencies.Add(elapsed);
            }
            if (isRead) Interlocked.Increment(ref reads);
            else Interlocked.Increment(ref writes);
        }
    });
    await Task.WhenAll(workers);
    timer.Stop();
    latencies.Sort();
    var diagnostics = await client.GetFromJsonAsync<Diagnostics>("/diagnostics")
        ?? throw new InvalidOperationException("Diagnostico ausente.");
    var unexpected = statuses.Where(pair => pair.Key.EndsWith("transport-error", StringComparison.Ordinal) ||
        (pair.Key.StartsWith("read:", StringComparison.Ordinal) && pair.Key is not ("read:200" or "read:429")) ||
        (pair.Key.StartsWith("write:", StringComparison.Ordinal) &&
            pair.Key is not ("write:201" or "write:409" or "write:429")) ||
        (pair.Key == "write:409" && options.HotspotPercent == 0)).Sum(pair => pair.Value);
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        mode = "closed-loop-load", options.Concurrency, options.ReadPercent,
        options.HotspotPercent, options.ReadMode, checkInBase = start,
        peakClientRequestsInFlight = peakInFlight,
        elapsedSeconds = timer.Elapsed.TotalSeconds,
        attemptedRequests = reads + writes, reads, writes,
        attemptedRequestsPerSecond = (reads + writes) / timer.Elapsed.TotalSeconds,
        confirmedWritesPerSecond = statuses.GetValueOrDefault("write:201") / timer.Elapsed.TotalSeconds,
        statusCounts = statuses, unexpectedResponses = unexpected,
        latencyMs = new { p50 = Percentile(0.50), p95 = Percentile(0.95), p99 = Percentile(0.99) },
        diagnostics,
        warning = "Throughput local medido; nao representa 100M/10M RPS. 409 nao e reserva confirmada."
    }, new JsonSerializerOptions { WriteIndented = true }));
    if (diagnostics.OverlappingPairs != 0 || unexpected != 0)
        throw new InvalidOperationException("Carga apresentou sobreposicao ou respostas inesperadas.");

    double Percentile(double p) => latencies.Count == 0 ? 0 :
        latencies[(int)Math.Ceiling(p * latencies.Count) - 1];

    void RecordInFlight(int current)
    {
        var peak = Volatile.Read(ref peakInFlight);
        while (current > peak)
        {
            var observed = Interlocked.CompareExchange(ref peakInFlight, current, peak);
            if (observed == peak) return;
            peak = observed;
        }
    }

    Task<HttpResponseMessage> Read()
    {
        var property = properties[Random.Shared.Next(properties.Length)];
        return client.GetAsync(options.ReadMode == "catalog"
            ? $"/properties/{property.Id}"
            : $"/properties/{property.Id}/availability?checkIn={start:yyyy-MM-dd}&checkOut={start.AddDays(2):yyyy-MM-dd}");
    }

    Task<HttpResponseMessage> Write()
    {
        var index = Interlocked.Increment(ref sequence) - 1;
        var hot = Random.Shared.Next(100) < options.HotspotPercent;
        var property = hot ? hotProperty : properties[1 + (int)(index % (properties.Length - 1))];
        var nightsOffset = hot ? 0 : checked((int)(index / (properties.Length - 1)) * 2);
        var checkIn = start.AddDays(nightsOffset);
        return Api.Post(client, new(property.Id, Guid.NewGuid(), checkIn, checkIn.AddDays(2)), Guid.NewGuid());
    }
}
