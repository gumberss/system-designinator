using AiChatinator.Domain;

namespace AiChatinator.Application;

public interface IChatProvider
{
    Task<ChatCompletion> GetReplyAsync(
        IReadOnlyList<(string Role, string Content)> history,
        CancellationToken cancellationToken);

    Task<ChatCompletion> SummarizeAsync(
        IReadOnlyList<(string Role, string Content)> history,
        CancellationToken cancellationToken) =>
        Task.FromException<ChatCompletion>(
            new NotSupportedException("The configured provider does not support context summarization."));
}

public interface IChatToolProvider : IChatProvider
{
    Task<ChatCompletion> GetReplyWithToolsAsync(
        IReadOnlyList<ChatPromptMessage> history,
        IReadOnlyList<ChatToolDefinition> tools,
        CancellationToken cancellationToken);
}

public interface IStreamingChatProvider : IChatToolProvider
{
    IAsyncEnumerable<ChatProviderStreamEvent> StreamReplyWithToolsAsync(
        IReadOnlyList<ChatPromptMessage> history,
        IReadOnlyList<ChatToolDefinition> tools,
        CancellationToken cancellationToken);
}

public sealed record ChatProviderStreamEvent(
    string? ContentDelta = null,
    ChatCompletion? Completion = null);

public sealed record ChatStreamEvent(
    string Type,
    string? Content = null,
    ChatTurnResult? Result = null);

public sealed record ChatPromptMessage(
    string Role,
    string Content,
    string? Name = null,
    string? ToolCallId = null,
    IReadOnlyList<ChatToolCall>? ToolCalls = null);

public sealed record ChatToolDefinition(string Name, string Description, string ParametersJson);

public sealed record ChatToolCall(string Id, string Name, string ArgumentsJson);

public sealed record ChatCompletion(
    string Content,
    string Provider,
    string Model,
    int InputTokens,
    int OutputTokens,
    IReadOnlyList<ChatToolCall>? ToolCalls = null);

public interface IChatToolCatalog
{
    IReadOnlyList<ChatToolDefinition> Definitions { get; }
    Task<IReadOnlyList<ChatToolDefinition>> GetDefinitionsAsync(CancellationToken cancellationToken) =>
        Task.FromResult(Definitions);
    Task<string> ExecuteAsync(
        string userId,
        string conversationId,
        ChatToolCall call,
        CancellationToken cancellationToken);
}
