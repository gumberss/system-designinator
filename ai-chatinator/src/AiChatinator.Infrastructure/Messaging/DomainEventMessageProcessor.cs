using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Domain;

namespace AiChatinator.Infrastructure.Messaging;

public enum DomainEventProcessingOutcome
{
    Handled,
    Ignored
}

/// <summary>
/// Turns a queue message body into a <see cref="DomainEventEnvelope"/> and routes it to the
/// handler registered for its event type. Accepts both SNS-wrapped notifications (the default
/// SNS-to-SQS subscription format) and raw envelopes (raw message delivery).
/// Malformed bodies throw so the message is retried and eventually moved to the dead-letter queue.
/// </summary>
public sealed class DomainEventMessageProcessor
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private readonly IReadOnlyDictionary<string, IDomainEventHandler> _handlers;

    public DomainEventMessageProcessor(IEnumerable<IDomainEventHandler> handlers)
    {
        var map = new Dictionary<string, IDomainEventHandler>(StringComparer.Ordinal);
        foreach (var handler in handlers)
        {
            if (!map.TryAdd(handler.EventType, handler))
            {
                throw new InvalidOperationException(
                    $"More than one handler is registered for event type '{handler.EventType}'.");
            }
        }

        _handlers = map;
    }

    public async Task<DomainEventProcessingOutcome> ProcessAsync(
        string messageBody,
        CancellationToken cancellationToken)
    {
        var domainEvent = Parse(messageBody);
        if (!_handlers.TryGetValue(domainEvent.EventType, out var handler))
        {
            return DomainEventProcessingOutcome.Ignored;
        }

        await handler.HandleAsync(domainEvent, cancellationToken);
        return DomainEventProcessingOutcome.Handled;
    }

    public static DomainEventEnvelope Parse(string messageBody)
    {
        if (string.IsNullOrWhiteSpace(messageBody))
        {
            throw new InvalidDataException("The queue message body is empty.");
        }

        try
        {
            using var document = JsonDocument.Parse(messageBody);
            var root = document.RootElement;
            var payload = messageBody;
            if (root.ValueKind == JsonValueKind.Object
                && root.TryGetProperty("Type", out var type)
                && type.ValueKind == JsonValueKind.String
                && type.GetString() == "Notification"
                && root.TryGetProperty("Message", out var message)
                && message.ValueKind == JsonValueKind.String)
            {
                payload = message.GetString()!;
            }

            var domainEvent = JsonSerializer.Deserialize<DomainEventEnvelope>(payload, JsonOptions);
            if (domainEvent is null
                || string.IsNullOrWhiteSpace(domainEvent.EventId)
                || string.IsNullOrWhiteSpace(domainEvent.EventType)
                || domainEvent.DataJson is null)
            {
                throw new InvalidDataException("The queue message is not a valid domain event envelope.");
            }

            return domainEvent;
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("The queue message body is not valid JSON.", exception);
        }
    }
}

/// <summary>
/// Maintains per-user daily usage totals from MessageTurnCompleted events. Idempotent by event id.
/// </summary>
public sealed class UsageRollupHandler(IUsageRollupStore store) : IDomainEventHandler
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    public string EventType => "MessageTurnCompleted";

    public async Task HandleAsync(DomainEventEnvelope domainEvent, CancellationToken cancellationToken)
    {
        TurnCompletedData? data;
        try
        {
            data = JsonSerializer.Deserialize<TurnCompletedData>(domainEvent.DataJson, JsonOptions);
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("MessageTurnCompleted data is not valid JSON.", exception);
        }

        if (data is null
            || string.IsNullOrWhiteSpace(data.OwnerId)
            || data.InputTokens < 0
            || data.OutputTokens < 0
            || data.CreditsCharged < 0)
        {
            throw new InvalidDataException("MessageTurnCompleted data is invalid.");
        }

        await store.ApplyTurnToDailyUsageAsync(
            domainEvent.EventId,
            data.OwnerId,
            DateOnly.FromDateTime(domainEvent.OccurredAt.UtcDateTime),
            data.InputTokens,
            data.OutputTokens,
            data.CreditsCharged,
            cancellationToken);
    }

    private sealed record TurnCompletedData(
        string OwnerId,
        int InputTokens,
        int OutputTokens,
        int CreditsCharged);
}
