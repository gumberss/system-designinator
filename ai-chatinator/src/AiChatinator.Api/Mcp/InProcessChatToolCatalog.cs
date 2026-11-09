using System.Text.Json;
using AiChatinator.Application;

namespace AiChatinator.Api.Mcp;

/// <summary>
/// Exposes the same tools as the /mcp server without an HTTP hop. Used in AWS Lambda, where the
/// API has no listening socket to call itself. The model sees identical names, descriptions and
/// argument schemas; identity always comes from the authenticated chat request, never the model.
/// </summary>
public sealed class InProcessChatToolCatalog(ConversationTools tools) : IChatToolCatalog
{
    private const string QuerySchema = """{"type":"object","properties":{"query":{"type":"string","minLength":1,"maxLength":160}},"required":["query"],"additionalProperties":false}""";
    private const string ListSchema = """{"type":"object","properties":{"offset":{"type":"integer","minimum":0,"default":0}},"additionalProperties":false}""";
    private const string MessagesSchema = """{"type":"object","properties":{"conversationId":{"type":"string","minLength":1,"maxLength":64},"messageOffset":{"type":"integer","minimum":0,"default":0},"contentOffset":{"type":"integer","minimum":0,"default":0}},"required":["conversationId"],"additionalProperties":false}""";

    public IReadOnlyList<ChatToolDefinition> Definitions { get; } =
    [
        new("search_conversation", ConversationTools.SearchConversationDescription, QuerySchema),
        new("search_other_conversations", ConversationTools.SearchOtherConversationsDescription, QuerySchema),
        new("list_conversation_ids", ConversationTools.ListConversationIdsDescription, ListSchema),
        new("get_conversation_messages", ConversationTools.GetConversationMessagesDescription, MessagesSchema)
    ];

    public Task<IReadOnlyList<ChatToolDefinition>> GetDefinitionsAsync(CancellationToken cancellationToken) =>
        Task.FromResult(Definitions);

    public Task<string> ExecuteAsync(
        string userId, string conversationId, ChatToolCall call, CancellationToken cancellationToken)
    {
        JsonElement args;
        try
        {
            using var document = JsonDocument.Parse(call.ArgumentsJson);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
                throw new InvalidDataException("Tool arguments must be a JSON object.");
            args = document.RootElement.Clone();
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("Tool arguments must be a valid JSON object.", exception);
        }

        return call.Name switch
        {
            "search_conversation" or "search_other_conversations" =>
                tools.SearchAsync(userId, conversationId, call.Name, String(args, "query") ?? "", cancellationToken),
            "list_conversation_ids" =>
                tools.ListConversationIdsAsync(userId, conversationId, Int(args, "offset"), cancellationToken),
            "get_conversation_messages" =>
                tools.GetConversationMessagesAsync(userId, String(args, "conversationId") ?? "",
                    Int(args, "messageOffset"), Int(args, "contentOffset"), cancellationToken),
            _ => throw new InvalidDataException("The requested tool is not allowed.")
        };
    }

    private static string? String(JsonElement args, string name) =>
        args.TryGetProperty(name, out var value)
            ? value.ValueKind == JsonValueKind.String ? value.GetString()
                : throw new InvalidDataException($"{name} must be a string.")
            : null;

    private static int Int(JsonElement args, string name) =>
        !args.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null ? 0
            : value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number) ? number
            : throw new InvalidDataException($"{name} must be an integer.");
}
