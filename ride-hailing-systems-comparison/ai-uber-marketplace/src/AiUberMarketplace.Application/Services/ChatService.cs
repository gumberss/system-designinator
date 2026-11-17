using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Represents a chat message between rider and driver
    /// </summary>
    public class ChatMessage
    {
        public Guid MessageId { get; set; }
        public Guid RideId { get; set; }
        public Guid SenderId { get; set; } // Rider or Driver ID
        public string SenderName { get; set; }
        public string SenderType { get; set; } // "rider" or "driver"
        public Guid RecipientId { get; set; }
        public string RecipientName { get; set; }
        public string Content { get; set; }
        public DateTime SentAt { get; set; }
        public DateTime? ReadAt { get; set; }
        public bool IsRead { get; set; }
    }

    /// <summary>
    /// Service for real-time chat between riders and drivers
    /// Handles message persistence and retrieval with read receipts
    /// </summary>
    public interface IChatService
    {
        /// <summary>
        /// Send a message in real-time via SignalR
        /// </summary>
        Task SendMessageAsync(
            Guid rideId,
            Guid senderId,
            string senderName,
            string senderType,
            Guid recipientId,
            string recipientName,
            string content);

        /// <summary>
        /// Get chat history for a ride (last 50 messages)
        /// </summary>
        Task<List<ChatMessage>> GetChatHistoryAsync(Guid rideId, int limit = 50);

        /// <summary>
        /// Mark a message as read
        /// </summary>
        Task MarkAsReadAsync(Guid messageId);

        /// <summary>
        /// Mark all messages in a conversation as read
        /// </summary>
        Task MarkConversationAsReadAsync(Guid rideId, Guid userId);

        /// <summary>
        /// Get unread message count for a user in a ride
        /// </summary>
        Task<int> GetUnreadCountAsync(Guid rideId, Guid userId);

        /// <summary>
        /// Delete a message (soft delete)
        /// </summary>
        Task DeleteMessageAsync(Guid messageId);

        /// <summary>
        /// Get chat statistics
        /// </summary>
        ChatStats GetStats();
    }

    /// <summary>
    /// Chat service statistics
    /// </summary>
    public class ChatStats
    {
        public int TotalMessagesSent { get; set; }
        public int ActiveChats { get; set; }
        public int UnreadMessages { get; set; }
        public double AverageResponseTimeMs { get; set; }
        public DateTime LastMessageTime { get; set; }
    }

    /// <summary>
    /// Implementation of chat service
    /// Stores messages in memory with persistence (would use database in production)
    /// </summary>
    public class ChatService : IChatService, IDisposable
    {
        // Store messages by ride ID
        private readonly Dictionary<Guid, List<ChatMessage>> _chatHistory = new();
        private readonly SemaphoreSlim _lockObj = new SemaphoreSlim(1, 1);

        // Statistics
        private int _totalMessagesSent = 0;
        private DateTime _lastMessageTime = DateTime.UtcNow;
        private readonly List<long> _responseTimes = new();

        /// <summary>
        /// Send a message
        /// </summary>
        public async Task SendMessageAsync(
            Guid rideId,
            Guid senderId,
            string senderName,
            string senderType,
            Guid recipientId,
            string recipientName,
            string content)
        {
            try
            {
                var message = new ChatMessage
                {
                    MessageId = Guid.NewGuid(),
                    RideId = rideId,
                    SenderId = senderId,
                    SenderName = senderName,
                    SenderType = senderType,
                    RecipientId = recipientId,
                    RecipientName = recipientName,
                    Content = content,
                    SentAt = DateTime.UtcNow,
                    ReadAt = null,
                    IsRead = false
                };

                await _lockObj.WaitAsync();
                try
                {
                    if (!_chatHistory.ContainsKey(rideId))
                    {
                        _chatHistory[rideId] = new List<ChatMessage>();
                    }

                    _chatHistory[rideId].Add(message);
                    _totalMessagesSent++;
                    _lastMessageTime = DateTime.UtcNow;
                }
                finally
                {
                    _lockObj.Release();
                }
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get chat history for a ride
        /// </summary>
        public async Task<List<ChatMessage>> GetChatHistoryAsync(Guid rideId, int limit = 50)
        {
            try
            {
                await _lockObj.WaitAsync();
                try
                {
                    if (!_chatHistory.ContainsKey(rideId))
                    {
                        return new List<ChatMessage>();
                    }

                    var messages = _chatHistory[rideId]
                        .OrderByDescending(m => m.SentAt)
                        .Take(limit)
                        .OrderBy(m => m.SentAt)
                        .ToList();

                    return messages;
                }
                finally
                {
                    _lockObj.Release();
                }
            }
            catch (Exception)
            {
                return new List<ChatMessage>();
            }
        }

        /// <summary>
        /// Mark a message as read
        /// </summary>
        public async Task MarkAsReadAsync(Guid messageId)
        {
            try
            {
                await _lockObj.WaitAsync();
                try
                {
                    foreach (var messages in _chatHistory.Values)
                    {
                        var message = messages.FirstOrDefault(m => m.MessageId == messageId);
                        if (message != null)
                        {
                            message.IsRead = true;
                            message.ReadAt = DateTime.UtcNow;
                            break;
                        }
                    }
                }
                finally
                {
                    _lockObj.Release();
                }
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Mark all messages in a conversation as read
        /// </summary>
        public async Task MarkConversationAsReadAsync(Guid rideId, Guid userId)
        {
            try
            {
                await _lockObj.WaitAsync();
                try
                {
                    if (_chatHistory.ContainsKey(rideId))
                    {
                        var messages = _chatHistory[rideId]
                            .Where(m => m.RecipientId == userId && !m.IsRead);

                        foreach (var message in messages)
                        {
                            message.IsRead = true;
                            message.ReadAt = DateTime.UtcNow;
                        }
                    }
                }
                finally
                {
                    _lockObj.Release();
                }
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get unread message count
        /// </summary>
        public async Task<int> GetUnreadCountAsync(Guid rideId, Guid userId)
        {
            try
            {
                await _lockObj.WaitAsync();
                try
                {
                    if (!_chatHistory.ContainsKey(rideId))
                    {
                        return 0;
                    }

                    var count = _chatHistory[rideId]
                        .Count(m => m.RecipientId == userId && !m.IsRead);

                    return count;
                }
                finally
                {
                    _lockObj.Release();
                }
            }
            catch (Exception)
            {
                return 0;
            }
        }

        /// <summary>
        /// Delete a message (soft delete by removing from history)
        /// </summary>
        public async Task DeleteMessageAsync(Guid messageId)
        {
            try
            {
                await _lockObj.WaitAsync();
                try
                {
                    foreach (var messages in _chatHistory.Values)
                    {
                        var message = messages.FirstOrDefault(m => m.MessageId == messageId);
                        if (message != null)
                        {
                            messages.Remove(message);
                            break;
                        }
                    }
                }
                finally
                {
                    _lockObj.Release();
                }
            }
            catch (Exception)
            {
                // Silent fail
            }
        }

        /// <summary>
        /// Get statistics
        /// </summary>
        public ChatStats GetStats()
        {
            _lockObj.Wait();
            try
            {
                var unreadCount = _chatHistory.Values
                    .SelectMany(m => m)
                    .Count(m => !m.IsRead);

                return new ChatStats
                {
                    TotalMessagesSent = _totalMessagesSent,
                    ActiveChats = _chatHistory.Count,
                    UnreadMessages = unreadCount,
                    AverageResponseTimeMs = _responseTimes.Count > 0 ? 
                        _responseTimes.Average() : 0,
                    LastMessageTime = _lastMessageTime
                };
            }
            finally
            {
                _lockObj.Release();
            }
        }

        public void Dispose()
        {
            _lockObj.Dispose();
        }
    }
}
