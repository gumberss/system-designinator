using AiChatinator.Domain;
using AiChatinator.Infrastructure;
using Amazon;
using Amazon.Runtime;
using Amazon.SimpleNotificationService;
using Amazon.SimpleNotificationService.Model;
using System.Text.Json;

namespace AiChatinator.Tests;

public sealed class SnsDomainEventPublisherTests
{
    [Fact]
    public async Task Publishes_versioned_event_with_routing_attributes()
    {
        using var client = new CapturingSnsClient();
        var publisher = new SnsDomainEventPublisher(client, "arn:aws:sns:us-east-1:123456789012:events");
        var domainEvent = new DomainEventEnvelope(
            "event-1",
            "MessageTurnCompleted",
            1,
            DateTimeOffset.UtcNow,
            "ai-chatinator",
            "trace-1",
            "conversation-1",
            "request-1",
            """{"userId":"user-1","messageId":"message-1"}""");

        await publisher.PublishAsync(domainEvent, CancellationToken.None);

        Assert.NotNull(client.LastRequest);
        Assert.Equal("arn:aws:sns:us-east-1:123456789012:events", client.LastRequest.TopicArn);
        Assert.Equal("event-1", client.LastRequest.MessageAttributes["eventId"].StringValue);
        Assert.Equal("MessageTurnCompleted", client.LastRequest.MessageAttributes["eventType"].StringValue);
        Assert.Equal("1", client.LastRequest.MessageAttributes["eventVersion"].StringValue);
        Assert.Equal(domainEvent, JsonSerializer.Deserialize<DomainEventEnvelope>(
            client.LastRequest.Message,
            new JsonSerializerOptions(JsonSerializerDefaults.Web)));
    }

    [Fact]
    public async Task Rejects_events_that_exceed_the_SNS_message_limit_before_publishing()
    {
        using var client = new CapturingSnsClient();
        var publisher = new SnsDomainEventPublisher(client, "arn:aws:sns:us-east-1:123456789012:events");
        var domainEvent = new DomainEventEnvelope(
            "event-2",
            "MessageTurnCompleted",
            1,
            DateTimeOffset.UtcNow,
            "ai-chatinator",
            "trace-2",
            "conversation-2",
            "request-2",
            new string('x', 256 * 1024));

        await Assert.ThrowsAsync<InvalidOperationException>(
            () => publisher.PublishAsync(domainEvent, CancellationToken.None));

        Assert.Null(client.LastRequest);
    }

    private sealed class CapturingSnsClient()
        : AmazonSimpleNotificationServiceClient(new AnonymousAWSCredentials(), RegionEndpoint.USEast1)
    {
        public PublishRequest? LastRequest { get; private set; }

        public override Task<PublishResponse> PublishAsync(
            PublishRequest request,
            CancellationToken cancellationToken = default)
        {
            LastRequest = request;
            return Task.FromResult(new PublishResponse { MessageId = "message-1" });
        }
    }
}
