using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using StackExchange.Redis;
using AiUberMarketplace.Domain;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Cache for driver locations with Redis GEO support.
    /// Provides O(log N) lookups for nearby drivers.
    /// Supports geospatial queries and TTL-based expiration.
    /// </summary>
    public interface ILocationCache
    {
        /// <summary>
        /// Set/update driver location in cache
        /// </summary>
        Task SetLocationAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading = null,
            decimal? speed = null);

        /// <summary>
        /// Get a driver's location from cache
        /// </summary>
        Task<(decimal? Latitude, decimal? Longitude, DateTime? UpdatedAt)> GetLocationAsync(Guid driverId);

        /// <summary>
        /// Find nearby drivers within radius
        /// </summary>
        Task<List<(Guid DriverId, decimal Distance)>> FindNearbyAsync(
            decimal latitude,
            decimal longitude,
            decimal radiusKm = 5m,
            int maxResults = 10);

        /// <summary>
        /// Remove a driver from cache (e.g., when going offline)
        /// </summary>
        Task RemoveLocationAsync(Guid driverId);

        /// <summary>
        /// Get all cached driver count
        /// </summary>
        Task<int> GetCachedDriverCountAsync();

        /// <summary>
        /// Clear entire cache (use with caution)
        /// </summary>
        Task ClearAllAsync();
    }

    /// <summary>
    /// Redis-backed location cache implementation
    /// Uses Redis GEO commands for efficient geospatial queries
    /// </summary>
    public class LocationCache : ILocationCache
    {
        private readonly IDatabase _redis;

        // Redis keys
        private const string GeoKeyPrefix = "location:drivers";
        private const string LocationHashKeyPrefix = "driver:{0}:location";

        public LocationCache(IConnectionMultiplexer redis)
        {
            _redis = redis.GetDatabase();
        }

        /// <summary>
        /// Set/update driver location in cache
        /// </summary>
        public async Task SetLocationAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading = null,
            decimal? speed = null)
        {
            try
            {
                var driverIdStr = driverId.ToString();
                var now = DateTime.UtcNow;

                // Add to GEO sorted set for geospatial queries
                await _redis.GeoAddAsync(
                    GeoKeyPrefix,
                    new GeoEntry((double)longitude, (double)latitude, driverIdStr));

                // Store location details
                var locationHashKey = $"driver:{driverIdStr}:location";
                var entries = new List<HashEntry>
                {
                    new("lat", latitude.ToString()),
                    new("lng", longitude.ToString()),
                    new("updated_at", now.Ticks.ToString())
                };

                if (heading.HasValue)
                    entries.Add(new("heading", heading.Value.ToString()));
                if (speed.HasValue)
                    entries.Add(new("speed", speed.Value.ToString()));

                await _redis.HashSetAsync(locationHashKey, entries.ToArray());

                // Set TTL (24 hours)
                await _redis.KeyExpireAsync(locationHashKey, TimeSpan.FromHours(24));
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get a driver's location from cache
        /// </summary>
        public async Task<(decimal? Latitude, decimal? Longitude, DateTime? UpdatedAt)> GetLocationAsync(Guid driverId)
        {
            try
            {
                var driverIdStr = driverId.ToString();
                var locationHashKey = $"driver:{driverIdStr}:location";

                var hashData = await _redis.HashGetAllAsync(locationHashKey);
                if (hashData.Length == 0)
                {
                    return (null, null, null);
                }

                var latHash = hashData.FirstOrDefault(x => x.Name == "lat");
                var lngHash = hashData.FirstOrDefault(x => x.Name == "lng");
                var updatedHash = hashData.FirstOrDefault(x => x.Name == "updated_at");

                if (!latHash.Value.HasValue || !lngHash.Value.HasValue)
                    return (null, null, null);

                var latitude = decimal.Parse(latHash.Value.ToString());
                var longitude = decimal.Parse(lngHash.Value.ToString());

                DateTime? updatedAt = null;
                if (updatedHash.Value.HasValue && long.TryParse(updatedHash.Value.ToString(), out var ticks))
                {
                    updatedAt = new DateTime(ticks, DateTimeKind.Utc);
                }

                return (latitude, longitude, updatedAt);
            }
            catch (Exception)
            {
                return (null, null, null);
            }
        }

        /// <summary>
        /// Find nearby drivers within specified radius
        /// Uses Redis GEORADIUS command for O(log N + M) performance where M = results
        /// </summary>
        public async Task<List<(Guid DriverId, decimal Distance)>> FindNearbyAsync(
            decimal latitude,
            decimal longitude,
            decimal radiusKm = 5m,
            int maxResults = 10)
        {
            try
            {
                // GEORADIUS: Search within radius, return with distance
                var results = await _redis.GeoRadiusAsync(
                    GeoKeyPrefix,
                    (double)longitude,
                    (double)latitude,
                    (double)radiusKm,
                    GeoUnit.Kilometers,
                    count: maxResults,
                    order: null,
                    options: GeoRadiusOptions.WithDistance);

                var nearby = new List<(Guid DriverId, decimal Distance)>();

                if (results is not null)
                {
                    foreach (var result in results)
                    {
                        if (Guid.TryParse(result.Member.ToString(), out var driverId))
                        {
                            var distance = result.Distance.HasValue ? 
                                (decimal)result.Distance.Value : 0m;
                            nearby.Add(new (driverId, distance));
                        }
                    }
                }

                return nearby.OrderBy(x => x.Distance).ToList();
            }
            catch (Exception)
            {
                return new List<(Guid, decimal)>();
            }
        }

        /// <summary>
        /// Remove a driver from cache
        /// </summary>
        public async Task RemoveLocationAsync(Guid driverId)
        {
            try
            {
                var driverIdStr = driverId.ToString();
                var locationHashKey = $"driver:{driverIdStr}:location";

                // Remove from GEO set
                await _redis.GeoRemoveAsync(GeoKeyPrefix, driverIdStr);

                // Remove hash entry
                await _redis.KeyDeleteAsync(locationHashKey);
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get count of cached driver locations
        /// </summary>
        public async Task<int> GetCachedDriverCountAsync()
        {
            try
            {
                // Use ZCARD to get cardinality of GEO sorted set
                var count = await _redis.ExecuteAsync("ZCARD", GeoKeyPrefix);
                if (count.IsNull || count.Type != ResultType.Integer)
                    return 0;

                return (int)count;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        /// <summary>
        /// Clear entire location cache
        /// </summary>
        public async Task ClearAllAsync()
        {
            try
            {
                // Get all driver IDs from GEO set
                var allMembers = await _redis.GeoRadiusAsync(
                    GeoKeyPrefix,
                    0,
                    0,
                    40075, // Earth circumference in km (search entire world)
                    GeoUnit.Kilometers);

                if (allMembers is not null && allMembers.Length > 0)
                {
                    var keysToDelete = new List<RedisKey>();
                    keysToDelete.Add(GeoKeyPrefix);

                    foreach (var member in allMembers)
                    {
                        keysToDelete.Add($"driver:{member.Member}:location");
                    }

                    await _redis.KeyDeleteAsync(keysToDelete.ToArray());
                }
            }
            catch (Exception)
            {
                // Silent fail
            }
        }
    }
}
