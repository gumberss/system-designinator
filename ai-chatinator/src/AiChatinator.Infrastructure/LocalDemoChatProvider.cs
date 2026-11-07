using AiChatinator.Application;

namespace AiChatinator.Infrastructure;

public sealed class LocalDemoChatProvider : IStreamingChatProvider
{
    public Task<ChatCompletion> GetReplyAsync(
        IReadOnlyList<(string Role, string Content)> history,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var latestMessage = history.LastOrDefault(message => message.Role == "user").Content
            ?? string.Empty;
        var preview = latestMessage.Length > 600
            ? $"{latestMessage[..600]}..."
            : latestMessage;
        var response = $"I received your message: “{preview}”\n\n"
            + "This installation is running in local demo mode and does not generate AI answers. "
            + "The chat, account, conversation history, sharing, and local credit ledger are working; "
            + "connect a real model provider before using this for AI-generated answers.";
        return Task.FromResult(new ChatCompletion(
            response,
            "local-demo",
            "echo-v1",
            EstimateTokens(history.Sum(message => message.Content.Length)),
            EstimateTokens(response.Length)));
    }

    public Task<ChatCompletion> GetReplyWithToolsAsync(
        IReadOnlyList<ChatPromptMessage> history,
        IReadOnlyList<ChatToolDefinition> tools,
        CancellationToken cancellationToken) =>
        GetReplyAsync(
            history.Select(message => (message.Role, message.Content)).ToArray(),
            cancellationToken);

    public async IAsyncEnumerable<ChatProviderStreamEvent> StreamReplyWithToolsAsync(
        IReadOnlyList<ChatPromptMessage> history,
        IReadOnlyList<ChatToolDefinition> tools,
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken)
    {
        var completion = await GetReplyWithToolsAsync(history, tools, cancellationToken);
        const int chunkLength = 36;
        for (var index = 0; index < completion.Content.Length; index += chunkLength)
        {
            cancellationToken.ThrowIfCancellationRequested();
            yield return new ChatProviderStreamEvent(
                ContentDelta: completion.Content.Substring(
                    index,
                    Math.Min(chunkLength, completion.Content.Length - index)));
        }

        yield return new ChatProviderStreamEvent(Completion: completion);
    }

    private static int EstimateTokens(int characters) => Math.Max(1, (characters + 3) / 4);
}
