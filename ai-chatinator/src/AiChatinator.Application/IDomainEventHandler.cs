using AiChatinator.Domain;

namespace AiChatinator.Application;

/// <summary>
/// Consumes one domain event delivered by a queue. Handlers must be idempotent because queue
/// delivery is at-least-once.
/// </summary>
public interface IDomainEventHandler
{
    string EventType { get; }

    Task HandleAsync(DomainEventEnvelope domainEvent, CancellationToken cancellationToken);
}

public interface IUsageRollupStore
{
    /// <summary>
    /// Adds a completed turn to the owner's daily usage rollup exactly once per event id.
    /// Returns false when the event was already applied.
    /// </summary>
    Task<bool> ApplyTurnToDailyUsageAsync(
        string eventId,
        string userId,
        DateOnly day,
        int inputTokens,
        int outputTokens,
        int creditsCharged,
        CancellationToken cancellationToken);

    Task<DailyUsage?> GetDailyUsageAsync(
        string userId,
        DateOnly day,
        CancellationToken cancellationToken);
}

public sealed record DailyUsage(
    string UserId,
    DateOnly Day,
    int Turns,
    long InputTokens,
    long OutputTokens,
    long CreditsCharged);
