using AiChatinator.Infrastructure.Messaging;
using Amazon.Lambda.Core;
using Amazon.Lambda.Serialization.SystemTextJson;
using Amazon.Lambda.SQSEvents;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

[assembly: LambdaSerializer(typeof(DefaultLambdaJsonSerializer))]

namespace AiChatinator.Worker;

/// <summary>
/// SQS-triggered Lambda: handles domain events and reports per-message failures so only the
/// failed messages are retried (and eventually moved to the dead-letter queue).
/// Handler string: AiChatinator.Worker::AiChatinator.Worker.SqsHandler::HandleAsync
/// </summary>
public sealed class SqsHandler
{
    private readonly DomainEventMessageProcessor _processor;
    private readonly ILogger<SqsHandler> _logger;

    public SqsHandler()
        : this(WorkerServices.Build())
    {
    }

    public SqsHandler(IServiceProvider services)
    {
        _processor = services.GetRequiredService<DomainEventMessageProcessor>();
        _logger = services.GetRequiredService<ILogger<SqsHandler>>();
    }

    public async Task<SQSBatchResponse> HandleAsync(SQSEvent sqsEvent, ILambdaContext? context)
    {
        using var timeout = context is null
            ? new CancellationTokenSource()
            : new CancellationTokenSource(context.RemainingTime - TimeSpan.FromSeconds(2));
        var failures = new List<SQSBatchResponse.BatchItemFailure>();
        foreach (var message in sqsEvent.Records ?? [])
        {
            try
            {
                var outcome = await _processor.ProcessAsync(message.Body, timeout.Token);
                _logger.LogInformation("Message {MessageId} {Outcome}.", message.MessageId, outcome);
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "Message {MessageId} failed; it will be retried.", message.MessageId);
                failures.Add(new SQSBatchResponse.BatchItemFailure { ItemIdentifier = message.MessageId });
            }
        }

        return new SQSBatchResponse(failures);
    }
}
