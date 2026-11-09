using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Infrastructure;

namespace AiChatinator.Api.Mcp;

/// <summary>
/// Identity-explicit conversation tools shared by the MCP server endpoint and the in-process
/// catalog used where the API cannot call its own /mcp endpoint (for example AWS Lambda).
/// </summary>
public sealed class ConversationTools(LocalChatToolCatalog searches, IChatStore store)
{
    public const string ListConversationIdsDescription = "List ALL conversation IDs owned by the authenticated user, including archived conversations, in pages of 20. Start with offset=0; follow nextOffset until null. For investigations in other chats, exclude the active conversation ID.";
    public const string GetConversationMessagesDescription = "Read ALL messages from an owned conversation ID, in chronological pages. Start with messageOffset=0 and contentOffset=0. Follow nextMessageOffset AND nextContentOffset until both are null. Long messages are split into exact content fragments, not summaries; isComplete indicates the end of that message. To find an unspecified number in another chat, first list_conversation_ids, then investigate each other conversation until found or all pages have been read.";
    public const string SearchConversationDescription = "Search earlier messages in the current conversation for a phrase. Returns up to five excerpts.";
    public const string SearchOtherConversationsDescription = "Search the authenticated user's other conversations, including archived chats, for a phrase. Excludes the current conversation. Returns up to five recent excerpts with source identifiers.";

    public async Task<string> ListConversationIdsAsync(
        string userId, string currentId, int offset, CancellationToken cancellationToken)
    {
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

    public async Task<string> GetConversationMessagesAsync(
        string userId,
        string conversationId,
        int messageOffset,
        int contentOffset,
        CancellationToken cancellationToken)
    {
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

    public Task<string> SearchAsync(
        string userId, string conversationId, string name, string query, CancellationToken cancellationToken) =>
        searches.ExecuteAsync(userId, conversationId,
            new ChatToolCall(Guid.NewGuid().ToString("N"), name, JsonSerializer.Serialize(new { query })),
            cancellationToken);
}
