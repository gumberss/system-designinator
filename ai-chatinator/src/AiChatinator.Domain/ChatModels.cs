namespace AiChatinator.Domain;

public sealed record LocalUser(
    string Id,
    string Email,
    string PasswordHash,
    int Credits,
    DateTimeOffset CreatedAt);

public sealed record Conversation(
    string Id,
    string OwnerId,
    string Title,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt,
    bool IsArchived,
    string? ShareTokenHash);

public sealed record ChatMessage(
    string Id,
    string ConversationId,
    string Role,
    string Content,
    DateTimeOffset CreatedAt);

public sealed record UsageEntry(
    string IdempotencyKey,
    string UserId,
    string ConversationId,
    string RequestFingerprint,
    string UserMessageId,
    string AssistantMessageId,
    int CreditsCharged,
    int InputTokens,
    int OutputTokens,
    string Provider,
    string Model,
    DateTimeOffset CreatedAt);

public sealed record AuditEntry(
    string EventId,
    string CorrelationId,
    string EventType,
    string ActorId,
    string ResourceId,
    DateTimeOffset OccurredAt);

public sealed record ConversationSummary(
    string Id,
    string Title,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt,
    bool IsArchived);

public sealed record ConversationMessageMatch(
    string ConversationId,
    string ConversationTitle,
    string MessageId,
    string Role,
    string Excerpt,
    DateTimeOffset CreatedAt);

public sealed record SharedConversation(
    string Title,
    IReadOnlyList<ChatMessage> Messages);

public sealed record ChatTurnResult(
    Conversation Conversation,
    ChatMessage UserMessage,
    ChatMessage AssistantMessage,
    int CreditsRemaining,
    int CreditsCharged,
    int InputTokens,
    int OutputTokens,
    string Provider,
    string Model);

public sealed record UsageHistoryItem(
    string ConversationId,
    int CreditsCharged,
    int InputTokens,
    int OutputTokens,
    string Provider,
    string Model,
    DateTimeOffset CreatedAt);

public sealed record ContextSummary(
    string ConversationId,
    int Version,
    string ThroughMessageId,
    string Content,
    string Model,
    DateTimeOffset CreatedAt);

public sealed record DomainEventEnvelope(
    string EventId,
    string EventType,
    int EventVersion,
    DateTimeOffset OccurredAt,
    string Producer,
    string CorrelationId,
    string AggregateId,
    string IdempotencyKey,
    string DataJson);

public sealed record OutboxRecord(
    DomainEventEnvelope Event,
    int Attempts,
    DateTimeOffset? DispatchedAt,
    string? LastError);

public sealed record ConversationFile(
    string Id,
    string OwnerId,
    string ConversationId,
    string FileName,
    string ContentType,
    long SizeBytes,
    string Sha256,
    string ExtractedText,
    DateTimeOffset CreatedAt);

public sealed class InsufficientCreditsException : Exception
{
}

public sealed class IdempotencyConflictException : Exception
{
}
