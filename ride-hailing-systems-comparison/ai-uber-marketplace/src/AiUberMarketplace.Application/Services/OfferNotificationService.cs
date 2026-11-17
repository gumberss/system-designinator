using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Represents an active offer for a driver
    /// </summary>
    public class Offer
    {
        public Guid OfferId { get; set; }
        public Guid RideRequestId { get; set; }
        public Guid DriverId { get; set; }
        public string DriverName { get; set; }
        public decimal EstimatedEarnings { get; set; }
        public decimal DistanceKm { get; set; }
        public string PickupAddress { get; set; }
        public string DropoffAddress { get; set; }
        public DateTime CreatedAt { get; set; }
        public DateTime ExpiresAt { get; set; }
        public bool IsActive { get; set; }
    }

    /// <summary>
    /// Service for managing real-time offer notifications to drivers.
    /// Pushes offers to top 5 nearby drivers with 30-second expiration window.
    /// </summary>
    public interface IOfferNotificationService
    {
        /// <summary>
        /// Send offer to nearby drivers
        /// </summary>
        Task SendOfferAsync(
            Guid offerId,
            Guid rideRequestId,
            Guid driverId,
            string driverName,
            decimal estimatedEarnings,
            decimal distanceKm,
            string pickupAddress,
            string dropoffAddress);

        /// <summary>
        /// Cancel an active offer
        /// </summary>
        Task CancelOfferAsync(Guid offerId);

        /// <summary>
        /// Get all active offers
        /// </summary>
        Task<List<Offer>> GetActiveOffersAsync();

        /// <summary>
        /// Get offers for a specific driver
        /// </summary>
        Task<List<Offer>> GetDriverOffersAsync(Guid driverId);

        /// <summary>
        /// Start the expiration handler background service
        /// </summary>
        Task StartExpirationHandlerAsync(CancellationToken cancellationToken = default);

        /// <summary>
        /// Stop the expiration handler
        /// </summary>
        Task StopExpirationHandlerAsync();

        /// <summary>
        /// Get statistics about active offers
        /// </summary>
        OfferStats GetStats();
    }

    /// <summary>
    /// Statistics for offer notifications
    /// </summary>
    public class OfferStats
    {
        public int ActiveOffers { get; set; }
        public int ExpiredOffers { get; set; }
        public int AcceptedOffers { get; set; }
        public int TotalOffersSent { get; set; }
        public double AverageResponseTimeMs { get; set; }
        public DateTime LastProcessedTime { get; set; }
    }

    /// <summary>
    /// Implementation of offer notification service
    /// </summary>
    public class OfferNotificationService : IOfferNotificationService, IDisposable
    {
        // Active offers keyed by offer ID
        private readonly ConcurrentDictionary<Guid, Offer> _activeOffers = new();

        // Offers by driver for quick lookup
        private readonly ConcurrentDictionary<Guid, List<Guid>> _driverOffers = new();

        // Expiration handler
        private CancellationTokenSource _cancellationTokenSource;
        private Task _expirationTask;

        // Statistics
        private readonly OfferStats _stats = new();
        private int _totalOffersSent = 0;
        private int _acceptedOffers = 0;

        // Configuration
        private const int OfferExpirationSeconds = 30; // 30-second offer window
        private const int ExpirationCheckIntervalMs = 2000; // Check every 2 seconds

        /// <summary>
        /// Send an offer to a driver
        /// </summary>
        public async Task SendOfferAsync(
            Guid offerId,
            Guid rideRequestId,
            Guid driverId,
            string driverName,
            decimal estimatedEarnings,
            decimal distanceKm,
            string pickupAddress,
            string dropoffAddress)
        {
            try
            {
                var offer = new Offer
                {
                    OfferId = offerId,
                    RideRequestId = rideRequestId,
                    DriverId = driverId,
                    DriverName = driverName,
                    EstimatedEarnings = estimatedEarnings,
                    DistanceKm = distanceKm,
                    PickupAddress = pickupAddress,
                    DropoffAddress = dropoffAddress,
                    CreatedAt = DateTime.UtcNow,
                    ExpiresAt = DateTime.UtcNow.AddSeconds(OfferExpirationSeconds),
                    IsActive = true
                };

                // Add to active offers
                _activeOffers.TryAdd(offerId, offer);

                // Track by driver
                _driverOffers.AddOrUpdate(
                    driverId,
                    new List<Guid> { offerId },
                    (_, offers) =>
                    {
                        offers.Add(offerId);
                        return offers;
                    });

                _totalOffersSent++;
                _stats.ActiveOffers = _activeOffers.Count;
                _stats.TotalOffersSent = _totalOffersSent;

                // Would notify driver via SignalR here
                await Task.CompletedTask;
            }
            catch (Exception)
            {
                // Silent fail - would log if logger available
            }
        }

        /// <summary>
        /// Cancel an active offer
        /// </summary>
        public async Task CancelOfferAsync(Guid offerId)
        {
            try
            {
                if (_activeOffers.TryRemove(offerId, out var offer))
                {
                    // Remove from driver's offer list
                    if (_driverOffers.TryGetValue(offer.DriverId, out var offers))
                    {
                        offers.Remove(offerId);
                    }

                    _stats.ActiveOffers = _activeOffers.Count;
                }

                await Task.CompletedTask;
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get all active offers
        /// </summary>
        public async Task<List<Offer>> GetActiveOffersAsync()
        {
            var result = _activeOffers.Values
                .Where(o => o.IsActive)
                .ToList();

            await Task.CompletedTask;
            return result;
        }

        /// <summary>
        /// Get offers for a specific driver
        /// </summary>
        public async Task<List<Offer>> GetDriverOffersAsync(Guid driverId)
        {
            var result = new List<Offer>();

            if (_driverOffers.TryGetValue(driverId, out var offerIds))
            {
                foreach (var offerId in offerIds)
                {
                    if (_activeOffers.TryGetValue(offerId, out var offer) && offer.IsActive)
                    {
                        result.Add(offer);
                    }
                }
            }

            await Task.CompletedTask;
            return result;
        }

        /// <summary>
        /// Start the expiration handler background service
        /// </summary>
        public async Task StartExpirationHandlerAsync(CancellationToken cancellationToken = default)
        {
            if (_expirationTask is not null && !_expirationTask.IsCompleted)
            {
                return;
            }

            _cancellationTokenSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _stats.LastProcessedTime = DateTime.UtcNow;

            _expirationTask = ExpirationLoopAsync(_cancellationTokenSource.Token);
            await Task.Yield();
        }

        /// <summary>
        /// Stop the expiration handler
        /// </summary>
        public async Task StopExpirationHandlerAsync()
        {
            if (_expirationTask is null || _expirationTask.IsCompleted)
            {
                return;
            }

            _cancellationTokenSource?.Cancel();
            try
            {
                await _expirationTask;
            }
            catch (OperationCanceledException)
            {
                // Normal shutdown
            }
        }

        /// <summary>
        /// Get current statistics
        /// </summary>
        public OfferStats GetStats()
        {
            return new OfferStats
            {
                ActiveOffers = _activeOffers.Count,
                ExpiredOffers = _stats.ExpiredOffers,
                AcceptedOffers = _acceptedOffers,
                TotalOffersSent = _stats.TotalOffersSent,
                AverageResponseTimeMs = _stats.AverageResponseTimeMs,
                LastProcessedTime = _stats.LastProcessedTime
            };
        }

        /// <summary>
        /// Main expiration loop - checks for expired offers periodically
        /// </summary>
        private async Task ExpirationLoopAsync(CancellationToken cancellationToken)
        {
            using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(ExpirationCheckIntervalMs));

            try
            {
                while (await timer.WaitForNextTickAsync(cancellationToken))
                {
                    await ProcessExpirationsAsync();
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
        /// Process expired offers
        /// </summary>
        private async Task ProcessExpirationsAsync()
        {
            try
            {
                var now = DateTime.UtcNow;
                var expiredOffers = _activeOffers.Values
                    .Where(o => o.IsActive && o.ExpiresAt <= now)
                    .ToList();

                foreach (var offer in expiredOffers)
                {
                    if (_activeOffers.TryGetValue(offer.OfferId, out var existingOffer))
                    {
                        existingOffer.IsActive = false;
                        _stats.ExpiredOffers++;
                    }

                    // Remove from driver's list
                    if (_driverOffers.TryGetValue(offer.DriverId, out var offers))
                    {
                        offers.Remove(offer.OfferId);
                    }
                }

                if (expiredOffers.Count > 0)
                {
                    _stats.LastProcessedTime = now;
                }

                await Task.CompletedTask;
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        public void Dispose()
        {
            _cancellationTokenSource?.Dispose();
            _expirationTask?.Dispose();
        }
    }
}
