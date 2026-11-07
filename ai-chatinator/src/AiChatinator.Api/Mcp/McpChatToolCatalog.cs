using System.Security.Claims;
using System.Text.Json;
using AiChatinator.Application;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

namespace AiChatinator.Api.Mcp;

public sealed class McpChatToolCatalog(
    IHttpContextAccessor accessor,
    ILogger<McpChatToolCatalog> logger) : IChatToolCatalog, IAsyncDisposable
{
    private McpClient? _client;
    public IReadOnlyList<ChatToolDefinition> Definitions =>
        throw new NotSupportedException("MCP tool discovery is asynchronous. Use GetDefinitionsAsync.");

    public async Task<IReadOnlyList<ChatToolDefinition>> GetDefinitionsAsync(
        CancellationToken cancellationToken)
    {
        var client = _client ??= await ConnectAsync(cancellationToken);
        var tools = await client.ListToolsAsync(cancellationToken: cancellationToken);
        return tools.Where(tool => IsAllowed(tool.Name))
            .Select(tool => new ChatToolDefinition(
                tool.Name, tool.Description ?? tool.Name, tool.JsonSchema.GetRawText()))
            .ToArray();
    }

    public async Task<string> ExecuteAsync(
        string userId,
        string conversationId,
        ChatToolCall call,
        CancellationToken cancellationToken)
    {
        var context = GetContext();
        var authenticatedUser = context.User.FindFirstValue(ClaimTypes.NameIdentifier)
            ?? context.User.FindFirstValue("sub");
        if (userId != authenticatedUser
            || conversationId != context.Request.RouteValues["conversationId"]?.ToString())
        {
            throw new UnauthorizedAccessException("Tool context does not match the authenticated chat request.");
        }
        if (!IsAllowed(call.Name))
        {
            throw new InvalidDataException("The requested MCP tool is not allowed.");
        }

        Dictionary<string, object?> arguments;
        try
        {
            arguments = JsonSerializer.Deserialize<Dictionary<string, object?>>(call.ArgumentsJson)
                ?? throw new InvalidDataException("Tool arguments must be a JSON object.");
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("Tool arguments must be a valid JSON object.", exception);
        }
        var client = _client ??= await ConnectAsync(cancellationToken);
        ModelContextProtocol.Protocol.CallToolResult result;
        try
        {
            result = await client.CallToolAsync(call.Name, arguments, cancellationToken: cancellationToken);
        }
        catch (HttpRequestException exception) when (exception.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
        {
            throw new ToolExecutionLimitException(
                "Investigation incomplete: the local MCP request rate limit was reached. Wait a minute before retrying.");
        }
        if (result.IsError == true)
        {
            throw new InvalidDataException("MCP rejected the conversation search request.");
        }
        var text = result.Content.OfType<TextContentBlock>().Select(block => block.Text).ToArray();
        if (text.Length == 0)
        {
            throw new InvalidDataException("MCP returned no text result.");
        }
        return string.Join("\n", text);
    }

    private async Task<McpClient> ConnectAsync(CancellationToken cancellationToken)
    {
        var context = GetContext();
        // Use the actual listening address, never the client-controlled Host header.
        var address = context.Connection.LocalIpAddress
            ?? throw new InvalidOperationException("The local MCP listening address is unavailable.");
        // Local listeners can require the localhost hostname rather than an IP-literal Host header.
        var host = System.Net.IPAddress.IsLoopback(address) ? "localhost" : address.ToString();
        var endpoint = new UriBuilder(context.Request.Scheme, host,
            context.Connection.LocalPort, "/mcp").Uri;
        var headers = new Dictionary<string, string>
        {
            ["X-Conversation-Id"] = context.Request.RouteValues["conversationId"]?.ToString()
                ?? throw new InvalidDataException("The current conversation is required.")
        };
        foreach (var name in new[] { "Cookie", "Authorization" })
        {
            if (context.Request.Headers.TryGetValue(name, out var value))
            {
                headers[name] = value.ToString();
            }
        }
        var httpClient = new HttpClient(new HttpClientHandler
        {
            AllowAutoRedirect = false,
            UseCookies = false
        });
        var transport = new HttpClientTransport(new HttpClientTransportOptions
        {
            Endpoint = endpoint,
            AdditionalHeaders = headers
        }, httpClient, ownsHttpClient: true);
        try
        {
            return await McpClient.CreateAsync(transport, cancellationToken: cancellationToken);
        }
        catch (HttpRequestException exception)
        {
            logger.LogWarning("Local MCP connection failed: endpoint={Endpoint}, HTTP={StatusCode}",
                endpoint, exception.StatusCode);
            await transport.DisposeAsync();
            if (exception.StatusCode == System.Net.HttpStatusCode.TooManyRequests)
            {
                throw new ToolExecutionLimitException(
                    "Investigation incomplete: the local MCP request rate limit was reached. Wait a minute before retrying.");
            }
            throw;
        }
        catch
        {
            await transport.DisposeAsync();
            throw;
        }
    }

    private HttpContext GetContext() => accessor.HttpContext
        ?? throw new InvalidOperationException("MCP tools require an active HTTP request.");

    public async ValueTask DisposeAsync()
    {
        if (_client is not null) await _client.DisposeAsync();
    }

    private static bool IsAllowed(string name) =>
        name is "search_conversation" or "search_other_conversations"
            or "list_conversation_ids" or "get_conversation_messages";
}
