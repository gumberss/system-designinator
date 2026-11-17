using Microsoft.AspNetCore.SignalR;
using Microsoft.AspNetCore.Authorization;
using System.Collections.Concurrent;
using AiUberMarketplace.Infrastructure.Persistence;
using AiUberMarketplace.Application.Services;
using Microsoft.EntityFrameworkCore;

namespace AiUberMarketplace.Api.Hubs
{
    /// <summary>
    /// SignalR Hub for real-time ride updates sent to riders.
    /// Handles rider connections, location broadcasting, and offer notifications.
    /// </summary>
    [Authorize]
    public class RideHub : Hub, ILocationBroadcastNotifier
    {
        private readonly AiUberMarketplaceDbContext _dbContext;
        private readonly ILogger<RideHub> _logger;

        // Static dictionary to track connectionId -> RiderId mappings
        private static readonly ConcurrentDictionary<string, string> ConnectionToRiderMap = 
            new();

        // Static dictionary to track RiderId -> list of connectionIds for presence
        private static readonly ConcurrentDictionary<string, HashSet<string>> RiderConnections = 
            new();

        public RideHub(AiUberMarketplaceDbContext dbContext, ILogger<RideHub> logger)
        {
            _dbContext = dbContext;
            _logger = logger;
        }

        /// <summary>
        /// Called when a rider connects. Validates JWT claims and stores connection mapping.
        /// </summary>
        public override async Task OnConnectedAsync()
        {
            try
            {
                var riderId = GetRiderIdFromClaims();
                if (string.IsNullOrEmpty(riderId))
                {
                    _logger.LogWarning("Connection rejected: No rider ID found in claims");
                    throw new HubException("Unauthorized: Could not extract rider ID from token");
                }

                // Validate rider exists in database
                var riderExists = await _dbContext.Riders
                    .AsNoTracking()
                    .AnyAsync(r => r.Id.ToString() == riderId);

                if (!riderExists)
                {
                    _logger.LogWarning($"Connection rejected: Rider {riderId} not found in database");
                    throw new HubException("Unauthorized: Rider not found");
                }

                // Store connection mapping
                ConnectionToRiderMap.AddOrUpdate(Context.ConnectionId, riderId, (_, _) => riderId);

                // Add to rider connections for presence tracking
                RiderConnections.AddOrUpdate(
                    riderId,
                    new HashSet<string> { Context.ConnectionId },
                    (_, connections) =>
                    {
                        connections.Add(Context.ConnectionId);
                        return connections;
                    });

                _logger.LogInformation($"Rider {riderId} connected with connectionId: {Context.ConnectionId}");
                await base.OnConnectedAsync();
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error during RideHub connection");
                throw;
            }
        }

        /// <summary>
        /// Called when a rider disconnects. Cleans up connection mappings and updates presence.
        /// </summary>
        public override async Task OnDisconnectedAsync(Exception exception)
        {
            try
            {
                if (ConnectionToRiderMap.TryRemove(Context.ConnectionId, out var riderId))
                {
                    // Remove from rider connections
                    if (RiderConnections.TryGetValue(riderId, out var connections))
                    {
                        connections.Remove(Context.ConnectionId);
                        if (connections.Count == 0)
                        {
                            RiderConnections.TryRemove(riderId, out _);
                            _logger.LogInformation($"Rider {riderId} is now offline");
                        }
                    }

                    _logger.LogInformation($"Rider {riderId} disconnected from RideHub");
                }

                await base.OnDisconnectedAsync(exception);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error during RideHub disconnection");
            }
        }

        /// <summary>
        /// Notify a rider that their ride request has been created.
        /// Sent from the API when POST /rides is called.
        /// </summary>
        public async Task NotifyRideCreated(string rideId, object rideDetails)
        {
            try
            {
                var riderId = GetRiderIdFromClaims();
                if (string.IsNullOrEmpty(riderId))
                {
                    await Clients.Caller.SendAsync("ReceiveError", "Unauthorized");
                    return;
                }

                _logger.LogInformation($"NotifyRideCreated for ride {rideId} to rider {riderId}");
                await Clients.User(riderId).SendAsync("RideCreated", rideId, rideDetails);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyRideCreated for ride {rideId}");
                await Clients.Caller.SendAsync("ReceiveError", "Failed to notify ride creation");
            }
        }

        /// <summary>
        /// Notify a rider that they've received an offer from a driver.
        /// This is called from OfferNotificationService when a driver accepts an offer.
        /// </summary>
        public async Task NotifyOfferReceived(string riderId, string offerId, object driverDetails, string estimatedPickup)
        {
            try
            {
                _logger.LogInformation($"NotifyOfferReceived: Offer {offerId} from driver to rider {riderId}");
                await Clients.User(riderId).SendAsync("OfferReceived", offerId, driverDetails, estimatedPickup);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyOfferReceived for offer {offerId}");
            }
        }

        /// <summary>
        /// Stream driver location updates to a rider in real-time.
        /// Called by LocationBroadcastService every 2-4 seconds.
        /// </summary>
        public async Task NotifyDriverLocationUpdated(string riderId, string driverId, decimal latitude, decimal longitude, string eta)
        {
            try
            {
                _logger.LogDebug($"Broadcasting driver {driverId} location to rider {riderId}");
                await Clients.User(riderId).SendAsync("DriverLocationUpdated", new
                {
                    driverId = driverId,
                    latitude = latitude,
                    longitude = longitude,
                    eta = eta,
                    timestamp = DateTime.UtcNow
                });
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyDriverLocationUpdated for driver {driverId} to rider {riderId}");
            }
        }

        /// <summary>
        /// Notify a rider that an offer has expired (no driver accepted in time).
        /// </summary>
        public async Task NotifyOfferExpired(string riderId, string offerId)
        {
            try
            {
                _logger.LogInformation($"NotifyOfferExpired: Offer {offerId} expired for rider {riderId}");
                await Clients.User(riderId).SendAsync("OfferExpired", offerId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyOfferExpired for offer {offerId}");
            }
        }

        /// <summary>
        /// Notify a rider that their ride has been completed.
        /// </summary>
        public async Task NotifyRideCompleted(string riderId, string rideId, decimal finalPrice, decimal rating)
        {
            try
            {
                _logger.LogInformation($"NotifyRideCompleted: Ride {rideId} completed for rider {riderId}");
                await Clients.User(riderId).SendAsync("RideCompleted", new
                {
                    rideId = rideId,
                    finalPrice = finalPrice,
                    rating = rating,
                    completedAt = DateTime.UtcNow
                });
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyRideCompleted for ride {rideId}");
            }
        }

        /// <summary>
        /// Check if a rider is currently online (has active connections).
        /// </summary>
        public static bool IsRiderOnline(string riderId)
        {
            return RiderConnections.TryGetValue(riderId, out var connections) && connections.Count > 0;
        }

        /// <summary>
        /// Get all connection IDs for a specific rider.
        /// </summary>
        public static IEnumerable<string> GetRiderConnections(string riderId)
        {
            if (RiderConnections.TryGetValue(riderId, out var connections))
            {
                return connections.ToList();
            }
            return Enumerable.Empty<string>();
        }

        /// <summary>
        /// Extract rider ID from JWT claims.
        /// </summary>
        private string GetRiderIdFromClaims()
        {
            var riderId = Context.User?.FindFirst("sub")?.Value ??
                         Context.User?.FindFirst("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier")?.Value ??
                         Context.User?.FindFirst("userId")?.Value;

            return riderId;
        }

        /// <summary>
        /// Broadcast driver location update to all connected riders (ILocationBroadcastNotifier implementation)
        /// </summary>
        public async Task NotifyDriverLocationUpdatedAsync(
            Guid driverId,
            decimal latitude,
            decimal longitude,
            decimal? heading,
            decimal? speed,
            DateTime timestamp,
            CancellationToken cancellationToken = default)
        {
            try
            {
                await Clients.All.SendAsync(
                    "DriverLocationUpdated",
                    driverId.ToString(),
                    latitude,
                    longitude,
                    heading ?? 0m,
                    speed ?? 0m,
                    timestamp,
                    cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error notifying driver location update for driver {driverId}");
            }
        }
    }
}
