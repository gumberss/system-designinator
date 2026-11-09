using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure.Messaging;
using AiChatinator.Worker;
using Amazon.Lambda.SQSEvents;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

namespace AiChatinator.Tests;

public sealed class WorkerLambdaTests
{
    private static DomainEventEnvelope Event(string id, string type = "Test") =>
        new(id, type, 1, DateTimeOffset.UtcNow, "tests", "c", "a", id, "{}");

    private static IServiceProvider Services(Action<IServiceCollection> configure)
    {
        var services = new ServiceCollection();
        services.AddSingleton<ILoggerFactory>(NullLoggerFactory.Instance);
        services.AddSingleton(typeof(ILogger<>), typeof(Logger<>));
        configure(services);
        return services.BuildServiceProvider();
    }

    [Fact]
    public async Task Sqs_handler_reports_only_failed_messages()
    {
        var handler = new RecordingHandler();
        var services = Services(s =>
        {
            s.AddSingleton<IDomainEventHandler>(handler);
            s.AddSingleton<DomainEventMessageProcessor>();
        });
        var sqsEvent = new SQSEvent
        {
            Records =
            [
                new SQSEvent.SQSMessage { MessageId = "ok", Body = JsonSerializer.Serialize(Event("e1"), JsonSerializerOptions.Web) },
                new SQSEvent.SQSMessage { MessageId = "bad", Body = "not json" },
                new SQSEvent.SQSMessage { MessageId = "ignored", Body = JsonSerializer.Serialize(Event("e2", "Other"), JsonSerializerOptions.Web) }
            ]
        };

        var response = await new SqsHandler(services).HandleAsync(sqsEvent, null);

        Assert.Equal(["bad"], response.BatchItemFailures.Select(f => f.ItemIdentifier));
        Assert.Equal(["e1"], handler.Handled);
    }

    [Fact]
    public async Task Outbox_relay_handler_publishes_pending_events_and_keeps_failures_pending()
    {
        var store = new FakeOutbox([Event("e1"), Event("e2"), Event("fail")]);
        var publisher = new FakePublisher();
        var services = Services(s =>
        {
            s.AddSingleton<IOutboxStore>(store);
            s.AddSingleton<IDomainEventPublisher>(publisher);
            s.AddSingleton<OutboxRelay>();
        });

        var result = await new OutboxRelayHandler(services).HandleAsync(null);

        Assert.Equal(2, result.Dispatched);
        Assert.Equal(1, result.Failed);
        Assert.Equal(["e1", "e2"], store.Dispatched);
        Assert.Equal(["fail"], store.Failed);
    }

    [Fact]
    public void Outbox_relay_handler_requires_a_topic()
    {
        Assert.Throws<InvalidOperationException>(() => new OutboxRelayHandler(Services(_ => { })));
    }

    private sealed class RecordingHandler : IDomainEventHandler
    {
        public List<string> Handled { get; } = [];
        public string EventType => "Test";

        public Task HandleAsync(DomainEventEnvelope domainEvent, CancellationToken cancellationToken)
        {
            Handled.Add(domainEvent.EventId);
            return Task.CompletedTask;
        }
    }

    private sealed class FakeOutbox(IEnumerable<DomainEventEnvelope> events) : IOutboxStore
    {
        private readonly List<DomainEventEnvelope> _pending = events.ToList();
        public List<string> Dispatched { get; } = [];
        public List<string> Failed { get; } = [];

        public Task<IReadOnlyList<OutboxRecord>> GetPendingEventsAsync(int limit, CancellationToken cancellationToken) =>
            Task.FromResult<IReadOnlyList<OutboxRecord>>(_pending
                .Where(e => !Dispatched.Contains(e.EventId))
                .Take(limit)
                .Select(e => new OutboxRecord(e, 0, null, null))
                .ToList());

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
        public Task PublishAsync(DomainEventEnvelope domainEvent, CancellationToken cancellationToken) =>
            domainEvent.EventId == "fail"
                ? throw new InvalidOperationException("boom")
                : Task.CompletedTask;
    }
}
