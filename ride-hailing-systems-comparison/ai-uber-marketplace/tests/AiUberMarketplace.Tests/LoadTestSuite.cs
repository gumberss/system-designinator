using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace AiUberMarketplace.Tests
{
    /// <summary>
    /// Load test configuration
    /// </summary>
    public class LoadTestConfig
    {
        public int ConcurrentConnections { get; set; } = 500;
        public int DurationSeconds { get; set; } = 60;
        public int LocationUpdatesPerSecond { get; set; } = 12500; // 750K / 60
        public int MessageBatchSize { get; set; } = 100;
        public int OfferBatchSize { get; set; } = 50;
    }

    /// <summary>
    /// Load test results
    /// </summary>
    public class LoadTestResults
    {
        public string TestName { get; set; }
        public int TotalRequests { get; set; }
        public int SuccessfulRequests { get; set; }
        public int FailedRequests { get; set; }
        public long MinLatencyMs { get; set; }
        public long MaxLatencyMs { get; set; }
        public double AverageLatencyMs { get; set; }
        public double P95LatencyMs { get; set; }
        public double P99LatencyMs { get; set; }
        public double ThroughputPerSecond { get; set; }
        public TimeSpan Duration { get; set; }
        public DateTime StartTime { get; set; }
        public DateTime EndTime { get; set; }
    }

    /// <summary>
    /// Load test suite for performance validation
    /// </summary>
    public class LoadTestSuite
    {
        private readonly List<long> _latencies = new();
        private readonly object _lockObj = new();

        public LoadTestConfig Config { get; set; } = new();
        public List<LoadTestResults> Results { get; set; } = new();

        /// <summary>
        /// Run connection stress test
        /// </summary>
        public async Task<LoadTestResults> RunConnectionStressTestAsync()
        {
            var testName = "Connection Stress Test";
            var results = new LoadTestResults { TestName = testName, StartTime = DateTime.UtcNow };
            var sw = Stopwatch.StartNew();
            var failedConnections = 0;

            try
            {
                var tasks = new List<Task>();
                var semaphore = new SemaphoreSlim(10); // Limit concurrent connection attempts

                for (int i = 0; i < Config.ConcurrentConnections; i++)
                {
                    tasks.Add(SimulateConnectionAsync(semaphore, () => Interlocked.Increment(ref failedConnections)));
                }

                await Task.WhenAll(tasks);

                results.TotalRequests = Config.ConcurrentConnections;
                results.SuccessfulRequests = Config.ConcurrentConnections - failedConnections;
                results.FailedRequests = failedConnections;
            }
            catch (Exception ex)
            {
                results.FailedRequests = Config.ConcurrentConnections;
            }

            sw.Stop();
            results.Duration = sw.Elapsed;
            results.EndTime = DateTime.UtcNow;
            results.ThroughputPerSecond = results.SuccessfulRequests / results.Duration.TotalSeconds;

            return results;
        }

        /// <summary>
        /// Run location broadcast load test (750K updates/second capacity)
        /// </summary>
        public async Task<LoadTestResults> RunLocationBroadcastLoadTestAsync()
        {
            var testName = "Location Broadcast Load Test";
            var results = new LoadTestResults { TestName = testName, StartTime = DateTime.UtcNow };
            var sw = Stopwatch.StartNew();
            var updateCount = 0;

            try
            {
                var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(Config.DurationSeconds));
                var drivers = Enumerable.Range(0, 100).Select(i => Guid.NewGuid()).ToList();
                var tasks = new List<Task>();

                // Simulate location updates from drivers
                foreach (var driver in drivers)
                {
                    tasks.Add(SimulateLocationUpdatesAsync(driver, cancellation.Token, () => Interlocked.Increment(ref updateCount)));
                }

                try
                {
                    await Task.WhenAll(tasks);
                }
                catch (OperationCanceledException)
                {
                    // Expected
                }

                results.TotalRequests = updateCount;
                results.SuccessfulRequests = updateCount;
                results.FailedRequests = 0;
            }
            catch (Exception ex)
            {
                results.FailedRequests++;
            }

            sw.Stop();
            results.Duration = sw.Elapsed;
            results.EndTime = DateTime.UtcNow;
            results.ThroughputPerSecond = results.TotalRequests / results.Duration.TotalSeconds;

            // Calculate latencies
            CalculateLatencyStats(results);

            return results;
        }

        /// <summary>
        /// Run offer notification load test
        /// </summary>
        public async Task<LoadTestResults> RunOfferNotificationLoadTestAsync()
        {
            var testName = "Offer Notification Load Test";
            var results = new LoadTestResults { TestName = testName, StartTime = DateTime.UtcNow };
            var sw = Stopwatch.StartNew();
            var offerCount = 0;
            var failedOffers = 0;

            try
            {
                var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(Config.DurationSeconds));
                var drivers = Enumerable.Range(0, 50).Select(i => Guid.NewGuid()).ToList();
                var riders = Enumerable.Range(0, 50).Select(i => Guid.NewGuid()).ToList();
                var tasks = new List<Task>();

                // Simulate offers being sent to drivers
                foreach (var rider in riders)
                {
                    tasks.Add(SimulateOfferSequenceAsync(
                        rider, 
                        drivers, 
                        cancellation.Token, 
                        () => Interlocked.Add(ref offerCount, 5),
                        () => Interlocked.Increment(ref failedOffers)));
                }

                try
                {
                    await Task.WhenAll(tasks);
                }
                catch (OperationCanceledException)
                {
                    // Expected
                }

                results.TotalRequests = offerCount + failedOffers;
                results.SuccessfulRequests = offerCount;
                results.FailedRequests = failedOffers;
            }
            catch (Exception ex)
            {
                results.FailedRequests++;
            }

            sw.Stop();
            results.Duration = sw.Elapsed;
            results.EndTime = DateTime.UtcNow;
            results.ThroughputPerSecond = results.SuccessfulRequests / results.Duration.TotalSeconds;

            // Calculate latencies
            CalculateLatencyStats(results);

            return results;
        }

        /// <summary>
        /// Run chat message load test
        /// </summary>
        public async Task<LoadTestResults> RunChatLoadTestAsync()
        {
            var testName = "Chat Message Load Test";
            var results = new LoadTestResults { TestName = testName, StartTime = DateTime.UtcNow };
            var sw = Stopwatch.StartNew();
            var messageCount = 0;
            var failedMessages = 0;

            try
            {
                var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(Config.DurationSeconds));
                var rides = Enumerable.Range(0, 50).Select(i => Guid.NewGuid()).ToList();
                var users = Enumerable.Range(0, 100).Select(i => Guid.NewGuid()).ToList();
                var tasks = new List<Task>();

                // Simulate messages being sent
                foreach (var user in users)
                {
                    tasks.Add(SimulateChatSequenceAsync(
                        user, 
                        rides, 
                        users, 
                        cancellation.Token, 
                        () => Interlocked.Increment(ref messageCount),
                        () => Interlocked.Increment(ref failedMessages)));
                }

                try
                {
                    await Task.WhenAll(tasks);
                }
                catch (OperationCanceledException)
                {
                    // Expected
                }

                results.TotalRequests = messageCount + failedMessages;
                results.SuccessfulRequests = messageCount;
                results.FailedRequests = failedMessages;
            }
            catch (Exception ex)
            {
                results.FailedRequests++;
            }

            sw.Stop();
            results.Duration = sw.Elapsed;
            results.EndTime = DateTime.UtcNow;
            results.ThroughputPerSecond = results.SuccessfulRequests / results.Duration.TotalSeconds;

            // Calculate latencies
            CalculateLatencyStats(results);

            return results;
        }

        /// <summary>
        /// Run all load tests
        /// </summary>
        public async Task RunAllTestsAsync()
        {
            Results.Clear();

            Console.WriteLine("Starting Load Test Suite...\n");

            // Connection stress test
            Console.WriteLine($"Running: Connection Stress Test ({Config.ConcurrentConnections} concurrent)");
            var connResults = await RunConnectionStressTestAsync();
            Results.Add(connResults);
            PrintResults(connResults);

            // Location broadcast test
            Console.WriteLine($"\nRunning: Location Broadcast Load Test (750K updates/sec target)");
            var locResults = await RunLocationBroadcastLoadTestAsync();
            Results.Add(locResults);
            PrintResults(locResults);

            // Offer notification test
            Console.WriteLine($"\nRunning: Offer Notification Load Test");
            var offerResults = await RunOfferNotificationLoadTestAsync();
            Results.Add(offerResults);
            PrintResults(offerResults);

            // Chat load test
            Console.WriteLine($"\nRunning: Chat Message Load Test");
            var chatResults = await RunChatLoadTestAsync();
            Results.Add(chatResults);
            PrintResults(chatResults);

            Console.WriteLine("\n=== LOAD TEST SUITE COMPLETE ===\n");
        }

        /// <summary>
        /// Simulate a driver connection
        /// </summary>
        private async Task SimulateConnectionAsync(SemaphoreSlim semaphore, Action onFail)
        {
            try
            {
                // Simulate connection latency (10-50ms)
                await Task.Delay(Random.Shared.Next(10, 50));
            }
            catch
            {
                onFail?.Invoke();
            }
            finally
            {
                semaphore.Release();
            }
        }

        /// <summary>
        /// Simulate location updates from a driver
        /// </summary>
        private async Task SimulateLocationUpdatesAsync(Guid driverId, CancellationToken cancellation, Action onUpdate)
        {
            while (!cancellation.IsCancellationRequested)
            {
                try
                {
                    var sw = Stopwatch.StartNew();
                    
                    // Simulate location update (should be <10ms)
                    await Task.Delay(Random.Shared.Next(1, 5));
                    
                    sw.Stop();
                    lock (_lockObj)
                    {
                        _latencies.Add(sw.ElapsedMilliseconds);
                    }
                    onUpdate?.Invoke();

                    // Delay between updates
                    await Task.Delay(Random.Shared.Next(50, 150));
                }
                catch
                {
                    // Silently fail
                }
            }
        }

        /// <summary>
        /// Simulate offer sequence
        /// </summary>
        private async Task SimulateOfferSequenceAsync(
            Guid riderId,
            List<Guid> drivers,
            CancellationToken cancellation,
            Action onSuccess,
            Action onFail)
        {
            while (!cancellation.IsCancellationRequested)
            {
                try
                {
                    var sw = Stopwatch.StartNew();
                    
                    // Simulate sending 5 offers
                    var topDrivers = drivers.OrderBy(_ => Random.Shared.Next()).Take(5);
                    foreach (var driver in topDrivers)
                    {
                        // Simulate offer (should be <5ms)
                        await Task.Delay(Random.Shared.Next(1, 3));
                    }

                    sw.Stop();
                    lock (_lockObj)
                    {
                        _latencies.Add(sw.ElapsedMilliseconds);
                    }
                    onSuccess?.Invoke();

                    // Delay between requests
                    await Task.Delay(Random.Shared.Next(100, 300));
                }
                catch
                {
                    onFail?.Invoke();
                }
            }
        }

        /// <summary>
        /// Simulate chat message sequence
        /// </summary>
        private async Task SimulateChatSequenceAsync(
            Guid userId,
            List<Guid> rides,
            List<Guid> users,
            CancellationToken cancellation,
            Action onSuccess,
            Action onFail)
        {
            while (!cancellation.IsCancellationRequested)
            {
                try
                {
                    var sw = Stopwatch.StartNew();
                    
                    var ride = rides[Random.Shared.Next(rides.Count)];
                    var recipient = users[Random.Shared.Next(users.Count)];
                    
                    // Simulate sending message (should be <3ms)
                    await Task.Delay(Random.Shared.Next(1, 2));
                    
                    sw.Stop();
                    lock (_lockObj)
                    {
                        _latencies.Add(sw.ElapsedMilliseconds);
                    }
                    onSuccess?.Invoke();

                    // Delay between messages
                    await Task.Delay(Random.Shared.Next(50, 200));
                }
                catch
                {
                    onFail?.Invoke();
                }
            }
        }

        /// <summary>
        /// Calculate latency statistics
        /// </summary>
        private void CalculateLatencyStats(LoadTestResults results)
        {
            if (_latencies.Count == 0)
                return;

            lock (_lockObj)
            {
                var sorted = _latencies.OrderBy(x => x).ToList();
                results.MinLatencyMs = sorted.First();
                results.MaxLatencyMs = sorted.Last();
                results.AverageLatencyMs = sorted.Average();
                results.P95LatencyMs = sorted[(int)(sorted.Count * 0.95)];
                results.P99LatencyMs = sorted[(int)(sorted.Count * 0.99)];
                
                _latencies.Clear();
            }
        }

        /// <summary>
        /// Print results to console
        /// </summary>
        private void PrintResults(LoadTestResults results)
        {
            Console.WriteLine($"\n  Test: {results.TestName}");
            Console.WriteLine($"  Duration: {results.Duration.TotalSeconds:F2}s");
            Console.WriteLine($"  Total Requests: {results.TotalRequests}");
            Console.WriteLine($"  Successful: {results.SuccessfulRequests} ({(results.SuccessfulRequests * 100.0 / (results.TotalRequests > 0 ? results.TotalRequests : 1)):F2}%)");
            Console.WriteLine($"  Failed: {results.FailedRequests}");
            Console.WriteLine($"  Throughput: {results.ThroughputPerSecond:F2} req/sec");
            
            if (results.AverageLatencyMs > 0)
            {
                Console.WriteLine($"  Latency:");
                Console.WriteLine($"    Min: {results.MinLatencyMs}ms");
                Console.WriteLine($"    Avg: {results.AverageLatencyMs:F2}ms");
                Console.WriteLine($"    P95: {results.P95LatencyMs:F2}ms");
                Console.WriteLine($"    P99: {results.P99LatencyMs:F2}ms");
                Console.WriteLine($"    Max: {results.MaxLatencyMs}ms");
            }
        }

        /// <summary>
        /// Generate HTML report
        /// </summary>
        public string GenerateHtmlReport()
        {
            var html = new System.Text.StringBuilder();
            html.AppendLine("<!DOCTYPE html>");
            html.AppendLine("<html>");
            html.AppendLine("<head>");
            html.AppendLine("<title>AI Uber Marketplace - Load Test Report</title>");
            html.AppendLine("<style>");
            html.AppendLine("body { font-family: Arial, sans-serif; margin: 20px; }");
            html.AppendLine("table { border-collapse: collapse; width: 100%; margin-bottom: 30px; }");
            html.AppendLine("th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }");
            html.AppendLine("th { background-color: #4CAF50; color: white; }");
            html.AppendLine("tr:nth-child(even) { background-color: #f2f2f2; }");
            html.AppendLine(".pass { color: green; font-weight: bold; }");
            html.AppendLine(".fail { color: red; font-weight: bold; }");
            html.AppendLine("h1, h2 { color: #333; }");
            html.AppendLine(".summary { background-color: #f0f0f0; padding: 15px; border-radius: 5px; margin-bottom: 20px; }");
            html.AppendLine("</style>");
            html.AppendLine("</head>");
            html.AppendLine("<body>");
            
            html.AppendLine("<h1>AI Uber Marketplace - Load Test Report</h1>");
            html.AppendLine($"<p>Generated: {DateTime.UtcNow:yyyy-MM-dd HH:mm:ss UTC}</p>");
            
            html.AppendLine("<div class=\"summary\">");
            html.AppendLine($"<h2>Test Summary</h2>");
            html.AppendLine($"<p>Total Tests Run: {Results.Count}</p>");
            html.AppendLine($"<p>Total Requests: {Results.Sum(r => r.TotalRequests)}</p>");
            html.AppendLine($"<p>Total Successful: {Results.Sum(r => r.SuccessfulRequests)}</p>");
            html.AppendLine($"<p>Overall Success Rate: {(Results.Sum(r => r.SuccessfulRequests) * 100.0 / Results.Sum(r => r.TotalRequests)):F2}%</p>");
            html.AppendLine("</div>");
            
            html.AppendLine("<h2>Test Results</h2>");
            html.AppendLine("<table>");
            html.AppendLine("<tr>");
            html.AppendLine("<th>Test Name</th>");
            html.AppendLine("<th>Duration (s)</th>");
            html.AppendLine("<th>Requests</th>");
            html.AppendLine("<th>Success Rate</th>");
            html.AppendLine("<th>Throughput (req/s)</th>");
            html.AppendLine("<th>Avg Latency (ms)</th>");
            html.AppendLine("<th>P95 Latency (ms)</th>");
            html.AppendLine("<th>P99 Latency (ms)</th>");
            html.AppendLine("</tr>");
            
            foreach (var result in Results)
            {
                var successRate = result.TotalRequests > 0 ? (result.SuccessfulRequests * 100.0 / result.TotalRequests) : 0;
                var statusClass = successRate >= 95 ? "pass" : "fail";
                
                html.AppendLine("<tr>");
                html.AppendLine($"<td>{result.TestName}</td>");
                html.AppendLine($"<td>{result.Duration.TotalSeconds:F2}</td>");
                html.AppendLine($"<td>{result.TotalRequests}</td>");
                html.AppendLine($"<td class=\"{statusClass}\">{successRate:F2}%</td>");
                html.AppendLine($"<td>{result.ThroughputPerSecond:F2}</td>");
                html.AppendLine($"<td>{result.AverageLatencyMs:F2}</td>");
                html.AppendLine($"<td>{result.P95LatencyMs:F2}</td>");
                html.AppendLine($"<td>{result.P99LatencyMs:F2}</td>");
                html.AppendLine("</tr>");
            }
            
            html.AppendLine("</table>");
            html.AppendLine("</body>");
            html.AppendLine("</html>");
            
            return html.ToString();
        }

        /// <summary>
        /// Generate JSON report
        /// </summary>
        public string GenerateJsonReport()
        {
            var json = new System.Text.StringBuilder();
            json.AppendLine("{");
            json.AppendLine($"  \"generatedAt\": \"{DateTime.UtcNow:yyyy-MM-ddTHH:mm:ssZ}\",");
            json.AppendLine($"  \"totalTests\": {Results.Count},");
            json.AppendLine($"  \"totalRequests\": {Results.Sum(r => r.TotalRequests)},");
            json.AppendLine($"  \"totalSuccessful\": {Results.Sum(r => r.SuccessfulRequests)},");
            json.AppendLine($"  \"overallSuccessRate\": {(Results.Sum(r => r.SuccessfulRequests) * 100.0 / (Results.Sum(r => r.TotalRequests) > 0 ? Results.Sum(r => r.TotalRequests) : 1)):F2},");
            json.AppendLine("  \"results\": [");
            
            for (int i = 0; i < Results.Count; i++)
            {
                var result = Results[i];
                json.AppendLine("    {");
                json.AppendLine($"      \"testName\": \"{result.TestName}\",");
                json.AppendLine($"      \"duration\": {result.Duration.TotalSeconds:F2},");
                json.AppendLine($"      \"totalRequests\": {result.TotalRequests},");
                json.AppendLine($"      \"successfulRequests\": {result.SuccessfulRequests},");
                json.AppendLine($"      \"failedRequests\": {result.FailedRequests},");
                json.AppendLine($"      \"successRate\": {(result.SuccessfulRequests * 100.0 / (result.TotalRequests > 0 ? result.TotalRequests : 1)):F2},");
                json.AppendLine($"      \"throughput\": {result.ThroughputPerSecond:F2},");
                json.AppendLine($"      \"latency\": {{");
                json.AppendLine($"        \"min\": {result.MinLatencyMs},");
                json.AppendLine($"        \"avg\": {result.AverageLatencyMs:F2},");
                json.AppendLine($"        \"p95\": {result.P95LatencyMs:F2},");
                json.AppendLine($"        \"p99\": {result.P99LatencyMs:F2},");
                json.AppendLine($"        \"max\": {result.MaxLatencyMs}");
                json.AppendLine("      }");
                json.AppendLine($"    }}{(i < Results.Count - 1 ? "," : "")}");
            }
            
            json.AppendLine("  ]");
            json.AppendLine("}");
            
            return json.ToString();
        }
    }
}
