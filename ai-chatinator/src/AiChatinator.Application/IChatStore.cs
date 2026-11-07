using AiChatinator.Domain;

namespace AiChatinator.Application;

public interface IChatStore : IConversationFileStore
{
    Task<LocalUser?> FindUserByEmailAsync(string email, CancellationToken cancellationToken);
    Task<LocalUser?> FindUserByIdAsync(string userId, CancellationToken cancellationToken);
    Task<bool> AddUserAsync(LocalUser user, CancellationToken cancellationToken);
    Task<IReadOnlyList<ConversationSummary>> ListConversationsAsync(
        string userId,
        CancellationToken cancellationToken);
    Task<ConversationSummary> CreateConversationAsync(
        string userId,
        CancellationToken cancellationToken);
    Task<IReadOnlyList<ConversationMessageMatch>> SearchOtherConversationMessagesAsync(
        string userId,
        string currentConversationId,
        string query,
        CancellationToken cancellationToken);
    Task<bool> SetConversationArchivedAsync(
        string userId,
        string conversationId,
        bool isArchived,
        CancellationToken cancellationToken);
    Task<IReadOnlyList<UsageHistoryItem>> GetUsageHistoryAsync(
        string userId,
        CancellationToken cancellationToken);
    Task<IReadOnlyList<ChatMessage>?> ListMessagesAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken);
    Task<ContextSummary?> GetContextSummaryAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken);
    Task<bool> SaveContextSummaryAsync(
        string userId,
        string conversationId,
        int expectedVersion,
        string throughMessageId,
        string content,
        string model,
        CancellationToken cancellationToken);
    Task<ChatTurnResult?> FindCompletedTurnAsync(
        string userId,
        string idempotencyKey,
        string conversationId,
        string requestFingerprint,
        CancellationToken cancellationToken);
    Task<ChatTurnResult> CompleteTurnAsync(
        string userId,
        string conversationId,
        string idempotencyKey,
        string requestFingerprint,
        string userContent,
        string assistantContent,
        int inputTokens,
        int outputTokens,
        string provider,
        string model,
        string correlationId,
        CancellationToken cancellationToken);
    Task<bool> DeleteConversationAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken);
    Task<bool> SetConversationShareAsync(
        string userId,
        string conversationId,
        string tokenHash,
        CancellationToken cancellationToken);
    Task<bool> RevokeConversationShareAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken);
    Task<SharedConversation?> GetSharedConversationAsync(
        string tokenHash,
        CancellationToken cancellationToken);
}
