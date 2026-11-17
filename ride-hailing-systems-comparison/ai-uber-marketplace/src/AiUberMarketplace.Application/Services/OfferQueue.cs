using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Queue item for an offer
    /// </summary>
    public class QueuedOffer
    {
        public Guid OfferId { get; set; }
        public Guid RideRequestId { get; set; }
        public Guid DriverId { get; set; }
        public string DriverName { get; set; }
        public decimal EstimatedEarnings { get; set; }
        public decimal DistanceKm { get; set; }
        public string PickupAddress { get; set; }
        public string DropoffAddress { get; set; }
        public int Priority { get; set; } // Distance-based priority (lower = higher priority)
        public DateTime QueuedAt { get; set; }
    }

    /// <summary>
    /// Offer queue management service.
    /// Maintains FIFO queue with priority handling based on distance.
    /// </summary>
    public interface IOfferQueue
    {
        /// <summary>
        /// Enqueue an offer
        /// </summary>
        Task EnqueueOfferAsync(QueuedOffer offer);

        /// <summary>
        /// Dequeue the next offer(s)
        /// </summary>
        Task<List<QueuedOffer>> DequeueOffersAsync(int count = 1);

        /// <summary>
        /// Peek at the next offer without removing it
        /// </summary>
        Task<QueuedOffer> PeekOfferAsync();

        /// <summary>
        /// Get current queue length
        /// </summary>
        Task<int> GetQueueLengthAsync();

        /// <summary>
        /// Clear all offers from queue
        /// </summary>
        Task ClearAsync();

        /// <summary>
        /// Get queue statistics
        /// </summary>
        OfferQueueStats GetStats();
    }

    /// <summary>
    /// Statistics for offer queue
    /// </summary>
    public class OfferQueueStats
    {
        public int QueueLength { get; set; }
        public int TotalEnqueued { get; set; }
        public int TotalDequeued { get; set; }
        public double AverageProcessingTimeMs { get; set; }
        public DateTime LastProcessedTime { get; set; }
    }

    /// <summary>
    /// Implementation of offer queue using ConcurrentBag for high throughput
    /// </summary>
    public class OfferQueue : IOfferQueue, IDisposable
    {
        // Use ConcurrentBag for lock-free enqueue/dequeue
        private readonly ConcurrentBag<QueuedOffer> _queue = new();

        // Track offers by ID for removal if needed
        private readonly ConcurrentDictionary<Guid, QueuedOffer> _offerMap = new();

        // Statistics
        private readonly OfferQueueStats _stats = new();
        private int _totalEnqueued = 0;
        private int _totalDequeued = 0;

        /// <summary>
        /// Enqueue an offer
        /// </summary>
        public async Task EnqueueOfferAsync(QueuedOffer offer)
        {
            try
            {
                if (offer == null)
                    return;

                offer.QueuedAt = DateTime.UtcNow;
                offer.Priority = (int)(offer.DistanceKm * 10); // Distance-based priority

                _queue.Add(offer);
                _offerMap.TryAdd(offer.OfferId, offer);

                _totalEnqueued++;
                _stats.QueueLength = GetCurrentQueueLength();
                _stats.TotalEnqueued = _totalEnqueued;

                await Task.CompletedTask;
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Dequeue the next offer(s) in priority order (distance-based)
        /// </summary>
        public async Task<List<QueuedOffer>> DequeueOffersAsync(int count = 1)
        {
            var result = new List<QueuedOffer>();

            try
            {
                var startTime = DateTime.UtcNow;

                // Convert bag to list, sort by priority, and take top N
                var allOffers = new List<QueuedOffer>();
                while (_queue.TryTake(out var offer))
                {
                    allOffers.Add(offer);
                }

                // Sort by priority (distance) ascending
                var prioritized = allOffers
                    .OrderBy(o => o.Priority)
                    .Take(count)
                    .ToList();

                // Put back non-selected offers
                var remaining = allOffers.Except(prioritized).ToList();
                foreach (var offer in remaining)
                {
                    _queue.Add(offer);
                }

                // Remove from map
                foreach (var offer in prioritized)
                {
                    _offerMap.TryRemove(offer.OfferId, out _);
                    result.Add(offer);
                    _totalDequeued++;
                }

                // Update stats
                _stats.QueueLength = GetCurrentQueueLength();
                _stats.TotalDequeued = _totalDequeued;
                _stats.LastProcessedTime = DateTime.UtcNow;

                var processingTime = DateTime.UtcNow - startTime;
                _stats.AverageProcessingTimeMs = 
                    (_stats.AverageProcessingTimeMs * (_totalDequeued - 1) + processingTime.TotalMilliseconds) / _totalDequeued;

                await Task.CompletedTask;
            }
            catch (Exception)
            {
                // Silent fail
            }

            return result;
        }

        /// <summary>
        /// Peek at the next offer without removing it
        /// </summary>
        public async Task<QueuedOffer> PeekOfferAsync()
        {
            try
            {
                // Get all offers temporarily
                var all = new List<QueuedOffer>();
                while (_queue.TryTake(out var offer))
                {
                    all.Add(offer);
                }

                // Put them all back
                foreach (var offer in all)
                {
                    _queue.Add(offer);
                }

                // Return the one with highest priority (lowest distance)
                var next = all.OrderBy(o => o.Priority).FirstOrDefault();
                await Task.CompletedTask;
                return next;
            }
            catch (Exception)
            {
                await Task.CompletedTask;
                return null;
            }
        }

        /// <summary>
        /// Get current queue length
        /// </summary>
        public async Task<int> GetQueueLengthAsync()
        {
            var length = GetCurrentQueueLength();
            await Task.CompletedTask;
            return length;
        }

        /// <summary>
        /// Clear all offers
        /// </summary>
        public async Task ClearAsync()
        {
            try
            {
                while (_queue.TryTake(out _))
                {
                    // Keep taking until empty
                }

                _offerMap.Clear();
                _stats.QueueLength = 0;

                await Task.CompletedTask;
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get current statistics
        /// </summary>
        public OfferQueueStats GetStats()
        {
            _stats.QueueLength = GetCurrentQueueLength();
            return new OfferQueueStats
            {
                QueueLength = _stats.QueueLength,
                TotalEnqueued = _stats.TotalEnqueued,
                TotalDequeued = _stats.TotalDequeued,
                AverageProcessingTimeMs = _stats.AverageProcessingTimeMs,
                LastProcessedTime = _stats.LastProcessedTime
            };
        }

        /// <summary>
        /// Get current queue length without async
        /// </summary>
        private int GetCurrentQueueLength()
        {
            return _queue.Count;
        }

        public void Dispose()
        {
            // ConcurrentBag doesn't need disposal, just clear it
            _offerMap.Clear();
        }
    }
}
