using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using AiUberMarketplace.Domain;
using AiUberMarketplace.Domain.Services;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Callback interface for location broadcast notifications
    /// Implemented by SignalR hubs or other notification systems
    /// </summary>
    public interface ILocationBroadcastNotifier
    {
        /// <summary>
        /// Broadcast location update to connected riders
        /// </summary>
        Task NotifyDriverLocationUpdatedAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading,
            decimal? speed,
            DateTime timestamp,
            CancellationToken cancellationToken = default);
    }

    /// <summary>
    /// Location broadcast service for real-time driver location updates to riders.
    /// Batches location updates at 2-4 second intervals to optimize network usage.
    /// Target latency: <100ms from driver update to rider notification.
    /// </summary>
    public interface ILocationBroadcastService
    {
        /// <summary>
        /// Start the broadcast service and begin processing location updates
        /// </summary>
        Task StartBroadcastAsync(CancellationToken cancellationToken = default);

        /// <summary>
        /// Stop the broadcast service
        /// </summary>
        Task StopBroadcastAsync();

        /// <summary>
        /// Publish a location update from a driver
        /// Queued for batching and broadcast to all connected riders
        /// </summary>
        Task PublishLocationUpdateAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading = null,
            decimal? speed = null);

        /// <summary>
        /// Get batch statistics for monitoring
        /// </summary>
        BroadcastStats GetStats();
    }

    /// <summary>
    /// Statistics for monitoring broadcast performance
    /// </summary>
    public class BroadcastStats
    {
        public int QueuedUpdates { get; set; }
        public int ProcessedBatches { get; set; }
        public int BroadcastedUpdates { get; set; }
        public TimeSpan AverageLatency { get; set; }
        public int ActiveConnections { get; set; }
        public DateTime LastProcessedTime { get; set; }
    }

    /// <summary>
    /// Location update queued for processing
    /// </summary>
    internal class QueuedLocationUpdate
    {
        public Guid DriverId { get; set; }
        public decimal Latitude { get; set; }
        public decimal Longitude { get; set; }
        public decimal? Heading { get; set; }
        public decimal? Speed { get; set; }
        public DateTime QueuedAt { get; set; }
    }

    /// <summary>
    /// Implementation of location broadcast service
    /// </summary>
    public class LocationBroadcastService : ILocationBroadcastService, IDisposable
    {
        private readonly ILocationService _locationService;
        private readonly ILocationCache _locationCache;
        private readonly ILocationBroadcastNotifier _notifier;

        // Queue for batching location updates
        private readonly ConcurrentQueue<QueuedLocationUpdate> _locationQueue = new();
        private CancellationTokenSource _cancellationTokenSource;
        private Task _broadcastTask;

        // Statistics
        private readonly BroadcastStats _stats = new();
        private int _totalUpdatesProcessed = 0;

        // Broadcast interval configuration (2-4 seconds)
        private const int BatchIntervalMs = 3000; // 3 seconds as middle ground
        private const int MaxBatchSize = 10000; // Max updates per batch

        public LocationBroadcastService(
            ILocationService locationService,
            ILocationCache locationCache,
            ILocationBroadcastNotifier notifier)
        {
            _locationService = locationService;
            _locationCache = locationCache;
            _notifier = notifier;
        }

        /// <summary>
        /// Start the broadcast service background task
        /// </summary>
        public async Task StartBroadcastAsync(CancellationToken cancellationToken = default)
        {
            if (_broadcastTask is not null && !_broadcastTask.IsCompleted)
            {
                return;
            }

            _cancellationTokenSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _stats.LastProcessedTime = DateTime.UtcNow;

            _broadcastTask = BroadcastLoopAsync(_cancellationTokenSource.Token);
            await Task.Yield(); // Let it start
        }

        /// <summary>
        /// Stop the broadcast service
        /// </summary>
        public async Task StopBroadcastAsync()
        {
            if (_broadcastTask is null || _broadcastTask.IsCompleted)
            {
                return;
            }

            _cancellationTokenSource?.Cancel();
            try
            {
                await _broadcastTask;
            }
            catch (OperationCanceledException)
            {
                // Normal shutdown
            }
        }

        /// <summary>
        /// Publish a location update (add to queue for batching)
        /// </summary>
        public async Task PublishLocationUpdateAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading = null,
            decimal? speed = null)
        {
            try
            {
                var update = new QueuedLocationUpdate
                {
                    DriverId = driverId,
                    Latitude = latitude,
                    Longitude = longitude,
                    Heading = heading,
                    Speed = speed,
                    QueuedAt = DateTime.UtcNow
                };

                _locationQueue.Enqueue(update);
                _stats.QueuedUpdates = _locationQueue.Count;

                // Also update the location service for other queries
                await _locationService.UpdateDriverLocationAsync(
                    driverId, latitude, longitude, heading, speed);
            }
            catch (Exception ex)
            {
                // Silent fail - would log with logger if available
            }
        }

        /// <summary>
        /// Get current broadcast statistics
        /// </summary>
        public BroadcastStats GetStats()
        {
            return new BroadcastStats
            {
                QueuedUpdates = _locationQueue.Count,
                ProcessedBatches = _stats.ProcessedBatches,
                BroadcastedUpdates = _stats.BroadcastedUpdates,
                AverageLatency = _stats.AverageLatency,
                ActiveConnections = _stats.ActiveConnections,
                LastProcessedTime = _stats.LastProcessedTime
            };
        }

        /// <summary>
        /// Main broadcast loop - processes batches at regular intervals
        /// </summary>
        private async Task BroadcastLoopAsync(CancellationToken cancellationToken)
        {
            using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(BatchIntervalMs));

            try
            {
                while (await timer.WaitForNextTickAsync(cancellationToken))
                {
                    await ProcessBatchAsync(cancellationToken);
                }
            }
            catch (OperationCanceledException)
            {
                // Normal shutdown
            }
            finally
            {
                timer.Dispose();
            }
        }

        /// <summary>
        /// Process a batch of queued location updates
        /// </summary>
        private async Task ProcessBatchAsync(CancellationToken cancellationToken)
        {
            var batchStartTime = DateTime.UtcNow;
            var batch = new List<QueuedLocationUpdate>();

            // Dequeue up to MaxBatchSize items
            while (batch.Count < MaxBatchSize && _locationQueue.TryDequeue(out var update))
            {
                batch.Add(update);
            }

            if (batch.Count == 0)
            {
                return; // No updates in this batch
            }

            try
            {
                // Group by driver for efficient broadcasting
                var groupedByDriver = batch.GroupBy(u => u.DriverId);

                foreach (var driverGroup in groupedByDriver)
                {
                    var latestUpdate = driverGroup.OrderByDescending(u => u.QueuedAt).First();
                    var latency = DateTime.UtcNow - latestUpdate.QueuedAt;

                    // Update location cache with latest position
                    await _locationCache.SetLocationAsync(
                        latestUpdate.DriverId,
                        latestUpdate.Latitude,
                        latestUpdate.Longitude,
                        latestUpdate.Heading,
                        latestUpdate.Speed);

                    // Broadcast to all connected riders via notifier
                    if (_notifier != null)
                    {
                        await _notifier.NotifyDriverLocationUpdatedAsync(
                            latestUpdate.DriverId,
                            latestUpdate.Latitude,
                            latestUpdate.Longitude,
                            latestUpdate.Heading,
                            latestUpdate.Speed,
                            DateTime.UtcNow,
                            cancellationToken);
                    }

                    // Update stats
                    _stats.BroadcastedUpdates += batch.Count;
                    _stats.AverageLatency = TimeSpan.FromMilliseconds(
                        (_stats.AverageLatency.TotalMilliseconds * _totalUpdatesProcessed + latency.TotalMilliseconds) /
                        (_totalUpdatesProcessed + 1));
                    _totalUpdatesProcessed++;
                }

                _stats.ProcessedBatches++;
                _stats.QueuedUpdates = _locationQueue.Count;
                _stats.LastProcessedTime = DateTime.UtcNow;
            }
            catch (Exception ex)
            {
                // Silent fail - would log with logger if available
            }
        }

        public void Dispose()
        {
            _cancellationTokenSource?.Dispose();
            _broadcastTask?.Dispose();
        }
    }
}
