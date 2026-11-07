using System.Text;
using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Domain;
using Amazon.SimpleNotificationService;
using Amazon.SimpleNotificationService.Model;

namespace AiChatinator.Infrastructure;

public sealed class SnsDomainEventPublisher(
    IAmazonSimpleNotificationService client,
    string topicArn) : IDomainEventPublisher
{
    private const int MaximumMessageBytes = 256 * 1024;
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    public async Task PublishAsync(
        DomainEventEnvelope domainEvent,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var message = JsonSerializer.Serialize(domainEvent, JsonOptions);
        if (Encoding.UTF8.GetByteCount(message) > MaximumMessageBytes)
        {
            throw new InvalidOperationException("The serialized domain event exceeds the SNS message size limit.");
        }

        var response = await client.PublishAsync(new PublishRequest
        {
            TopicArn = topicArn,
            Message = message,
            MessageAttributes = new Dictionary<string, MessageAttributeValue>
            {
                ["eventId"] = new() { DataType = "String", StringValue = domainEvent.EventId },
                ["eventType"] = new() { DataType = "String", StringValue = domainEvent.EventType },
                ["eventVersion"] = new() { DataType = "Number", StringValue = domainEvent.EventVersion.ToString() }
            }
        }, cancellationToken);

        if (string.IsNullOrWhiteSpace(response.MessageId))
        {
            throw new InvalidOperationException("SNS did not return a message ID for the published domain event.");
        }
    }
}
