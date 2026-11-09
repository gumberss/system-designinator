using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure;
using AiChatinator.Infrastructure.DynamoDb;
using Amazon.DynamoDBv2;
using Amazon.DynamoDBv2.Model;
using Amazon.Runtime;

namespace AiChatinator.Tests;

/// <summary>
/// Behavioural contract every <see cref="IChatStore"/> must satisfy. Runs against the local JSON
/// store and against DynamoDB Local so the AWS adapter is proven to match local semantics.
/// </summary>
public abstract class ChatStoreContractTests
{
    protected abstract Task<IChatStore> CreateStoreAsync();

    private static LocalUser NewUser(int credits = 1000) =>
        new(Guid.NewGuid().ToString("N"), $"{Guid.NewGuid():N}@Example.test", "hash", credits, DateTimeOffset.UtcNow);

    private static Task<ChatTurnResult> TurnAsync(
        IChatStore store, string userId, string conversationId, string key, string fingerprint = "fp", string content = "hello") =>
        store.CompleteTurnAsync(userId, conversationId, key, fingerprint, content, "reply to " + content,
            10, 20, "local-demo", "demo", "trace-" + key, CancellationToken.None);

    [SkippableFact]
    public async Task Users_are_unique_by_case_insensitive_email()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();

        Assert.True(await store.AddUserAsync(user, CancellationToken.None));
        Assert.False(await store.AddUserAsync(user with { Id = "other-id", Email = user.Email.ToUpperInvariant() }, CancellationToken.None));
        Assert.Equal(user.Id, (await store.FindUserByEmailAsync(user.Email.ToLowerInvariant(), CancellationToken.None))!.Id);
        Assert.Equal(user.Email, (await store.FindUserByIdAsync(user.Id, CancellationToken.None))!.Email);
        Assert.Null(await store.FindUserByIdAsync("missing", CancellationToken.None));
    }

    [SkippableFact]
    public async Task Completed_turn_charges_credits_persists_messages_and_is_idempotent()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);

        var first = await TurnAsync(store, user.Id, conversation.Id, "k1");
        var replay = await store.FindCompletedTurnAsync(user.Id, "k1", conversation.Id, "fp", CancellationToken.None);

        Assert.Equal(999, first.CreditsRemaining);
        Assert.Equal(1, first.CreditsCharged);
        Assert.NotNull(replay);
        Assert.Equal(first.AssistantMessage.Id, replay.AssistantMessage.Id);
        Assert.Equal(999, (await store.FindUserByIdAsync(user.Id, CancellationToken.None))!.Credits);
        var messages = await store.ListMessagesAsync(user.Id, conversation.Id, CancellationToken.None);
        Assert.NotNull(messages);
        Assert.Equal(["user", "assistant"], messages.Select(message => message.Role));
        Assert.Equal("hello", messages[0].Content);
        var history = Assert.Single(await store.GetUsageHistoryAsync(user.Id, CancellationToken.None));
        Assert.Equal(conversation.Id, history.ConversationId);
        Assert.Equal(20, history.OutputTokens);

        await Assert.ThrowsAsync<IdempotencyConflictException>(
            () => store.FindCompletedTurnAsync(user.Id, "k1", conversation.Id, "different", CancellationToken.None));
        await Assert.ThrowsAsync<IdempotencyConflictException>(
            () => TurnAsync(store, user.Id, conversation.Id, "k1", fingerprint: "different"));
        Assert.Equal(999, (await store.FindUserByIdAsync(user.Id, CancellationToken.None))!.Credits);
    }

    [SkippableFact]
    public async Task Turn_without_credits_is_rejected_and_nothing_is_written()
    {
        var store = await CreateStoreAsync();
        var user = NewUser(credits: 0);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);

        await Assert.ThrowsAsync<InsufficientCreditsException>(() => TurnAsync(store, user.Id, conversation.Id, "k1"));

        Assert.Empty((await store.ListMessagesAsync(user.Id, conversation.Id, CancellationToken.None))!);
        Assert.Empty(await store.GetUsageHistoryAsync(user.Id, CancellationToken.None));
    }

    [SkippableFact]
    public async Task Conversations_are_owner_scoped()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();
        var other = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        await store.AddUserAsync(other, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);

        Assert.Null(await store.ListMessagesAsync(other.Id, conversation.Id, CancellationToken.None));
        Assert.False(await store.SetConversationArchivedAsync(other.Id, conversation.Id, true, CancellationToken.None));
        Assert.False(await store.DeleteConversationAsync(other.Id, conversation.Id, CancellationToken.None));
        Assert.Empty(await store.ListConversationsAsync(other.Id, CancellationToken.None));
        Assert.True(await store.SetConversationArchivedAsync(user.Id, conversation.Id, true, CancellationToken.None));
        Assert.True(Assert.Single(await store.ListConversationsAsync(user.Id, CancellationToken.None)).IsArchived);
    }

    [SkippableFact]
    public async Task Search_finds_messages_only_in_the_owners_other_conversations()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();
        var stranger = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        await store.AddUserAsync(stranger, CancellationToken.None);
        var current = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var previous = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var strangers = await store.CreateConversationAsync(stranger.Id, CancellationToken.None);
        await TurnAsync(store, user.Id, previous.Id, "a", content: "my number is 4242");
        await TurnAsync(store, user.Id, current.Id, "b", content: "the number 4242 again");
        await TurnAsync(store, stranger.Id, strangers.Id, "c", content: "stranger 4242");

        var matches = await store.SearchOtherConversationMessagesAsync(user.Id, current.Id, "4242", CancellationToken.None);

        Assert.NotEmpty(matches);
        Assert.All(matches, match => Assert.Equal(previous.Id, match.ConversationId));
    }

    [SkippableFact]
    public async Task Context_summary_uses_optimistic_versioning()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var turn = await TurnAsync(store, user.Id, conversation.Id, "k1");

        Assert.Null(await store.GetContextSummaryAsync(user.Id, conversation.Id, CancellationToken.None));
        Assert.True(await store.SaveContextSummaryAsync(user.Id, conversation.Id, 0, turn.AssistantMessage.Id, "summary", "m", CancellationToken.None));
        Assert.False(await store.SaveContextSummaryAsync(user.Id, conversation.Id, 0, turn.AssistantMessage.Id, "stale", "m", CancellationToken.None));
        Assert.True(await store.SaveContextSummaryAsync(user.Id, conversation.Id, 1, turn.AssistantMessage.Id, "summary 2", "m", CancellationToken.None));

        var summary = await store.GetContextSummaryAsync(user.Id, conversation.Id, CancellationToken.None);
        Assert.Equal(2, summary!.Version);
        Assert.Equal("summary 2", summary.Content);
    }

    [SkippableFact]
    public async Task Files_are_owner_scoped()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var file = await store.SaveConversationFileAsync(new ConversationFile(
            Guid.NewGuid().ToString("N"), user.Id, conversation.Id, "plan.txt", "text/plain", 5, "abc", "hello",
            DateTimeOffset.UtcNow), CancellationToken.None);

        Assert.Equal("hello", Assert.Single(await store.GetConversationFilesAsync(user.Id, conversation.Id, [file.Id], CancellationToken.None)).ExtractedText);
        Assert.Single(await store.ListConversationFilesAsync(user.Id, conversation.Id, CancellationToken.None));
        await Assert.ThrowsAsync<KeyNotFoundException>(
            () => store.GetConversationFilesAsync("someone-else", conversation.Id, [file.Id], CancellationToken.None));
    }

    [SkippableFact]
    public async Task Share_links_can_be_revoked_and_deleted_conversations_disappear()
    {
        var store = await CreateStoreAsync();
        var user = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        await TurnAsync(store, user.Id, conversation.Id, "k1");
        var hash = Guid.NewGuid().ToString("N");

        Assert.True(await store.SetConversationShareAsync(user.Id, conversation.Id, hash, CancellationToken.None));
        Assert.Equal(2, (await store.GetSharedConversationAsync(hash, CancellationToken.None))!.Messages.Count);
        Assert.True(await store.RevokeConversationShareAsync(user.Id, conversation.Id, CancellationToken.None));
        Assert.Null(await store.GetSharedConversationAsync(hash, CancellationToken.None));

        Assert.True(await store.SetConversationShareAsync(user.Id, conversation.Id, hash, CancellationToken.None));
        Assert.True(await store.DeleteConversationAsync(user.Id, conversation.Id, CancellationToken.None));
        Assert.Null(await store.GetSharedConversationAsync(hash, CancellationToken.None));
        Assert.Null(await store.ListMessagesAsync(user.Id, conversation.Id, CancellationToken.None));
        Assert.Empty(await store.ListConversationsAsync(user.Id, CancellationToken.None));
    }

    [SkippableFact]
    public async Task Outbox_events_are_retried_and_then_dispatched()
    {
        var store = await CreateStoreAsync();
        var outbox = (IOutboxStore)store;
        foreach (var pending in await outbox.GetPendingEventsAsync(100, CancellationToken.None))
        {
            await outbox.MarkEventDispatchedAsync(pending.Event.EventId, CancellationToken.None);
        }

        var user = NewUser();
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        await TurnAsync(store, user.Id, conversation.Id, "k1", content: "private prompt");

        var record = Assert.Single(await outbox.GetPendingEventsAsync(10, CancellationToken.None));
        Assert.Equal("MessageTurnCompleted", record.Event.EventType);
        Assert.Equal(conversation.Id, record.Event.AggregateId);
        Assert.DoesNotContain("private prompt", record.Event.DataJson, StringComparison.Ordinal);

        await outbox.MarkEventDispatchFailedAsync(record.Event.EventId, "Boom", CancellationToken.None);
        var retry = Assert.Single(await outbox.GetPendingEventsAsync(10, CancellationToken.None));
        Assert.Equal(1, retry.Attempts);
        Assert.Equal("Boom", retry.LastError);

        await outbox.MarkEventDispatchedAsync(record.Event.EventId, CancellationToken.None);
        Assert.Empty(await outbox.GetPendingEventsAsync(10, CancellationToken.None));
    }
}

public sealed class LocalChatStoreContractTests : ChatStoreContractTests, IDisposable
{
    private readonly string _dataPath = Path.Combine(Path.GetTempPath(), $"ai-chatinator-contract-{Guid.NewGuid():N}.json");

    protected override Task<IChatStore> CreateStoreAsync() => Task.FromResult<IChatStore>(new LocalDataStore(_dataPath));

    public void Dispose()
    {
        if (File.Exists(_dataPath))
        {
            File.Delete(_dataPath);
        }
    }
}

[Collection(DynamoDbLocalCollection.Name)]
public sealed class DynamoDbChatStoreContractTests(DynamoDbLocalFixture fixture) : ChatStoreContractTests
{
    protected override async Task<IChatStore> CreateStoreAsync()
    {
        Skip.IfNot(fixture.IsAvailable, fixture.UnavailableReason);
        return new DynamoDbChatStore(fixture.Client!, await fixture.CreateTablesAsync());
    }

    [SkippableFact]
    public async Task Daily_usage_rollup_is_idempotent_by_event_id()
    {
        Skip.IfNot(fixture.IsAvailable, fixture.UnavailableReason);
        var store = new DynamoDbChatStore(fixture.Client!, await fixture.CreateTablesAsync());
        var day = new DateOnly(2025, 3, 5);

        Assert.True(await store.ApplyTurnToDailyUsageAsync("e1", "u1", day, 10, 20, 1, CancellationToken.None));
        Assert.False(await store.ApplyTurnToDailyUsageAsync("e1", "u1", day, 10, 20, 1, CancellationToken.None));
        Assert.True(await store.ApplyTurnToDailyUsageAsync("e2", "u1", day, 5, 5, 2, CancellationToken.None));

        Assert.Equal(new DailyUsage("u1", day, 2, 15, 25, 3), await store.GetDailyUsageAsync("u1", day, CancellationToken.None));
        Assert.Null(await store.GetDailyUsageAsync("u1", day.AddDays(1), CancellationToken.None));
    }
}

[CollectionDefinition(Name)]
public sealed class DynamoDbLocalCollection : ICollectionFixture<DynamoDbLocalFixture>
{
    public const string Name = "DynamoDB Local";
}

/// <summary>
/// Starts DynamoDB Local (in-memory, random port) once per test run. Tests are skipped when Java or
/// the DynamoDB Local JAR is missing. Override the location with DYNAMODB_LOCAL_DIR.
/// </summary>
public sealed class DynamoDbLocalFixture : IAsyncLifetime
{
    private Process? _process;

    public AmazonDynamoDBClient? Client { get; private set; }
    public bool IsAvailable => Client is not null;
    public string UnavailableReason { get; private set; } = "DynamoDB Local is not available.";

    public async Task InitializeAsync()
    {
        var directory = Environment.GetEnvironmentVariable("DYNAMODB_LOCAL_DIR") ?? @"C:\tools\dynamodb-local";
        var jar = Path.Combine(directory, "DynamoDBLocal.jar");
        if (!File.Exists(jar))
        {
            UnavailableReason = $"DynamoDB Local JAR not found at {jar}.";
            return;
        }

        var port = GetFreePort();
        try
        {
            _process = Process.Start(new ProcessStartInfo("java")
            {
                ArgumentList =
                {
                    $"-Djava.library.path={Path.Combine(directory, "DynamoDBLocal_lib")}",
                    "-jar", jar, "-inMemory", "-port", port.ToString()
                },
                WorkingDirectory = directory,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true
            });
        }
        catch (Exception exception)
        {
            UnavailableReason = $"Could not start java: {exception.Message}";
            return;
        }

        _process!.BeginOutputReadLine();
        _process.BeginErrorReadLine();
        var client = new AmazonDynamoDBClient(
            new BasicAWSCredentials("local", "local"),
            new AmazonDynamoDBConfig { ServiceURL = $"http://127.0.0.1:{port}" });
        var deadline = DateTime.UtcNow.AddSeconds(30);
        while (DateTime.UtcNow < deadline)
        {
            try
            {
                await client.ListTablesAsync(new ListTablesRequest());
                Client = client;
                return;
            }
            catch (Exception) when (!_process.HasExited)
            {
                await Task.Delay(250);
            }
        }

        client.Dispose();
        UnavailableReason = "DynamoDB Local did not become ready in time.";
    }

    public async Task<DynamoDbTableOptions> CreateTablesAsync()
    {
        var suffix = Guid.NewGuid().ToString("N")[..8];
        var options = new DynamoDbTableOptions($"conversations-{suffix}", $"usage-{suffix}", $"audit-{suffix}");
        await CreateTableAsync(options.ConversationsTable, withIndex: true);
        await CreateTableAsync(options.UsageTable, withIndex: true);
        await CreateTableAsync(options.AuditTable, withIndex: false);
        return options;
    }

    private async Task CreateTableAsync(string name, bool withIndex)
    {
        var request = new CreateTableRequest
        {
            TableName = name,
            BillingMode = BillingMode.PAY_PER_REQUEST,
            KeySchema = [new("pk", KeyType.HASH), new("sk", KeyType.RANGE)],
            AttributeDefinitions = [new("pk", ScalarAttributeType.S), new("sk", ScalarAttributeType.S)]
        };
        if (withIndex)
        {
            request.AttributeDefinitions.Add(new("gsi1pk", ScalarAttributeType.S));
            request.AttributeDefinitions.Add(new("gsi1sk", ScalarAttributeType.S));
            request.GlobalSecondaryIndexes =
            [
                new GlobalSecondaryIndex
                {
                    IndexName = "gsi1",
                    KeySchema = [new("gsi1pk", KeyType.HASH), new("gsi1sk", KeyType.RANGE)],
                    Projection = new Projection { ProjectionType = ProjectionType.ALL }
                }
            ];
        }

        await Client!.CreateTableAsync(request);
    }

    public Task DisposeAsync()
    {
        Client?.Dispose();
        if (_process is { HasExited: false })
        {
            _process.Kill(entireProcessTree: true);
        }

        _process?.Dispose();
        return Task.CompletedTask;
    }

    private static int GetFreePort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }
}
