using AiChatinator.Domain;

namespace AiChatinator.Application;

public interface IOutboxStore
{
    Task<IReadOnlyList<OutboxRecord>> GetPendingEventsAsync(
        int limit,
        CancellationToken cancellationToken);

    Task MarkEventDispatchedAsync(
        string eventId,
        CancellationToken cancellationToken);

    Task MarkEventDispatchFailedAsync(
        string eventId,
        string error,
        CancellationToken cancellationToken);
}

public interface IDomainEventPublisher
{
    Task PublishAsync(
        DomainEventEnvelope domainEvent,
        CancellationToken cancellationToken);
}
