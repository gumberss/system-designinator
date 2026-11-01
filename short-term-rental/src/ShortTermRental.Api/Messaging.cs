using System.Text;
using System.Text.Json;
using Npgsql;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;

namespace ShortTermRental.Api;

public static class Messaging
{
    public const string Exchange = "booking-events";
    public const string Queue = "booking-statistics";
    public const string RoutingKey = "booking.changed";
    public const string DeadLetterExchange = "booking-dead-letter";
    public const string DeadLetterQueue = "booking-statistics-dead-letter";

    public static ConnectionFactory Factory(IConfiguration config) => new()
    {
        HostName = config["RabbitMq:Host"] ?? throw new InvalidOperationException("RabbitMq:Host ausente."),
        Port = config.GetValue<int>("RabbitMq:Port", 5672),
        UserName = config["RabbitMq:User"] ?? throw new InvalidOperationException("RabbitMq:User ausente."),
        Password = config["RabbitMq:Password"] ?? throw new InvalidOperationException("RabbitMq:Password ausente."),
        ConsumerDispatchConcurrency = 8,
        AutomaticRecoveryEnabled = false
    };

    public static async Task Declare(IChannel channel, CancellationToken ct)
    {
        await channel.ExchangeDeclareAsync(Exchange, ExchangeType.Direct, durable: true, cancellationToken: ct);
        await channel.ExchangeDeclareAsync(DeadLetterExchange, ExchangeType.Fanout,
            durable: true, cancellationToken: ct);
        await channel.QueueDeclareAsync(DeadLetterQueue, durable: true, exclusive: false,
            autoDelete: false, cancellationToken: ct);
        await channel.QueueBindAsync(DeadLetterQueue, DeadLetterExchange, "", cancellationToken: ct);
        await channel.QueueDeclareAsync(Queue, durable: true, exclusive: false,
            autoDelete: false, arguments: new Dictionary<string, object?>
            {
                ["x-dead-letter-exchange"] = DeadLetterExchange
            }, cancellationToken: ct);
        await channel.QueueBindAsync(Queue, Exchange, RoutingKey, cancellationToken: ct);
    }
}

public sealed class OutboxPublisher(NpgsqlDataSource source, IConfiguration config,
    ILogger<OutboxPublisher> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await using var rabbit = await Messaging.Factory(config).CreateConnectionAsync(stoppingToken);
                await using var channel = await rabbit.CreateChannelAsync(
                    new CreateChannelOptions(publisherConfirmationsEnabled: true,
                        publisherConfirmationTrackingEnabled: true), stoppingToken);
                await Messaging.Declare(channel, stoppingToken);
                while (!stoppingToken.IsCancellationRequested)
                {
                    var count = await PublishBatch(channel, stoppingToken);
                    if (count == 0)
                        await Task.Delay(250, stoppingToken);
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                // Supervisory boundary: keep pending events in PostgreSQL and retry with backoff.
                logger.LogError(ex, "Publicador outbox interrompido; nova tentativa em 2s");
                try { await Task.Delay(2000, stoppingToken); }
                catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            }
        }
    }

    private async Task<int> PublishBatch(IChannel channel, CancellationToken ct)
    {
        await using var connection = await source.OpenConnectionAsync(ct);
        await using var tx = await connection.BeginTransactionAsync(ct);
        await using var select = new NpgsqlCommand("""
            SELECT event_id, payload::text FROM outbox WHERE published_at IS NULL
            ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED
            """, connection, tx);
        var events = new List<(Guid Id, string Payload)>();
        await using (var reader = await select.ExecuteReaderAsync(ct))
        {
            while (await reader.ReadAsync(ct))
                events.Add((reader.GetGuid(0), reader.GetString(1)));
        }
        foreach (var evt in events)
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(5));
            await channel.BasicPublishAsync(Messaging.Exchange, Messaging.RoutingKey, mandatory: true,
                basicProperties: new BasicProperties
                {
                    Persistent = true, ContentType = "application/json", MessageId = evt.Id.ToString()
                }, body: Encoding.UTF8.GetBytes(evt.Payload), cancellationToken: timeout.Token);
            await using var mark = new NpgsqlCommand(
                "UPDATE outbox SET published_at = now() WHERE event_id = @id", connection, tx);
            mark.Parameters.AddWithValue("id", evt.Id);
            await mark.ExecuteNonQueryAsync(ct);
        }
        await tx.CommitAsync(ct);
        return events.Count;
    }
}

public sealed class StatisticsConsumer(NpgsqlDataSource source, IConfiguration config,
    ILogger<StatisticsConsumer> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await using var rabbit = await Messaging.Factory(config).CreateConnectionAsync(stoppingToken);
                await using var channel = await rabbit.CreateChannelAsync(cancellationToken: stoppingToken);
                await Messaging.Declare(channel, stoppingToken);
                await channel.BasicQosAsync(0, 16, false, stoppingToken);
                var failure = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                channel.ChannelShutdownAsync += (_, args) =>
                {
                    failure.TrySetException(new IOException($"Canal RabbitMQ fechado: {args.ReplyText}"));
                    return Task.CompletedTask;
                };
                var consumer = new AsyncEventingBasicConsumer(channel);
                consumer.ReceivedAsync += async (_, args) =>
                {
                    try
                    {
                        var evt = JsonSerializer.Deserialize<BookingEvent>(args.Body.Span)
                            ?? throw new JsonException("Evento vazio.");
                        if (evt.EventId == Guid.Empty || evt.BookingId == Guid.Empty ||
                            evt.PropertyId == Guid.Empty ||
                            evt.EventType is not ("booking.confirmed" or "booking.cancelled"))
                            throw new JsonException("Evento invalido.");
                        await Process(evt, stoppingToken);
                        await channel.BasicAckAsync(args.DeliveryTag, false, stoppingToken);
                    }
                    catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { }
                    catch (JsonException ex)
                    {
                        logger.LogError(ex, "Evento invalido; enviando para dead-letter");
                        await channel.BasicNackAsync(args.DeliveryTag, false, false, stoppingToken);
                    }
                    catch (Exception ex)
                    {
                        logger.LogError(ex, "Evento nao processado; fechando canal para reentrega");
                        failure.TrySetException(ex);
                    }
                };
                await channel.BasicConsumeAsync(Messaging.Queue, autoAck: false,
                    consumer: consumer, cancellationToken: stoppingToken);
                await failure.Task.WaitAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                logger.LogError(ex, "Consumidor interrompido; nova tentativa em 2s");
                try { await Task.Delay(2000, stoppingToken); }
                catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            }
        }
    }

    private async Task Process(BookingEvent evt, CancellationToken ct)
    {
        await using var connection = await source.OpenConnectionAsync(ct);
        await using var tx = await connection.BeginTransactionAsync(ct);
        await using var deduplicate = new NpgsqlCommand("""
            INSERT INTO processed_events(event_id, event_type) VALUES (@id, @type)
            ON CONFLICT (event_id) DO UPDATE SET deliveries = processed_events.deliveries + 1
            RETURNING deliveries
            """, connection, tx);
        deduplicate.Parameters.AddWithValue("id", evt.EventId);
        deduplicate.Parameters.AddWithValue("type", evt.EventType);
        if ((long)(await deduplicate.ExecuteScalarAsync(ct)
            ?? throw new InvalidOperationException("Inbox nao retornou contador de entregas.")) == 1)
        {
            await using var update = new NpgsqlCommand("""
                INSERT INTO booking_statistics(property_id, active_bookings) VALUES (@property, @delta)
                ON CONFLICT (property_id) DO UPDATE
                SET active_bookings = booking_statistics.active_bookings + EXCLUDED.active_bookings
                """, connection, tx);
            update.Parameters.AddWithValue("property", evt.PropertyId);
            update.Parameters.AddWithValue("delta", evt.EventType == "booking.confirmed" ? 1L : -1L);
            await update.ExecuteNonQueryAsync(ct);
        }
        await tx.CommitAsync(ct);
    }
}
