using System;
using System.Threading.Tasks;
using Microsoft.AspNetCore.SignalR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.EntityFrameworkCore;
using AiUberMarketplace.Infrastructure.Persistence;
using AiUberMarketplace.Application.Services;

namespace AiUberMarketplace.Api.Hubs
{
    /// <summary>
    /// SignalR Hub for real-time chat between riders and drivers
    /// Supports message sending, read receipts, and typing indicators
    /// </summary>
    [Authorize]
    public class ChatHub : Hub
    {
        private readonly AiUberMarketplaceDbContext _dbContext;
        private readonly IChatService _chatService;
        private readonly ILogger<ChatHub> _logger;

        public ChatHub(
            AiUberMarketplaceDbContext dbContext,
            IChatService chatService,
            ILogger<ChatHub> logger)
        {
            _dbContext = dbContext;
            _chatService = chatService;
            _logger = logger;
        }

        /// <summary>
        /// Called when a user connects
        /// </summary>
        public override async Task OnConnectedAsync()
        {
            try
            {
                var userId = GetUserIdFromClaims();
                if (string.IsNullOrEmpty(userId))
                {
                    _logger.LogWarning("Chat connection rejected: No user ID found");
                    throw new HubException("Unauthorized: Could not extract user ID");
                }

                // Join group by user ID for targeted messaging
                await Groups.AddToGroupAsync(Context.ConnectionId, $"user:{userId}");

                _logger.LogInformation($"User {userId} connected to chat hub");
                await base.OnConnectedAsync();
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error during ChatHub connection");
                throw;
            }
        }

        /// <summary>
        /// Called when a user disconnects
        /// </summary>
        public override async Task OnDisconnectedAsync(Exception exception)
        {
            try
            {
                var userId = GetUserIdFromClaims();
                if (!string.IsNullOrEmpty(userId))
                {
                    await Groups.RemoveFromGroupAsync(Context.ConnectionId, $"user:{userId}");
                    _logger.LogInformation($"User {userId} disconnected from chat hub");
                }

                await base.OnDisconnectedAsync(exception);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error during ChatHub disconnection");
            }
        }

        /// <summary>
        /// Send a message to another user
        /// </summary>
        public async Task SendMessageAsync(string rideId, string recipientId, string message)
        {
            try
            {
                var senderId = GetUserIdFromClaims();
                if (string.IsNullOrEmpty(senderId))
                {
                    throw new HubException("Unauthorized: Could not extract sender ID");
                }

                if (string.IsNullOrEmpty(message) || string.IsNullOrWhiteSpace(message))
                {
                    throw new ArgumentException("Message cannot be empty");
                }

                if (!Guid.TryParse(rideId, out var rideGuid))
                {
                    throw new ArgumentException("Invalid ride ID");
                }

                if (!Guid.TryParse(senderId, out var senderGuid))
                {
                    throw new ArgumentException("Invalid sender ID");
                }

                if (!Guid.TryParse(recipientId, out var recipientGuid))
                {
                    throw new ArgumentException("Invalid recipient ID");
                }

                // Determine sender type (rider or driver)
                var senderType = "rider"; // Default to rider; could be determined from user roles
                var senderName = senderId; // Would get actual name from database

                // Save message
                await _chatService.SendMessageAsync(
                    rideGuid,
                    senderGuid,
                    senderName,
                    senderType,
                    recipientGuid,
                    recipientId,
                    message);

                // Send to recipient
                await Clients.Group($"user:{recipientId}").SendAsync(
                    "ReceiveMessage",
                    rideId,
                    senderId,
                    senderName,
                    message,
                    DateTime.UtcNow);

                // Acknowledge to sender
                await Clients.Caller.SendAsync(
                    "MessageSent",
                    rideId,
                    recipientId,
                    DateTime.UtcNow);

                _logger.LogInformation($"Message sent from {senderId} to {recipientId} in ride {rideId}");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error sending message");
                await Clients.Caller.SendAsync("ReceiveError", ex.Message);
            }
        }

        /// <summary>
        /// Load chat history for a ride
        /// </summary>
        public async Task LoadChatHistoryAsync(string rideId)
        {
            try
            {
                var userId = GetUserIdFromClaims();
                if (string.IsNullOrEmpty(userId))
                {
                    throw new HubException("Unauthorized");
                }

                if (!Guid.TryParse(rideId, out var rideGuid))
                {
                    throw new ArgumentException("Invalid ride ID");
                }

                // Get chat history
                var history = await _chatService.GetChatHistoryAsync(rideGuid, 50);

                // Send to client
                await Clients.Caller.SendAsync("ChatHistoryLoaded", rideId, history);

                _logger.LogInformation($"Chat history loaded for ride {rideId}");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error loading chat history");
                await Clients.Caller.SendAsync("ReceiveError", ex.Message);
            }
        }

        /// <summary>
        /// Mark a message as read
        /// </summary>
        public async Task MarkMessageAsReadAsync(string messageId)
        {
            try
            {
                if (!Guid.TryParse(messageId, out var msgGuid))
                {
                    throw new ArgumentException("Invalid message ID");
                }

                await _chatService.MarkAsReadAsync(msgGuid);

                await Clients.All.SendAsync("MessageRead", messageId, DateTime.UtcNow);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error marking message as read");
                await Clients.Caller.SendAsync("ReceiveError", ex.Message);
            }
        }

        /// <summary>
        /// Notify typing indicator
        /// </summary>
        public async Task NotifyTypingAsync(string rideId, string recipientId)
        {
            try
            {
                var userId = GetUserIdFromClaims();
                if (string.IsNullOrEmpty(userId))
                {
                    return;
                }

                // Send typing indicator to recipient
                await Clients.Group($"user:{recipientId}").SendAsync(
                    "UserTyping",
                    rideId,
                    userId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error notifying typing");
            }
        }

        /// <summary>
        /// Notify stopped typing
        /// </summary>
        public async Task NotifyStoppedTypingAsync(string rideId, string recipientId)
        {
            try
            {
                var userId = GetUserIdFromClaims();
                if (string.IsNullOrEmpty(userId))
                {
                    return;
                }

                // Send stopped typing to recipient
                await Clients.Group($"user:{recipientId}").SendAsync(
                    "UserStoppedTyping",
                    rideId,
                    userId);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error notifying stopped typing");
            }
        }

        /// <summary>
        /// Extract user ID from JWT claims
        /// </summary>
        private string GetUserIdFromClaims()
        {
            var userId = Context.User?.FindFirst("sub")?.Value ??
                         Context.User?.FindFirst("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier")?.Value ??
                         Context.User?.FindFirst("userId")?.Value;

            return userId;
        }
    }
}
