using AiChatinator.Infrastructure.Messaging;
using Amazon.Lambda.Core;
using Microsoft.Extensions.DependencyInjection;

namespace AiChatinator.Worker;

/// <summary>
/// Scheduled Lambda (EventBridge): publishes pending outbox events to SNS. Replaces the
/// in-process OutboxDispatcher, which cannot run in a request-driven Lambda.
/// Handler string: AiChatinator.Worker::AiChatinator.Worker.OutboxRelayHandler::HandleAsync
/// </summary>
public sealed class OutboxRelayHandler
{
    private const int BatchSize = 100;
    private readonly OutboxRelay _relay;

    public OutboxRelayHandler()
        : this(WorkerServices.Build())
    {
    }

    public OutboxRelayHandler(IServiceProvider services)
    {
        _relay = services.GetService<OutboxRelay>()
            ?? throw new InvalidOperationException("DOMAIN_EVENTS_TOPIC_ARN is required for the outbox relay.");
    }

    public async Task<OutboxRelayResult> HandleAsync(ILambdaContext? context)
    {
        using var timeout = context is null
            ? new CancellationTokenSource()
            : new CancellationTokenSource(context.RemainingTime - TimeSpan.FromSeconds(2));
        var total = new OutboxRelayResult(0, 0, 0);
        while (!timeout.IsCancellationRequested)
        {
            var pass = await _relay.RelayPendingAsync(BatchSize, timeout.Token);
            total = new OutboxRelayResult(
                total.Dispatched + pass.Dispatched,
                total.Failed + pass.Failed,
                Math.Max(total.MaxFailedAttempts, pass.MaxFailedAttempts));
            // Stop on a partial batch or on failures; failed events wait for the next schedule.
            if (pass.Dispatched + pass.Failed < BatchSize || pass.Failed > 0)
            {
                break;
            }
        }

        return total;
    }
}
