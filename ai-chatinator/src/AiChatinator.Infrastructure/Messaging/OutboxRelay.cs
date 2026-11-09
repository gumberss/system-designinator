using AiChatinator.Application;
using Microsoft.Extensions.Logging;

namespace AiChatinator.Infrastructure.Messaging;

public sealed record OutboxRelayResult(int Dispatched, int Failed, int MaxFailedAttempts);

/// <summary>
/// One pass of the transactional outbox: publishes pending events and records the outcome.
/// Used by the in-process API dispatcher and by the scheduled worker Lambda.
/// </summary>
public sealed class OutboxRelay(
    IOutboxStore store,
    IDomainEventPublisher publisher,
    ILogger<OutboxRelay> logger)
{
    public async Task<OutboxRelayResult> RelayPendingAsync(int limit, CancellationToken cancellationToken)
    {
        var pendingEvents = await store.GetPendingEventsAsync(limit, cancellationToken);
        var dispatched = 0;
        var failed = 0;
        var maxFailedAttempts = 0;
        foreach (var record in pendingEvents)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                await publisher.PublishAsync(record.Event, cancellationToken);
                await store.MarkEventDispatchedAsync(record.Event.EventId, cancellationToken);
                dispatched++;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception)
            {
                failed++;
                maxFailedAttempts = Math.Max(maxFailedAttempts, record.Attempts + 1);
                logger.LogError(
                    exception,
                    "Failed to dispatch outbox event {EventId} ({EventType}); it remains pending for retry.",
                    record.Event.EventId,
                    record.Event.EventType);
                try
                {
                    await store.MarkEventDispatchFailedAsync(
                        record.Event.EventId,
                        exception.GetType().Name,
                        cancellationToken);
                }
                catch (Exception markException) when (markException is not OperationCanceledException)
                {
                    logger.LogError(markException, "Failed to record the dispatch failure for {EventId}.", record.Event.EventId);
                }
            }
        }

        return new OutboxRelayResult(dispatched, failed, maxFailedAttempts);
    }
}
