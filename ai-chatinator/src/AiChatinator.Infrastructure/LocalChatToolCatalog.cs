using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Domain;

namespace AiChatinator.Infrastructure;

public sealed class LocalChatToolCatalog(IChatStore store) : IChatToolCatalog
{
    private static readonly ChatToolDefinition[] ToolDefinitions =
    [
        new(
            "search_conversation",
            "Search earlier messages in the current conversation for a phrase. Returns at most five short excerpts.",
            """{"type":"object","properties":{"query":{"type":"string","minLength":1,"maxLength":160}},"required":["query"],"additionalProperties":false}"""),
        new(
            "search_other_conversations",
            "Search messages in the current user's other conversations, including archived ones, for a phrase. Excludes the current conversation. Returns up to five recent matching excerpts with conversation and message identifiers. Use when the user asks about an earlier or different chat.",
            """{"type":"object","properties":{"query":{"type":"string","minLength":1,"maxLength":160}},"required":["query"],"additionalProperties":false}""")
    ];

    public IReadOnlyList<ChatToolDefinition> Definitions => ToolDefinitions;

    public async Task<string> ExecuteAsync(
        string userId,
        string conversationId,
        ChatToolCall call,
        CancellationToken cancellationToken)
    {
        if (call.Name is not ("search_conversation" or "search_other_conversations"))
        {
            throw new InvalidDataException($"Tool '{call.Name}' is not registered.");
        }

        JsonDocument parsedArguments;
        try
        {
            parsedArguments = JsonDocument.Parse(call.ArgumentsJson);
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("The conversation search arguments must be valid JSON.", exception);
        }
        using var arguments = parsedArguments;
        if (arguments.RootElement.ValueKind != JsonValueKind.Object
            || !arguments.RootElement.TryGetProperty("query", out var queryElement)
            || queryElement.ValueKind != JsonValueKind.String
            || arguments.RootElement.EnumerateObject().Count() != 1)
        {
            throw new InvalidDataException("The conversation search tool requires a string query.");
        }

        var query = queryElement.GetString()?.Trim();
        if (string.IsNullOrWhiteSpace(query) || query.Length > 160)
        {
            throw new InvalidDataException("The conversation search query must be 1 to 160 characters.");
        }

        if (call.Name == "search_other_conversations")
        {
            var otherMatches = await store.SearchOtherConversationMessagesAsync(
                userId, conversationId, query, cancellationToken);
            return JsonSerializer.Serialize(otherMatches);
        }

        var messages = await store.ListMessagesAsync(userId, conversationId, cancellationToken)
            ?? throw new KeyNotFoundException("Conversation not found.");
        var matches = messages
            .Where(message => message.Content.Contains(query, StringComparison.OrdinalIgnoreCase))
            .TakeLast(5)
            .Select(message => new
            {
                message.Role,
                Excerpt = message.Content.Length <= 500
                    ? message.Content
                    : $"{message.Content[..500]}..."
            });
        return JsonSerializer.Serialize(matches);
    }
}
