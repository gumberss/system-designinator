using Microsoft.AspNetCore.SignalR;
using Microsoft.AspNetCore.Authorization;
using System.Collections.Concurrent;
using AiUberMarketplace.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;

namespace AiUberMarketplace.Api.Hubs
{
    /// <summary>
    /// SignalR Hub for real-time driver updates.
    /// Handles driver connections, offer notifications, and ride completion.
    /// </summary>
    [Authorize]
    public class DriverHub : Hub
    {
        private readonly AiUberMarketplaceDbContext _dbContext;
        private readonly ILogger<DriverHub> _logger;

        // Static dictionary to track connectionId -> DriverId mappings
        private static readonly ConcurrentDictionary<string, string> ConnectionToDriverMap = 
            new();

        // Static dictionary to track DriverId -> list of connectionIds for presence
        private static readonly ConcurrentDictionary<string, HashSet<string>> DriverConnections = 
            new();

        // Static dictionary to track driver availability status
        private static readonly ConcurrentDictionary<string, bool> DriverAvailability = 
            new();

        public DriverHub(AiUberMarketplaceDbContext dbContext, ILogger<DriverHub> logger)
        {
            _dbContext = dbContext;
            _logger = logger;
        }

        /// <summary>
        /// Called when a driver connects. Validates JWT claims and stores connection mapping.
        /// </summary>
        public override async Task OnConnectedAsync()
        {
            try
            {
                var driverId = GetDriverIdFromClaims();
                if (string.IsNullOrEmpty(driverId))
                {
                    _logger.LogWarning("Connection rejected: No driver ID found in claims");
                    throw new HubException("Unauthorized: Could not extract driver ID from token");
                }

                // Validate driver exists in database
                var driverExists = await _dbContext.Drivers
                    .AsNoTracking()
                    .AnyAsync(d => d.Id.ToString() == driverId);

                if (!driverExists)
                {
                    _logger.LogWarning($"Connection rejected: Driver {driverId} not found in database");
                    throw new HubException("Unauthorized: Driver not found");
                }

                // Store connection mapping
                ConnectionToDriverMap.AddOrUpdate(Context.ConnectionId, driverId, (_, _) => driverId);

                // Add to driver connections for presence tracking
                DriverConnections.AddOrUpdate(
                    driverId,
                    new HashSet<string> { Context.ConnectionId },
                    (_, connections) =>
                    {
                        connections.Add(Context.ConnectionId);
                        return connections;
                    });

                // Set initial availability (default to true on connection, but check database)
                var driver = await _dbContext.Drivers
                    .AsNoTracking()
                    .FirstOrDefaultAsync(d => d.Id.ToString() == driverId);

                if (driver != null)
                {
                    DriverAvailability.AddOrUpdate(driverId, driver.IsAvailable, (_, _) => driver.IsAvailable);
                }

                _logger.LogInformation($"Driver {driverId} connected with connectionId: {Context.ConnectionId}");
                await base.OnConnectedAsync();
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error during DriverHub connection");
                throw;
            }
        }

        /// <summary>
        /// Called when a driver disconnects. Cleans up connection mappings and updates presence.
        /// </summary>
        public override async Task OnDisconnectedAsync(Exception exception)
        {
            try
            {
                if (ConnectionToDriverMap.TryRemove(Context.ConnectionId, out var driverId))
                {
                    // Remove from driver connections
                    if (DriverConnections.TryGetValue(driverId, out var connections))
                    {
                        connections.Remove(Context.ConnectionId);
                        if (connections.Count == 0)
                        {
                            DriverConnections.TryRemove(driverId, out _);
                            DriverAvailability.TryRemove(driverId, out _);
                            _logger.LogInformation($"Driver {driverId} is now offline");
                        }
                    }

                    _logger.LogInformation($"Driver {driverId} disconnected from DriverHub");
                }

                await base.OnDisconnectedAsync(exception);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error during DriverHub disconnection");
            }
        }

        /// <summary>
        /// Notify a driver that a new ride offer has been created for them.
        /// Called from OfferNotificationService when a new ride needs a driver.
        /// </summary>
        public async Task NotifyOfferCreated(string driverId, string offerId, object rideDetails, object pickupLocation, decimal estimatedEarnings)
        {
            try
            {
                _logger.LogInformation($"NotifyOfferCreated: Offer {offerId} to driver {driverId}");
                await Clients.User(driverId).SendAsync("OfferCreated", new
                {
                    offerId = offerId,
                    rideDetails = rideDetails,
                    pickupLocation = pickupLocation,
                    estimatedEarnings = estimatedEarnings,
                    createdAt = DateTime.UtcNow,
                    expiresAt = DateTime.UtcNow.AddSeconds(30)
                });
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyOfferCreated for offer {offerId}");
            }
        }

        /// <summary>
        /// Notify a driver that they have accepted an offer (confirmation).
        /// </summary>
        public async Task NotifyOfferAccepted(string driverId, string offerId, string rideId, object riderDetails)
        {
            try
            {
                _logger.LogInformation($"NotifyOfferAccepted: Offer {offerId} accepted by driver {driverId}");
                await Clients.User(driverId).SendAsync("OfferAccepted", new
                {
                    offerId = offerId,
                    rideId = rideId,
                    riderDetails = riderDetails,
                    acceptedAt = DateTime.UtcNow
                });
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyOfferAccepted for offer {offerId}");
            }
        }

        /// <summary>
        /// Notify a driver that an offer they didn't accept has been rejected/expired.
        /// </summary>
        public async Task NotifyOfferRejected(string driverId, string offerId)
        {
            try
            {
                _logger.LogInformation($"NotifyOfferRejected: Offer {offerId} rejected for driver {driverId}");
                await Clients.User(driverId).SendAsync("OfferRejected", offerId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyOfferRejected for offer {offerId}");
            }
        }

        /// <summary>
        /// Request a driver to send their current location.
        /// Called by LocationBroadcastService when location is needed.
        /// </summary>
        public async Task NotifyLocationRequested(string driverId)
        {
            try
            {
                _logger.LogDebug($"Requesting location from driver {driverId}");
                await Clients.User(driverId).SendAsync("LocationRequested");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in NotifyLocationRequested for driver {driverId}");
            }
        }

        /// <summary>
        /// Notify a driver that their ride has been completed.
        /// </summary>
        public async Task NotifyRideCompleted(string driverId, string rideId, decimal earnings, decimal rating)
        {
            try
            {
                _logger.LogInformation($"NotifyRideCompleted: Ride {rideId} completed for driver {driverId}");
                await Clients.User(driverId).SendAsync("RideCompleted", new
                {
                    rideId = rideId,
                    earnings = earnings,
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
        /// Client method: Accept an offer. Called when driver clicks "Accept".
        /// </summary>
        public async Task AcceptOffer(string offerId)
        {
            try
            {
                var driverId = GetDriverIdFromClaims();
                if (string.IsNullOrEmpty(driverId))
                {
                    await Clients.Caller.SendAsync("ReceiveError", "Unauthorized");
                    return;
                }

                _logger.LogInformation($"Driver {driverId} accepted offer {offerId}");

                // TODO: Call OfferNotificationService.AcceptOfferAsync(offerId, driverId)
                // This will update the database and notify the rider

                await Clients.Caller.SendAsync("OfferAcceptanceProcessed", offerId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in AcceptOffer");
                await Clients.Caller.SendAsync("ReceiveError", "Failed to accept offer");
            }
        }

        /// <summary>
        /// Client method: Reject an offer. Called when driver clicks "Reject".
        /// </summary>
        public async Task RejectOffer(string offerId)
        {
            try
            {
                var driverId = GetDriverIdFromClaims();
                if (string.IsNullOrEmpty(driverId))
                {
                    await Clients.Caller.SendAsync("ReceiveError", "Unauthorized");
                    return;
                }

                _logger.LogInformation($"Driver {driverId} rejected offer {offerId}");

                // TODO: Call OfferNotificationService.RejectOfferAsync(offerId, driverId)
                // This will move to the next driver in queue

                await Clients.Caller.SendAsync("OfferRejectionProcessed", offerId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in RejectOffer");
                await Clients.Caller.SendAsync("ReceiveError", "Failed to reject offer");
            }
        }

        /// <summary>
        /// Client method: Update driver's current location.
        /// Called periodically by the mobile app to send GPS coordinates.
        /// </summary>
        public async Task UpdateLocation(decimal latitude, decimal longitude, decimal? heading = null, decimal? speed = null)
        {
            try
            {
                var driverId = GetDriverIdFromClaims();
                if (string.IsNullOrEmpty(driverId))
                {
                    await Clients.Caller.SendAsync("ReceiveError", "Unauthorized");
                    return;
                }

                _logger.LogDebug($"Driver {driverId} location update: ({latitude}, {longitude})");

                // TODO: Call LocationService.UpdateDriverLocationAsync(driverId, latitude, longitude)
                // This will store the location in Redis and trigger broadcasts

                await Clients.Caller.SendAsync("LocationUpdateAcknowledged");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in UpdateLocation");
                await Clients.Caller.SendAsync("ReceiveError", "Failed to update location");
            }
        }

        /// <summary>
        /// Client method: Set driver availability status.
        /// </summary>
        public async Task SetAvailability(bool isAvailable)
        {
            try
            {
                var driverId = GetDriverIdFromClaims();
                if (string.IsNullOrEmpty(driverId))
                {
                    await Clients.Caller.SendAsync("ReceiveError", "Unauthorized");
                    return;
                }

                DriverAvailability.AddOrUpdate(driverId, isAvailable, (_, _) => isAvailable);
                _logger.LogInformation($"Driver {driverId} availability set to {isAvailable}");

                // TODO: Update database: driver.IsAvailable = isAvailable

                await Clients.Caller.SendAsync("AvailabilityUpdated", isAvailable);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, $"Error in SetAvailability");
                await Clients.Caller.SendAsync("ReceiveError", "Failed to update availability");
            }
        }

        /// <summary>
        /// Check if a driver is currently online (has active connections).
        /// </summary>
        public static bool IsDriverOnline(string driverId)
        {
            return DriverConnections.TryGetValue(driverId, out var connections) && connections.Count > 0;
        }

        /// <summary>
        /// Check if a driver is available (online and has set availability to true).
        /// </summary>
        public static bool IsDriverAvailable(string driverId)
        {
            return IsDriverOnline(driverId) && DriverAvailability.TryGetValue(driverId, out var available) && available;
        }

        /// <summary>
        /// Get all connection IDs for a specific driver.
        /// </summary>
        public static IEnumerable<string> GetDriverConnections(string driverId)
        {
            if (DriverConnections.TryGetValue(driverId, out var connections))
            {
                return connections.ToList();
            }
            return Enumerable.Empty<string>();
        }

        /// <summary>
        /// Extract driver ID from JWT claims.
        /// </summary>
        private string GetDriverIdFromClaims()
        {
            var driverId = Context.User?.FindFirst("sub")?.Value ??
                          Context.User?.FindFirst("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier")?.Value ??
                          Context.User?.FindFirst("userId")?.Value;

            return driverId;
        }
    }
}
