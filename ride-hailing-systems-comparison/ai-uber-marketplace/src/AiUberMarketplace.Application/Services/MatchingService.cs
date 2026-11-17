using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using AiUberMarketplace.Domain;
using AiUberMarketplace.Domain.Services;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Enhanced matching service with geospatial driver selection
    /// Uses Redis GEO for <50ms latency queries
    /// </summary>
    public interface IMatchingService
    {
        /// <summary>
        /// Find nearby available drivers for a ride request
        /// Uses geospatial search instead of full list iteration
        /// </summary>
        Task<List<(Guid DriverId, DriverLocation Location)>> FindNearbyDriversAsync(
            Location pickupLocation,
            decimal radiusKm = 5m,
            int maxDrivers = 10);

        /// <summary>
        /// Rank drivers by multiple criteria
        /// </summary>
        Task<List<(Guid DriverId, decimal Score)>> RankDriversAsync(
            List<(Guid DriverId, DriverLocation Location)> drivers,
            Location rideLocation);

        /// <summary>
        /// Create offers for multiple drivers
        /// </summary>
        Task CreateOffersAsync(
            Guid rideRequestId,
            List<Guid> driverIds);
    }

    /// <summary>
    /// Implementation of matching service
    /// </summary>
    public class MatchingService : IMatchingService
    {
        private readonly ILocationService _locationService;
        private readonly IDriverRepository _driverRepository;
        private readonly IRideRequestRepository _rideRequestRepository;
        private readonly IDriverOfferRepository _driverOfferRepository;

        public MatchingService(
            ILocationService locationService,
            IDriverRepository driverRepository,
            IRideRequestRepository rideRequestRepository,
            IDriverOfferRepository driverOfferRepository)
        {
            _locationService = locationService ?? throw new ArgumentNullException(nameof(locationService));
            _driverRepository = driverRepository ?? throw new ArgumentNullException(nameof(driverRepository));
            _rideRequestRepository = rideRequestRepository ?? throw new ArgumentNullException(nameof(rideRequestRepository));
            _driverOfferRepository = driverOfferRepository ?? throw new ArgumentNullException(nameof(driverOfferRepository));
        }

        /// <summary>
        /// Find nearby drivers using Redis GEORADIUS
        /// O(N+log(M)) complexity; should complete in <50ms for typical density
        /// </summary>
        public async Task<List<(Guid DriverId, DriverLocation Location)>> FindNearbyDriversAsync(
            Location pickupLocation,
            decimal radiusKm = 5m,
            int maxDrivers = 10)
        {
            // Query Redis GEO for nearby drivers
            var nearbyLocations = await _locationService.FindNearbyDriversAsync(
                pickupLocation.Latitude,
                pickupLocation.Longitude,
                radiusKm,
                maxDrivers);

            if (!nearbyLocations.Any())
                return new List<(Guid, DriverLocation)>();

            var result = new List<(Guid, DriverLocation)>();

            foreach (var location in nearbyLocations)
            {
                // Verify driver is still available (not on trip, online)
                var isOnline = await _locationService.IsDriverOnlineAsync(location.DriverId);
                if (!isOnline)
                    continue;

                // Check driver status from repository
                var driver = await _driverRepository.GetByIdAsync(location.DriverId);
                if (driver == null || !driver.IsAvailable)
                    continue;

                result.Add((location.DriverId, location));
            }

            return result;
        }

        /// <summary>
        /// Rank drivers by distance (main criteria)
        /// Secondary sort by rating if available
        /// </summary>
        public async Task<List<(Guid DriverId, decimal Score)>> RankDriversAsync(
            List<(Guid DriverId, DriverLocation Location)> drivers,
            Location rideLocation)
        {
            if (!drivers.Any())
                return new List<(Guid, decimal)>();

            var rankedDrivers = new List<(Guid DriverId, decimal Score)>();

            foreach (var (driverId, location) in drivers)
            {
                var driver = await _driverRepository.GetByIdAsync(driverId);
                if (driver == null)
                    continue;

                // Score based on distance (0-1, higher is better)
                // Closer drivers get higher score
                var distanceScore = CalculateDistanceScore(location.Distance);
                
                // Secondary score based on average rating if available
                var ratingScore = CalculateRatingScore(driver.AverageRating);

                // Weighted score: distance is primary (70%), rating is secondary (30%)
                var totalScore = (distanceScore * 0.7m) + (ratingScore * 0.3m);

                rankedDrivers.Add((driverId, totalScore));
            }

            return rankedDrivers
                .OrderByDescending(x => x.Score)
                .ToList();
        }

        /// <summary>
        /// Create driver offers for a ride request
        /// </summary>
        public async Task CreateOffersAsync(
            Guid rideRequestId,
            List<Guid> driverIds)
        {
            var rideRequest = await _rideRequestRepository.GetByIdAsync(rideRequestId);
            if (rideRequest == null)
                throw new InvalidOperationException($"Ride request {rideRequestId} not found");

            // Get drivers and create offers
            foreach (var driverId in driverIds.Take(5)) // Limit to top 5
            {
                var driver = await _driverRepository.GetByIdAsync(driverId);
                if (driver == null)
                    continue;

                var offer = new DriverOffer(
                    rideRequestId,
                    driverId,
                    TimeSpan.FromSeconds(30) // 30-second expiration
                );

                await _driverOfferRepository.AddAsync(offer);
            }
        }

        // ===== PRIVATE SCORING HELPERS =====

        /// <summary>
        /// Score based on distance (0-1, higher is better)
        /// </summary>
        private decimal CalculateDistanceScore(decimal distanceKm)
        {
            // Drivers within 1km get score 1.0
            // Drivers at 5km get score 0.5
            // Drivers > 5km get score 0.2
            if (distanceKm <= 1)
                return 1.0m;
            if (distanceKm <= 5)
                return 1.0m - ((distanceKm - 1) / 4 * 0.5m);
            return 0.2m;
        }

        private decimal CalculateRatingScore(decimal rating)
        {
            // 5.0 = 1.0, 3.0 = 0.6, 2.0 = 0.4, 0.0 = 0.0
            if (rating >= 5.0m)
                return 1.0m;
            return rating / 5.0m;
        }
    }
}
