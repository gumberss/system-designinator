using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using StackExchange.Redis;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Surge pricing calculation service
    /// Adjusts ride prices based on demand/supply ratio
    /// </summary>
    public interface ISurgePricingService
    {
        /// <summary>
        /// Calculate surge multiplier for a location at current time
        /// Returns 1.0 (no surge) to 3.0 (triple price)
        /// </summary>
        Task<decimal> CalculateSurgeMultiplierAsync(
            decimal latitude,
            decimal longitude,
            DateTime? at = null);

        /// <summary>
        /// Update demand metrics for a location
        /// Called when new ride requests arrive
        /// </summary>
        Task RecordRideDemandAsync(
            decimal latitude,
            decimal longitude);

        /// <summary>
        /// Update supply metrics for a location
        /// Called when drivers go online/offline
        /// </summary>
        Task RecordDriverSupplyAsync(
            decimal latitude,
            decimal longitude,
            bool isOnline);

        /// <summary>
        /// Get current demand/supply ratio for analytics
        /// </summary>
        Task<(int DemandRequests, int AvailableDrivers)> GetMetricsAsync(
            decimal latitude,
            decimal longitude);

        /// <summary>
        /// Get surge pricing history for a location
        /// </summary>
        Task<List<SurgePricingRecord>> GetHistoryAsync(
            decimal latitude,
            decimal longitude,
            DateTime startTime,
            DateTime endTime);
    }

    /// <summary>
    /// Surge pricing record for analytics
    /// </summary>
    public class SurgePricingRecord
    {
        public DateTime Timestamp { get; set; }
        public decimal Multiplier { get; set; }
        public int DemandRequests { get; set; }
        public int AvailableDrivers { get; set; }
    }

    /// <summary>
    /// Implementation using Redis for real-time metrics
    /// </summary>
    public class SurgePricingService : ISurgePricingService
    {
        private readonly IDatabase _redis;
        private readonly IServer _redisServer;

        // Redis keys for metrics
        private const string DemandKeyPrefix = "surge:demand:{0}"; // Grid cell
        private const string SupplyKeyPrefix = "surge:supply:{0}"; // Grid cell
        private const string HistoryKeyPrefix = "surge:history:{0}"; // Grid cell

        // Grid size: 0.01 degrees ≈ 1km at equator
        private const decimal GridSize = 0.01m;

        // Surge pricing thresholds
        private const decimal LowDemandMultiplier = 1.0m;
        private const decimal MediumDemandMultiplier = 1.25m;
        private const decimal HighDemandMultiplier = 1.75m;
        private const decimal VeryHighDemandMultiplier = 2.5m;
        private const decimal MaxMultiplier = 3.0m;

        public SurgePricingService(IConnectionMultiplexer redis)
        {
            _redis = redis.GetDatabase();
            _redisServer = redis.GetServer(redis.GetEndPoints().First());
        }

        /// <summary>
        /// Calculate surge multiplier based on demand/supply ratio
        /// Uses grid cells for efficient metrics collection
        /// </summary>
        public async Task<decimal> CalculateSurgeMultiplierAsync(
            decimal latitude,
            decimal longitude,
            DateTime? at = null)
        {
            var gridCell = GetGridCell(latitude, longitude);
            var demandKey = string.Format(DemandKeyPrefix, gridCell);
            var supplyKey = string.Format(SupplyKeyPrefix, gridCell);

            // Get current demand and supply
            var demandStr = await _redis.StringGetAsync(demandKey);
            var supplyStr = await _redis.StringGetAsync(supplyKey);

            var demand = demandStr.HasValue && int.TryParse(demandStr.ToString(), out var d) ? d : 0;
            var supply = supplyStr.HasValue && int.TryParse(supplyStr.ToString(), out var s) ? s : 1;

            // Avoid division by zero
            if (supply == 0)
                supply = 1;

            // Calculate ratio (demand per driver)
            var ratio = (decimal)demand / supply;

            // Calculate multiplier based on ratio
            var multiplier = CalculateMultiplierFromRatio(ratio);

            // Record to history
            await RecordSurgeHistoryAsync(gridCell, multiplier, demand, supply);

            return multiplier;
        }

        /// <summary>
        /// Record ride demand for a location
        /// </summary>
        public async Task RecordRideDemandAsync(
            decimal latitude,
            decimal longitude)
        {
            var gridCell = GetGridCell(latitude, longitude);
            var demandKey = string.Format(DemandKeyPrefix, gridCell);

            // Increment demand counter with 30-minute expiration
            await _redis.StringIncrementAsync(demandKey);
            await _redis.KeyExpireAsync(demandKey, TimeSpan.FromMinutes(30));
        }

        /// <summary>
        /// Record driver supply for a location
        /// </summary>
        public async Task RecordDriverSupplyAsync(
            decimal latitude,
            decimal longitude,
            bool isOnline)
        {
            var gridCell = GetGridCell(latitude, longitude);
            var supplyKey = string.Format(SupplyKeyPrefix, gridCell);

            if (isOnline)
            {
                // Increment supply when driver goes online
                await _redis.StringIncrementAsync(supplyKey);
            }
            else
            {
                // Decrement supply when driver goes offline
                var current = await _redis.StringGetAsync(supplyKey);
                if (current.HasValue && int.TryParse(current.ToString(), out var val) && val > 0)
                {
                    await _redis.StringDecrementAsync(supplyKey);
                }
            }

            // Supply expires after 1 hour (driver session timeout)
            await _redis.KeyExpireAsync(supplyKey, TimeSpan.FromHours(1));
        }

        /// <summary>
        /// Get current metrics for a location
        /// </summary>
        public async Task<(int DemandRequests, int AvailableDrivers)> GetMetricsAsync(
            decimal latitude,
            decimal longitude)
        {
            var gridCell = GetGridCell(latitude, longitude);
            var demandKey = string.Format(DemandKeyPrefix, gridCell);
            var supplyKey = string.Format(SupplyKeyPrefix, gridCell);

            var demandStr = await _redis.StringGetAsync(demandKey);
            var supplyStr = await _redis.StringGetAsync(supplyKey);

            var demand = demandStr.HasValue && int.TryParse(demandStr.ToString(), out var d) ? d : 0;
            var supply = supplyStr.HasValue && int.TryParse(supplyStr.ToString(), out var s) ? s : 0;

            return (demand, supply);
        }

        /// <summary>
        /// Get surge pricing history
        /// </summary>
        public async Task<List<SurgePricingRecord>> GetHistoryAsync(
            decimal latitude,
            decimal longitude,
            DateTime startTime,
            DateTime endTime)
        {
            var gridCell = GetGridCell(latitude, longitude);
            var historyKey = string.Format(HistoryKeyPrefix, gridCell);

            var entries = await _redis.ListRangeAsync(historyKey);
            var records = new List<SurgePricingRecord>();

            foreach (var entry in entries)
            {
                if (entry.IsNullOrEmpty)
                    continue;

                var parts = entry.ToString().Split('|');
                if (parts.Length < 4)
                    continue;

                if (!long.TryParse(parts[0], out var ticks) ||
                    !decimal.TryParse(parts[1], out var multiplier) ||
                    !int.TryParse(parts[2], out var demand) ||
                    !int.TryParse(parts[3], out var supply))
                    continue;

                var timestamp = new DateTime(ticks, DateTimeKind.Utc);
                if (timestamp < startTime || timestamp > endTime)
                    continue;

                records.Add(new SurgePricingRecord
                {
                    Timestamp = timestamp,
                    Multiplier = multiplier,
                    DemandRequests = demand,
                    AvailableDrivers = supply
                });
            }

            return records.OrderBy(r => r.Timestamp).ToList();
        }

        // ===== PRIVATE HELPERS =====

        /// <summary>
        /// Get grid cell ID for latitude/longitude
        /// Quantizes coordinates to grid size
        /// </summary>
        private string GetGridCell(decimal latitude, decimal longitude)
        {
            var gridLat = Math.Floor(latitude / GridSize) * GridSize;
            var gridLng = Math.Floor(longitude / GridSize) * GridSize;
            return $"{gridLat:F2},{gridLng:F2}";
        }

        /// <summary>
        /// Calculate multiplier from demand/supply ratio
        /// Ratio = demand / supply (requests per available driver)
        /// 0.5 = plenty of supply → 1.0x
        /// 1.0 = balanced → 1.25x
        /// 2.0 = high demand → 1.75x
        /// 4.0+ = very high demand → 2.5-3.0x
        /// </summary>
        private decimal CalculateMultiplierFromRatio(decimal ratio)
        {
            if (ratio <= 0.5m)
                return LowDemandMultiplier;
            if (ratio <= 1.0m)
                return MediumDemandMultiplier;
            if (ratio <= 2.0m)
                return HighDemandMultiplier;
            if (ratio <= 4.0m)
                return VeryHighDemandMultiplier;

            // Very high ratio, cap at max multiplier
            return MaxMultiplier;
        }

        /// <summary>
        /// Record surge pricing event to history
        /// </summary>
        private async Task RecordSurgeHistoryAsync(
            string gridCell,
            decimal multiplier,
            int demand,
            int supply)
        {
            var historyKey = string.Format(HistoryKeyPrefix, gridCell);
            var record = $"{DateTime.UtcNow.Ticks}|{multiplier}|{demand}|{supply}";

            await _redis.ListRightPushAsync(historyKey, record);
            await _redis.ListTrimAsync(historyKey, -1000, -1); // Keep last 1000 records
            await _redis.KeyExpireAsync(historyKey, TimeSpan.FromDays(7)); // Expire after 7 days
        }
    }
}
