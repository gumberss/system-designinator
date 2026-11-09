using AiChatinator.Infrastructure.Messaging;
using Amazon.SQS;
using Amazon.SQS.Model;

namespace AiChatinator.Api.Worker;

public sealed record SqsWorkerOptions(
    string QueueUrl,
    int MaxMessages = 10,
    int WaitTimeSeconds = 20)
{
    public void Validate()
    {
        if (!Uri.TryCreate(QueueUrl, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttps && uri.Scheme != Uri.UriSchemeHttp))
        {
            throw new InvalidOperationException("Worker:QueueUrl must be an absolute SQS queue URL.");
        }

        if (MaxMessages is < 1 or > 10)
        {
            throw new InvalidOperationException("Worker:MaxMessages must be between 1 and 10.");
        }

        if (WaitTimeSeconds is < 0 or > 20)
        {
            throw new InvalidOperationException("Worker:WaitTimeSeconds must be between 0 and 20.");
        }
    }
}

/// <summary>
/// Receives domain events from SQS and dispatches them to handlers. A message is deleted only
/// after it was handled (or deliberately ignored); failures stay on the queue so SQS redelivers
/// them after the visibility timeout and finally moves them to the dead-letter queue.
/// </summary>
public sealed class SqsDomainEventWorker(
    IAmazonSQS sqs,
    SqsWorkerOptions options,
    IServiceScopeFactory scopeFactory,
    ILogger<SqsDomainEventWorker> logger) : BackgroundService
{
    private static readonly TimeSpan ErrorDelay = TimeSpan.FromSeconds(5);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await PollOnceAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Failed to receive messages from the worker queue.");
                await Task.Delay(ErrorDelay, stoppingToken);
            }
        }
    }

    /// <summary>Receives and processes one batch. Returns the number of messages deleted.</summary>
    public async Task<int> PollOnceAsync(CancellationToken cancellationToken)
    {
        var response = await sqs.ReceiveMessageAsync(new ReceiveMessageRequest
        {
            QueueUrl = options.QueueUrl,
            MaxNumberOfMessages = options.MaxMessages,
            WaitTimeSeconds = options.WaitTimeSeconds
        }, cancellationToken);

        var messages = response.Messages ?? [];
        if (messages.Count == 0)
        {
            return 0;
        }

        using var scope = scopeFactory.CreateScope();
        var processor = scope.ServiceProvider.GetRequiredService<DomainEventMessageProcessor>();
        var deleted = 0;
        foreach (var message in messages)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var outcome = await processor.ProcessAsync(message.Body, cancellationToken);
                if (outcome == DomainEventProcessingOutcome.Ignored)
                {
                    logger.LogDebug(
                        "Ignored queue message {MessageId}: no handler for its event type.",
                        message.MessageId);
                }

                await sqs.DeleteMessageAsync(options.QueueUrl, message.ReceiptHandle, cancellationToken);
                deleted++;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception)
            {
                logger.LogError(
                    exception,
                    "Failed to process queue message {MessageId}; it will be redelivered or dead-lettered.",
                    message.MessageId);
            }
        }

        return deleted;
    }
}
