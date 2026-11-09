using System.ComponentModel;
using System.Security.Claims;
using ModelContextProtocol.Server;

namespace AiChatinator.Api.Mcp;

[McpServerToolType]
public sealed class ConversationSearchTools(IHttpContextAccessor accessor, ConversationTools tools)
{
    [McpServerTool(Name = "list_conversation_ids")]
    [Description(ConversationTools.ListConversationIdsDescription)]
    public Task<string> ListConversationIds(CancellationToken cancellationToken, int offset = 0)
    {
        var (userId, currentId) = GetIdentity();
        return tools.ListConversationIdsAsync(userId, currentId, offset, cancellationToken);
    }

    [McpServerTool(Name = "get_conversation_messages")]
    [Description(ConversationTools.GetConversationMessagesDescription)]
    public Task<string> GetConversationMessages(
        string conversationId,
        CancellationToken cancellationToken,
        int messageOffset = 0,
        int contentOffset = 0)
    {
        var (userId, _) = GetIdentity();
        return tools.GetConversationMessagesAsync(userId, conversationId, messageOffset, contentOffset, cancellationToken);
    }

    [McpServerTool(Name = "search_conversation")]
    [Description(ConversationTools.SearchConversationDescription)]
    public Task<string> SearchConversation(string query, CancellationToken cancellationToken) =>
        SearchAsync("search_conversation", query, cancellationToken);

    [McpServerTool(Name = "search_other_conversations")]
    [Description(ConversationTools.SearchOtherConversationsDescription)]
    public Task<string> SearchOtherConversations(string query, CancellationToken cancellationToken) =>
        SearchAsync("search_other_conversations", query, cancellationToken);

    private Task<string> SearchAsync(string name, string query, CancellationToken cancellationToken)
    {
        var (userId, conversationId) = GetIdentity();
        return tools.SearchAsync(userId, conversationId, name, query, cancellationToken);
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
