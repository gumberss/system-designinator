using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure.Messaging;

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
    ILogger<OutboxDispatcher> logger) : BackgroundService
{
    private const int BatchSize = 20;
    private static readonly TimeSpan IdleDelay = TimeSpan.FromSeconds(2);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            OutboxRelayResult result;
            try
            {
                using var scope = scopeFactory.CreateScope();
                var relay = scope.ServiceProvider.GetRequiredService<OutboxRelay>();
                result = await relay.RelayPendingAsync(BatchSize, stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Outbox relay pass failed.");
                await Task.Delay(TimeSpan.FromSeconds(5), stoppingToken);
                continue;
            }

            if (result.Failed > 0)
            {
                var retrySeconds = Math.Min(60, Math.Pow(2, Math.Min(result.MaxFailedAttempts, 6)));
                await Task.Delay(TimeSpan.FromSeconds(retrySeconds), stoppingToken);
            }
            else if (result.Dispatched == 0)
            {
                await Task.Delay(IdleDelay, stoppingToken);
            }
        }
    }
}
