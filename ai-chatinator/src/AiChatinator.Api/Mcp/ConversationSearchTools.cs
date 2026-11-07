using System.ComponentModel;
using System.Security.Claims;
using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Infrastructure;
using ModelContextProtocol.Server;

namespace AiChatinator.Api.Mcp;

[McpServerToolType]
public sealed class ConversationSearchTools(
    IHttpContextAccessor accessor,
    LocalChatToolCatalog searches,
    IChatStore store)
{
    [McpServerTool(Name = "list_conversation_ids")]
    [Description("List ALL conversation IDs owned by the authenticated user, including archived conversations, in pages of 20. Start with offset=0; follow nextOffset until null. For investigations in other chats, exclude the active conversation ID.")]
    public async Task<string> ListConversationIds(CancellationToken cancellationToken, int offset = 0)
    {
        var (userId, currentId) = GetIdentity();
        if (offset < 0) throw new InvalidDataException("offset must be non-negative.");
        _ = await store.ListMessagesAsync(userId, currentId, cancellationToken)
            ?? throw new KeyNotFoundException("Conversation not found.");
        var conversations = (await store.ListConversationsAsync(userId, cancellationToken))
            .OrderBy(conversation => conversation.CreatedAt)
            .ThenBy(conversation => conversation.Id, StringComparer.Ordinal).ToArray();
        if (offset > conversations.Length) throw new InvalidDataException("offset exceeds the conversation count.");
        var ids = conversations.Skip(offset).Take(20).Select(conversation => conversation.Id).ToArray();
        return JsonSerializer.Serialize(new
        {
            conversationIds = ids,
            currentConversationId = currentId,
            total = conversations.Length,
            nextOffset = offset + ids.Length < conversations.Length ? (int?)(offset + ids.Length) : null
        });
    }

    [McpServerTool(Name = "get_conversation_messages")]
    [Description("Read ALL messages from an owned conversation ID, in chronological pages. Start with messageOffset=0 and contentOffset=0. Follow nextMessageOffset AND nextContentOffset until both are null. Long messages are split into exact content fragments, not summaries; isComplete indicates the end of that message. To find an unspecified number in another chat, first list_conversation_ids, then investigate each other conversation until found or all pages have been read.")]
    public async Task<string> GetConversationMessages(
        string conversationId,
        CancellationToken cancellationToken,
        int messageOffset = 0,
        int contentOffset = 0)
    {
        var (userId, _) = GetIdentity();
        if (string.IsNullOrWhiteSpace(conversationId) || conversationId.Length > 64
            || messageOffset < 0 || contentOffset < 0)
        {
            throw new InvalidDataException("Provide a valid conversationId and non-negative offsets.");
        }
        var messages = await store.ListMessagesAsync(userId, conversationId, cancellationToken)
            ?? throw new KeyNotFoundException("Conversation not found.");
        if (messageOffset > messages.Count
            || (messageOffset == messages.Count && contentOffset != 0)
            || (messageOffset < messages.Count && contentOffset > messages[messageOffset].Content.Length))
        {
            throw new InvalidDataException("The message cursor is out of range.");
        }
        var fragments = new List<object>();
        var budget = 3_000;
        var index = messageOffset;
        var start = contentOffset;
        while (index < messages.Count && budget > 0 && fragments.Count < 10)
        {
            var message = messages[index];
            var length = Math.Min(budget, message.Content.Length - start);
            var complete = start + length == message.Content.Length;
            fragments.Add(new
            {
                messageId = message.Id,
                role = message.Role,
                createdAt = message.CreatedAt,
                content = message.Content.Substring(start, length),
                contentOffset = start,
                isComplete = complete
            });
            budget -= length;
            start += length;
            if (complete) { index++; start = 0; }
        }
        return JsonSerializer.Serialize(new
        {
            conversationId,
            messages = fragments,
            totalMessages = messages.Count,
            nextMessageOffset = index < messages.Count ? (int?)index : null,
            nextContentOffset = index < messages.Count ? (int?)start : null
        });
    }

    [McpServerTool(Name = "search_conversation")]
    [Description("Search earlier messages in the current conversation for a phrase. Returns up to five excerpts.")]
    public Task<string> SearchConversation(string query, CancellationToken cancellationToken) =>
        SearchAsync("search_conversation", query, cancellationToken);

    [McpServerTool(Name = "search_other_conversations")]
    [Description("Search the authenticated user's other conversations, including archived chats, for a phrase. Excludes the current conversation. Returns up to five recent excerpts with source identifiers.")]
    public Task<string> SearchOtherConversations(string query, CancellationToken cancellationToken) =>
        SearchAsync("search_other_conversations", query, cancellationToken);

    private Task<string> SearchAsync(string name, string query, CancellationToken cancellationToken)
    {
        var (userId, conversationId) = GetIdentity();
        return searches.ExecuteAsync(userId, conversationId,
            new ChatToolCall(Guid.NewGuid().ToString("N"), name, JsonSerializer.Serialize(new { query })),
            cancellationToken);
    }

    private (string UserId, string ConversationId) GetIdentity()
    {
        var context = accessor.HttpContext
            ?? throw new InvalidOperationException("Conversation tools require an authenticated HTTP request.");
        var userId = context.User.FindFirstValue(ClaimTypes.NameIdentifier)
            ?? context.User.FindFirstValue("sub");
        if (context.User.Identity?.IsAuthenticated != true || string.IsNullOrWhiteSpace(userId))
        {
            throw new UnauthorizedAccessException("Authentication is required.");
        }

        var conversationId = context.Request.Headers["X-Conversation-Id"].ToString();
        if (string.IsNullOrWhiteSpace(conversationId) || conversationId.Length > 64)
        {
            throw new InvalidDataException("Provide a valid X-Conversation-Id header.");
        }

        return (userId, conversationId);
    }
}
