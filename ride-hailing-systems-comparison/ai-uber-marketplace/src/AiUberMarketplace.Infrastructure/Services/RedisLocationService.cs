using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using StackExchange.Redis;
using AiUberMarketplace.Domain;
using AiUberMarketplace.Domain.Services;

namespace AiUberMarketplace.Infrastructure.Services
{
    /// <summary>
    /// Location service implementation using Redis GEO
    /// Handles real-time driver location tracking and geospatial queries
    /// </summary>
    public class RedisLocationService : ILocationService
    {
        private readonly IDatabase _redis;
        private readonly IServer _redisServer;
        
        // Redis keys
        private const string GeoKeyPrefix = "location:active";
        private const string LocationHashKeyPrefix = "driver:{0}:location";
        private const string StatusKeyPrefix = "driver:{0}:status";
        private const string OnlineSetKey = "drivers:online";
        private const string HistoryKeyPrefix = "driver:{0}:history";
        
        // Configuration
        private const int LocationHistoryMaxLength = 1000; // Keep last 1000 locations
        private const int OfflineThresholdMinutes = 5; // After 5 min without update = offline

        public RedisLocationService(IConnectionMultiplexer redis)
        {
            _redis = redis.GetDatabase();
            _redisServer = redis.GetServer(redis.GetEndPoints().First());
        }

        /// <summary>
        /// Update driver location in Redis GEO and hash
        /// </summary>
        public async Task UpdateDriverLocationAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading = null,
            decimal? speed = null,
            decimal? accuracy = null,
            Guid? tripId = null)
        {
            var driverIdStr = driverId.ToString();
            var now = DateTime.UtcNow;

            // 1. Add/Update to GEO sorted set (for GEORADIUS queries)
            await _redis.GeoAddAsync(
                GeoKeyPrefix,
                new GeoEntry((double)longitude, (double)latitude, $"driver:{driverIdStr}"));

            // 2. Store location details in hash
            var locationHashKey = new RedisKey(string.Format(LocationHashKeyPrefix, driverIdStr));
            var entries = new List<HashEntry>
            {
                new("lat", latitude.ToString()),
                new("lng", longitude.ToString()),
                new("updated_at", now.Ticks.ToString()),
                new("updated_at_unix", DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString())
            };

            if (heading.HasValue)
                entries.Add(new("heading", heading.Value.ToString()));
            if (speed.HasValue)
                entries.Add(new("speed", speed.Value.ToString()));
            if (accuracy.HasValue)
                entries.Add(new("accuracy", accuracy.Value.ToString()));
            if (tripId.HasValue)
                entries.Add(new("trip_id", tripId.Value.ToString()));

            await _redis.HashSetAsync(locationHashKey, entries.ToArray());

            // 3. Set expiration on location (auto-cleanup if not updated)
            await _redis.KeyExpireAsync(locationHashKey, TimeSpan.FromHours(24));

            // 4. Add to online set
            var onlineKey = new RedisKey($"driver:{driverIdStr}:online");
            await _redis.StringSetAsync(onlineKey, "1", TimeSpan.FromHours(24));

            // 5. Store history (for analytics/replay)
            var historyKey = new RedisKey(string.Format(HistoryKeyPrefix, driverIdStr));
            var historyEntry = $"{now.Ticks}|{latitude}|{longitude}|{heading}|{speed}";
            await _redis.ListRightPushAsync(historyKey, historyEntry);
            await _redis.ListTrimAsync(historyKey, -LocationHistoryMaxLength, -1); // Keep only last 1000
            await _redis.KeyExpireAsync(historyKey, TimeSpan.FromDays(30)); // Expire after 30 days
        }

        /// <summary>
        /// Find nearby drivers using Redis GEORADIUS
        /// O(N+log(M)) where N = elements within radius, M = total elements
        /// </summary>
        public async Task<List<DriverLocation>> FindNearbyDriversAsync(
            decimal centerLatitude,
            decimal centerLongitude,
            decimal radiusKm = 5m,
            int maxResults = 10)
        {
            // GEORADIUS returns [member, distance] pairs
            var results = await _redis.GeoRadiusAsync(
                GeoKeyPrefix,
                (double)centerLongitude,
                (double)centerLatitude,
                (double)radiusKm,
                GeoUnit.Kilometers,
                count: maxResults,
                options: GeoRadiusOptions.WithDistance | GeoRadiusOptions.WithCoordinates);

            if (results == null || results.Length == 0)
                return new List<DriverLocation>();

            var driverLocations = new List<DriverLocation>();

            foreach (var result in results)
            {
                var memberId = result.Member.ToString(); // Format: "driver:uuid"
                if (!memberId.StartsWith("driver:"))
                    continue;

                var driverIdStr = memberId.Substring("driver:".Length);
                if (!Guid.TryParse(driverIdStr, out var driverId))
                    continue;

                // Fetch full location details from hash
                var locationDetails = await GetLocationFromHashAsync(driverId);
                if (locationDetails != null)
                {
                    locationDetails.Distance = (decimal)(result.Distance ?? 0);
                    driverLocations.Add(locationDetails);
                }
            }

            return driverLocations.OrderBy(d => d.Distance).ToList();
        }

        /// <summary>
        /// Get driver's current location from Redis hash
        /// </summary>
        public async Task<DriverLocation> GetDriverLocationAsync(Guid driverId)
        {
            var locationDetails = await GetLocationFromHashAsync(driverId);
            return locationDetails;
        }

        /// <summary>
        /// Calculate distance using Haversine formula
        /// </summary>
        public decimal CalculateDistance(Location from, Location to)
        {
            return DistanceCalculator.CalculateHaversine(
                from.Latitude, from.Longitude,
                to.Latitude, to.Longitude);
        }

        /// <summary>
        /// Bulk update multiple driver locations
        /// Optimized for high volume updates
        /// </summary>
        public async Task BulkUpdateLocationsAsync(
            IEnumerable<DriverLocationUpdate> updates)
        {
            var batch = _redis.CreateBatch();
            var tasks = new List<Task>();

            foreach (var update in updates)
            {
                tasks.Add(UpdateDriverLocationAsync(
                    update.DriverId,
                    update.Latitude,
                    update.Longitude,
                    update.Heading,
                    update.Speed,
                    update.Accuracy,
                    update.TripId));
            }

            await Task.WhenAll(tasks);
        }

        /// <summary>
        /// Check if driver is online (recent location)
        /// A driver is online if:
        /// 1. Has location in Redis GEO
        /// 2. Last update was < 5 minutes ago
        /// </summary>
        public async Task<bool> IsDriverOnlineAsync(Guid driverId)
        {
            var location = await GetDriverLocationAsync(driverId);
            if (location == null)
                return false;

            // Check if location is recent (< 5 minutes old)
            var age = DateTime.UtcNow - location.UpdatedAt;
            return age < TimeSpan.FromMinutes(OfflineThresholdMinutes);
        }

        /// <summary>
        /// Mark driver offline (remove from GEO index)
        /// </summary>
        public async Task MarkDriverOfflineAsync(Guid driverId)
        {
            var driverIdStr = driverId.ToString();
            
            // Remove from GEO index
            await _redis.GeoRemoveAsync(GeoKeyPrefix, $"driver:{driverIdStr}");
            
            // Remove location hash
            await _redis.KeyDeleteAsync(
                new RedisKey(string.Format(LocationHashKeyPrefix, driverIdStr)));
            
            // Remove from online set
            await _redis.KeyDeleteAsync(new RedisKey($"driver:{driverIdStr}:online"));
        }

        /// <summary>
        /// Get count of active drivers
        /// </summary>
        public async Task<int> GetActiveDriverCountAsync()
        {
            var count = await _redis.GeoRadiusAsync(
                GeoKeyPrefix,
                0, 0, // Any center, we want all
                40075, // Earth circumference in km
                GeoUnit.Kilometers,
                options: GeoRadiusOptions.None);

            return count?.Length ?? 0;
        }

        /// <summary>
        /// Get location history for a driver (time-series)
        /// </summary>
        public async Task<List<DriverLocation>> GetLocationHistoryAsync(
            Guid driverId,
            DateTime startTime,
            DateTime endTime)
        {
            var historyKey = new RedisKey(string.Format(HistoryKeyPrefix, driverId));
            var entries = await _redis.ListRangeAsync(historyKey);

            var locations = new List<DriverLocation>();

            foreach (var entry in entries)
            {
                if (entry.IsNullOrEmpty)
                    continue;

                var parts = entry.ToString().Split('|');
                if (parts.Length < 3)
                    continue;

                if (!long.TryParse(parts[0], out var ticks) ||
                    !decimal.TryParse(parts[1], out var lat) ||
                    !decimal.TryParse(parts[2], out var lng))
                    continue;

                var timestamp = new DateTime(ticks, DateTimeKind.Utc);
                if (timestamp < startTime || timestamp > endTime)
                    continue;

                decimal? heading = null;
                decimal? speed = null;

                if (parts.Length > 3 && decimal.TryParse(parts[3], out var h))
                    heading = h;
                if (parts.Length > 4 && decimal.TryParse(parts[4], out var s))
                    speed = s;

                locations.Add(new DriverLocation
                {
                    DriverId = driverId,
                    Latitude = lat,
                    Longitude = lng,
                    Heading = heading,
                    Speed = speed,
                    UpdatedAt = timestamp
                });
            }

            return locations.OrderBy(l => l.UpdatedAt).ToList();
        }

        // ===== PRIVATE HELPERS =====

        private async Task<DriverLocation> GetLocationFromHashAsync(Guid driverId)
        {
            var driverIdStr = driverId.ToString();
            var locationHashKey = new RedisKey(string.Format(LocationHashKeyPrefix, driverIdStr));

            var hash = await _redis.HashGetAllAsync(locationHashKey);
            if (hash.Length == 0)
                return null;

            var location = new DriverLocation { DriverId = driverId };

            foreach (var entry in hash)
            {
                switch (entry.Name.ToString())
                {
                    case "lat":
                        if (decimal.TryParse(entry.Value.ToString(), out var lat))
                            location.Latitude = lat;
                        break;
                    case "lng":
                        if (decimal.TryParse(entry.Value.ToString(), out var lng))
                            location.Longitude = lng;
                        break;
                    case "heading":
                        if (decimal.TryParse(entry.Value.ToString(), out var heading))
                            location.Heading = heading;
                        break;
                    case "speed":
                        if (decimal.TryParse(entry.Value.ToString(), out var speed))
                            location.Speed = speed;
                        break;
                    case "accuracy":
                        if (decimal.TryParse(entry.Value.ToString(), out var accuracy))
                            location.Accuracy = accuracy;
                        break;
                    case "updated_at":
                        if (long.TryParse(entry.Value.ToString(), out var ticks))
                            location.UpdatedAt = new DateTime(ticks, DateTimeKind.Utc);
                        break;
                    case "trip_id":
                        if (Guid.TryParse(entry.Value.ToString(), out var tripId))
                            location.TripId = tripId;
                        break;
                }
            }

            return location;
        }
    }
}
