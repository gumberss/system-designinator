using System.Text.Json;
using AiChatinator.Api.Outbox;
using AiChatinator.Api.Worker;
using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure.Messaging;
using Amazon;
using Amazon.Runtime;
using Amazon.SQS;
using Amazon.SQS.Model;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;

namespace AiChatinator.Tests;

public sealed class AwsMessagingTests
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    private static DomainEventEnvelope TurnEvent(
        string eventId = "event-1",
        string ownerId = "user-1",
        int input = 10,
        int output = 20,
        int credits = 3) => new(
        eventId,
        "MessageTurnCompleted",
        1,
        new DateTimeOffset(2025, 3, 4, 23, 30, 0, TimeSpan.FromHours(-3)),
        "ai-chatinator",
        "trace-1",
        "conversation-1",
        "request-1",
        JsonSerializer.Serialize(new
        {
            conversationId = "conversation-1",
            ownerId,
            inputTokens = input,
            outputTokens = output,
            creditsCharged = credits
        }, Json));

    [Fact]
    public void Parses_raw_and_sns_wrapped_envelopes()
    {
        var domainEvent = TurnEvent();
        var raw = JsonSerializer.Serialize(domainEvent, Json);
        var wrapped = JsonSerializer.Serialize(new { Type = "Notification", MessageId = "m", Message = raw });

        Assert.Equal(domainEvent, DomainEventMessageProcessor.Parse(raw));
        Assert.Equal(domainEvent, DomainEventMessageProcessor.Parse(wrapped));
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    [InlineData("{}")]
    [InlineData("""{"eventId":"e","eventType":""}""")]
    public void Rejects_malformed_bodies(string body)
    {
        Assert.Throws<InvalidDataException>(() => DomainEventMessageProcessor.Parse(body));
    }

    [Fact]
    public async Task Ignores_unknown_event_types_and_rejects_duplicate_handlers()
    {
        var store = new FakeRollupStore();
        var processor = new DomainEventMessageProcessor([new UsageRollupHandler(store)]);
        var other = TurnEvent() with { EventType = "ConversationShared" };

        var outcome = await processor.ProcessAsync(JsonSerializer.Serialize(other, Json), CancellationToken.None);

        Assert.Equal(DomainEventProcessingOutcome.Ignored, outcome);
        Assert.Empty(store.Applied);
        Assert.Throws<InvalidOperationException>(() => new DomainEventMessageProcessor(
            [new UsageRollupHandler(store), new UsageRollupHandler(store)]));
    }

    [Fact]
    public async Task Rollup_handler_applies_turn_on_the_utc_day()
    {
        var store = new FakeRollupStore();
        var processor = new DomainEventMessageProcessor([new UsageRollupHandler(store)]);

        var outcome = await processor.ProcessAsync(JsonSerializer.Serialize(TurnEvent(), Json), CancellationToken.None);

        Assert.Equal(DomainEventProcessingOutcome.Handled, outcome);
        var applied = Assert.Single(store.Applied);
        Assert.Equal(("event-1", "user-1", new DateOnly(2025, 3, 5), 10, 20, 3), applied);
    }

    [Fact]
    public async Task Rollup_handler_rejects_invalid_data()
    {
        var handler = new UsageRollupHandler(new FakeRollupStore());

        await Assert.ThrowsAsync<InvalidDataException>(
            () => handler.HandleAsync(TurnEvent(ownerId: ""), CancellationToken.None));
        await Assert.ThrowsAsync<InvalidDataException>(
            () => handler.HandleAsync(TurnEvent(credits: -1), CancellationToken.None));
        await Assert.ThrowsAsync<InvalidDataException>(
            () => handler.HandleAsync(TurnEvent() with { DataJson = "{" }, CancellationToken.None));
    }

    [Fact]
    public async Task Worker_deletes_handled_and_ignored_messages_but_keeps_failures()
    {
        var store = new FakeRollupStore();
        using var sqs = new FakeSqsClient(
        [
            new Message { MessageId = "ok", ReceiptHandle = "r-ok", Body = JsonSerializer.Serialize(TurnEvent(), Json) },
            new Message { MessageId = "ignored", ReceiptHandle = "r-ignored", Body = JsonSerializer.Serialize(TurnEvent() with { EventType = "Other" }, Json) },
            new Message { MessageId = "bad", ReceiptHandle = "r-bad", Body = "not json" }
        ]);
        var worker = CreateWorker(sqs, store);

        var deleted = await worker.PollOnceAsync(CancellationToken.None);

        Assert.Equal(2, deleted);
        Assert.Equal(["r-ok", "r-ignored"], sqs.Deleted);
        Assert.Single(store.Applied);
        Assert.Equal("https://sqs.us-east-1.amazonaws.com/123456789012/worker", sqs.LastReceive!.QueueUrl);
        Assert.Equal(5, sqs.LastReceive.MaxNumberOfMessages);
    }

    [Fact]
    public async Task Worker_keeps_message_when_the_store_fails()
    {
        var store = new FakeRollupStore { Fail = true };
        using var sqs = new FakeSqsClient(
        [
            new Message { MessageId = "ok", ReceiptHandle = "r-ok", Body = JsonSerializer.Serialize(TurnEvent(), Json) }
        ]);

        var deleted = await CreateWorker(sqs, store).PollOnceAsync(CancellationToken.None);

        Assert.Equal(0, deleted);
        Assert.Empty(sqs.Deleted);
    }

    [Theory]
    [InlineData("", 10, 20)]
    [InlineData("ftp://queue", 10, 20)]
    [InlineData("https://sqs.us-east-1.amazonaws.com/1/q", 0, 20)]
    [InlineData("https://sqs.us-east-1.amazonaws.com/1/q", 11, 20)]
    [InlineData("https://sqs.us-east-1.amazonaws.com/1/q", 10, 21)]
    public void Worker_options_are_validated(string url, int max, int wait)
    {
        Assert.Throws<InvalidOperationException>(() => new SqsWorkerOptions(url, max, wait).Validate());
    }

    [Fact]
    public async Task Relay_marks_dispatched_and_records_failures()
    {
        var store = new FakeOutboxStore(
        [
            new OutboxRecord(TurnEvent("a"), 0, null, null),
            new OutboxRecord(TurnEvent("b"), 2, null, null)
        ]);
        var publisher = new FakePublisher { FailFor = "b" };
        var relay = new OutboxRelay(store, publisher, NullLogger<OutboxRelay>.Instance);

        var result = await relay.RelayPendingAsync(20, CancellationToken.None);

        Assert.Equal(new OutboxRelayResult(1, 1, 3), result);
        Assert.Equal(["a"], store.Dispatched);
        Assert.Equal(["b"], store.Failed);
        Assert.Equal(["a", "b"], publisher.Published);
    }

    private static SqsDomainEventWorker CreateWorker(IAmazonSQS sqs, IUsageRollupStore store)
    {
        var services = new ServiceCollection();
        services.AddSingleton(store);
        services.AddSingleton<IDomainEventHandler, UsageRollupHandler>();
        services.AddScoped<DomainEventMessageProcessor>();
        var provider = services.BuildServiceProvider();
        return new SqsDomainEventWorker(
            sqs,
            new SqsWorkerOptions("https://sqs.us-east-1.amazonaws.com/123456789012/worker", 5, 0),
            provider.GetRequiredService<IServiceScopeFactory>(),
            NullLogger<SqsDomainEventWorker>.Instance);
    }

    private sealed class FakeRollupStore : IUsageRollupStore
    {
        public List<(string, string, DateOnly, int, int, int)> Applied { get; } = [];
        public bool Fail { get; init; }

        public Task<bool> ApplyTurnToDailyUsageAsync(
            string eventId, string userId, DateOnly day, int inputTokens, int outputTokens, int creditsCharged,
            CancellationToken cancellationToken)
        {
            if (Fail)
            {
                throw new InvalidOperationException("store down");
            }

            Applied.Add((eventId, userId, day, inputTokens, outputTokens, creditsCharged));
            return Task.FromResult(true);
        }

        public Task<DailyUsage?> GetDailyUsageAsync(string userId, DateOnly day, CancellationToken cancellationToken) =>
            Task.FromResult<DailyUsage?>(null);
    }

    private sealed class FakeSqsClient(List<Message> messages)
        : AmazonSQSClient(new AnonymousAWSCredentials(), RegionEndpoint.USEast1)
    {
        public ReceiveMessageRequest? LastReceive { get; private set; }
        public List<string> Deleted { get; } = [];

        public override Task<ReceiveMessageResponse> ReceiveMessageAsync(
            ReceiveMessageRequest request, CancellationToken cancellationToken = default)
        {
            LastReceive = request;
            return Task.FromResult(new ReceiveMessageResponse { Messages = messages });
        }

        public override Task<DeleteMessageResponse> DeleteMessageAsync(
            string queueUrl, string receiptHandle, CancellationToken cancellationToken = default)
        {
            Deleted.Add(receiptHandle);
            return Task.FromResult(new DeleteMessageResponse());
        }
    }

    private sealed class FakeOutboxStore(List<OutboxRecord> pending) : IOutboxStore
    {
        public List<string> Dispatched { get; } = [];
        public List<string> Failed { get; } = [];

        public Task<IReadOnlyList<OutboxRecord>> GetPendingEventsAsync(int limit, CancellationToken cancellationToken) =>
            Task.FromResult<IReadOnlyList<OutboxRecord>>(pending.Take(limit).ToList());

        public Task MarkEventDispatchedAsync(string eventId, CancellationToken cancellationToken)
        {
            Dispatched.Add(eventId);
            return Task.CompletedTask;
        }

        public Task MarkEventDispatchFailedAsync(string eventId, string error, CancellationToken cancellationToken)
        {
            Failed.Add(eventId);
            return Task.CompletedTask;
        }
    }

    private sealed class FakePublisher : IDomainEventPublisher
    {
        public string? FailFor { get; init; }
        public List<string> Published { get; } = [];

        public Task PublishAsync(DomainEventEnvelope domainEvent, CancellationToken cancellationToken)
        {
            Published.Add(domainEvent.EventId);
            return domainEvent.EventId == FailFor
                ? Task.FromException(new InvalidOperationException("sns down"))
                : Task.CompletedTask;
        }
    }
}
