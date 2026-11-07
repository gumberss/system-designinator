using AiChatinator.Application;
using AiChatinator.Domain;

namespace AiChatinator.Api.Outbox;

public sealed class LocalDomainEventPublisher(ILogger<LocalDomainEventPublisher> logger)
    : IDomainEventPublisher
{
    public Task PublishAsync(
        DomainEventEnvelope domainEvent,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        logger.LogInformation(
            "Local domain event dispatched: {EventType} v{EventVersion}, event {EventId}, aggregate {AggregateId}",
            domainEvent.EventType,
            domainEvent.EventVersion,
            domainEvent.EventId,
            domainEvent.AggregateId);
        return Task.CompletedTask;
    }
}

public sealed class OutboxDispatcher(
    IServiceScopeFactory scopeFactory,
    IDomainEventPublisher publisher,
    ILogger<OutboxDispatcher> logger) : BackgroundService
{
    private static readonly TimeSpan IdleDelay = TimeSpan.FromSeconds(2);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            using var scope = scopeFactory.CreateScope();
            var store = scope.ServiceProvider.GetRequiredService<IOutboxStore>();
            var pendingEvents = await store.GetPendingEventsAsync(20, stoppingToken);
            if (pendingEvents.Count == 0)
            {
                await Task.Delay(IdleDelay, stoppingToken);
                continue;
            }

            foreach (var record in pendingEvents)
            {
                stoppingToken.ThrowIfCancellationRequested();
                try
                {
                    await publisher.PublishAsync(record.Event, stoppingToken);
                    await store.MarkEventDispatchedAsync(record.Event.EventId, stoppingToken);
                }
                catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception exception)
                {
                    await store.MarkEventDispatchFailedAsync(
                        record.Event.EventId,
                        exception.GetType().Name,
                        stoppingToken);
                    logger.LogError(
                        exception,
                        "Failed to dispatch outbox event {EventId} ({EventType}); it remains pending for retry.",
                        record.Event.EventId,
                        record.Event.EventType);

                    var retrySeconds = Math.Min(60, Math.Pow(2, Math.Min(record.Attempts, 6)));
                    await Task.Delay(TimeSpan.FromSeconds(retrySeconds), stoppingToken);
                }
            }
        }
    }
}
