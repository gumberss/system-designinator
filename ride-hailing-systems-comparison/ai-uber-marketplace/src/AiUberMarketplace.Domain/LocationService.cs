using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using AiUberMarketplace.Domain;

namespace AiUberMarketplace.Domain.Services
{
    // ===== VALUE OBJECTS =====

    /// <summary>
    /// Represents a driver's current location with metadata
    /// </summary>
    public class DriverLocation
    {
        public Guid DriverId { get; set; }
        public decimal Latitude { get; set; }
        public decimal Longitude { get; set; }
        public decimal? Heading { get; set; } // 0-359 degrees
        public decimal? Speed { get; set; } // km/h
        public decimal? Accuracy { get; set; } // meters
        public DateTime UpdatedAt { get; set; }
        public Guid? TripId { get; set; }
        public decimal Distance { get; set; } // Distance from query point
    }

    /// <summary>
    /// Location search criteria
    /// </summary>
    public class LocationSearchCriteria
    {
        public decimal CenterLatitude { get; set; }
        public decimal CenterLongitude { get; set; }
        public decimal RadiusKm { get; set; } = 5m;
        public int MaxResults { get; set; } = 10;
        public bool OnlyAvailable { get; set; } = true;
    }

    /// <summary>
    /// Domain event: Driver location updated
    /// </summary>
    public class DriverLocationUpdatedEvent : DomainEvent
    {
        public Guid DriverId { get; set; }
        public decimal Latitude { get; set; }
        public decimal Longitude { get; set; }
        public decimal? Heading { get; set; }
        public decimal? Speed { get; set; }
        public Guid? TripId { get; set; }
        public int ActiveDriversNearby { get; set; }
    }

    /// <summary>
    /// Domain event: Driver went offline
    /// </summary>
    public class DriverWentOfflineEvent : DomainEvent
    {
        public Guid DriverId { get; set; }
        public DateTime LastLocationTime { get; set; }
    }

    /// <summary>
    /// Domain event: Nearby drivers found for ride
    /// </summary>
    public class NearbyDriversFoundEvent : DomainEvent
    {
        public Guid RideId { get; set; }
        public decimal SearchLatitude { get; set; }
        public decimal SearchLongitude { get; set; }
        public decimal SearchRadiusKm { get; set; }
        public int DriversFound { get; set; }
        public int OffersCreated { get; set; }
    }

    // ===== DOMAIN SERVICE =====

    /// <summary>
    /// Location-based domain service
    /// Handles geospatial queries and driver location tracking
    /// </summary>
    public interface ILocationService
    {
        /// <summary>
        /// Update driver location in Redis GEO
        /// </summary>
        Task UpdateDriverLocationAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading = null,
            decimal? speed = null,
            decimal? accuracy = null,
            Guid? tripId = null);

        /// <summary>
        /// Find nearby drivers using Redis GEORADIUS
        /// Returns sorted by distance (closest first)
        /// </summary>
        Task<List<DriverLocation>> FindNearbyDriversAsync(
            decimal centerLatitude,
            decimal centerLongitude,
            decimal radiusKm = 5m,
            int maxResults = 10);

        /// <summary>
        /// Get driver's current location
        /// </summary>
        Task<DriverLocation> GetDriverLocationAsync(Guid driverId);

        /// <summary>
        /// Calculate distance between two points (Haversine formula)
        /// </summary>
        decimal CalculateDistance(Location from, Location to);

        /// <summary>
        /// Bulk update driver locations
        /// </summary>
        Task BulkUpdateLocationsAsync(
            IEnumerable<DriverLocationUpdate> updates);

        /// <summary>
        /// Check if driver is online (recent location)
        /// </summary>
        Task<bool> IsDriverOnlineAsync(Guid driverId);

        /// <summary>
        /// Mark driver offline
        /// </summary>
        Task MarkDriverOfflineAsync(Guid driverId);

        /// <summary>
        /// Get active driver count
        /// </summary>
        Task<int> GetActiveDriverCountAsync();

        /// <summary>
        /// Get location history for driver
        /// </summary>
        Task<List<DriverLocation>> GetLocationHistoryAsync(
            Guid driverId,
            DateTime startTime,
            DateTime endTime);
    }

    /// <summary>
    /// DTO for bulk location updates
    /// </summary>
    public class DriverLocationUpdate
    {
        public Guid DriverId { get; set; }
        public decimal Latitude { get; set; }
        public decimal Longitude { get; set; }
        public decimal? Heading { get; set; }
        public decimal? Speed { get; set; }
        public decimal? Accuracy { get; set; }
        public Guid? TripId { get; set; }
        public DateTime Timestamp { get; set; }
    }

    // ===== HELPER FUNCTIONS =====

    /// <summary>
    /// Distance calculation using Haversine formula
    /// </summary>
    public static class DistanceCalculator
    {
        private const decimal EarthRadiusKm = 6371m;

        /// <summary>
        /// Calculate great-circle distance between two points on Earth
        /// </summary>
        public static decimal CalculateHaversine(
            decimal lat1, decimal lon1,
            decimal lat2, decimal lon2)
        {
            var dLat = ToRadians(lat2 - lat1);
            var dLon = ToRadians(lon2 - lon1);

            var a = (decimal)Math.Sin((double)(dLat / 2)) *
                    (decimal)Math.Sin((double)(dLat / 2)) +
                    (decimal)Math.Cos((double)ToRadians(lat1)) *
                    (decimal)Math.Cos((double)ToRadians(lat2)) *
                    (decimal)Math.Sin((double)(dLon / 2)) *
                    (decimal)Math.Sin((double)(dLon / 2));

            var c = 2 * (decimal)Math.Atan2(
                (double)Math.Sqrt((double)a),
                (double)Math.Sqrt((double)(1 - a)));

            return EarthRadiusKm * c;
        }

        private static decimal ToRadians(decimal degrees)
        {
            return degrees * ((decimal)Math.PI / 180m);
        }
    }
}
