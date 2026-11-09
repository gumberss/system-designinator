using System.Globalization;
using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Domain;
using Amazon.DynamoDBv2;
using Amazon.DynamoDBv2.Model;

namespace AiChatinator.Infrastructure.DynamoDb;

public sealed record DynamoDbTableOptions(
    string ConversationsTable,
    string UsageTable,
    string AuditTable,
    int AuditRetentionDays = 365)
{
    public const string PendingOutboxIndex = "gsi1";
    public const string UsageByCreatedAtIndex = "gsi1";

    public void Validate()
    {
        if (string.IsNullOrWhiteSpace(ConversationsTable)
            || string.IsNullOrWhiteSpace(UsageTable)
            || string.IsNullOrWhiteSpace(AuditTable))
        {
            throw new InvalidOperationException("All DynamoDB table names must be configured.");
        }

        if (AuditRetentionDays is < 1 or > 3650)
        {
            throw new InvalidOperationException("Audit retention must be between 1 and 3650 days.");
        }
    }
}

/// <summary>
/// DynamoDB implementation of the chat, file, outbox and usage-rollup stores.
/// Conversations table (single-table design, pk/sk):
///   USER#{id}       / PROFILE            user profile and credits
///   EMAIL#{EMAIL}   / EMAIL              email uniqueness guard
///   USER#{owner}    / CONV#{id}          conversation (owner-scoped key enforces ownership)
///   CONV#{id}       / MSG#{ticks}#{id}   message, naturally ordered by time then id
///   CONV#{id}       / SUMMARY            rolling context summary (optimistic version)
///   CONV#{id}       / FILE#{id}          extracted attachment text
///   SHARE#{hash}    / SHARE              public share token lookup
///   OUTBOX#{id}     / EVENT              transactional outbox; sparse gsi1 holds pending events
/// Usage table: USER#{id} / IDEM#{key} (idempotent turn, gsi1 by time), DAILY#{day}; EVENT#{id} markers.
/// Audit table: RESOURCE#{id} / {ticks}#{eventId} with a TTL attribute.
/// </summary>
public sealed class DynamoDbChatStore : IChatStore, IOutboxStore, IUsageRollupStore
{
    private const string NewConversationTitle = "New conversation";
    private const string PendingOutboxPartition = "OUTBOX#PENDING";
    private const string ConditionalCheckFailed = "ConditionalCheckFailed";

    private readonly IAmazonDynamoDB _client;
    private readonly DynamoDbTableOptions _tables;

    public DynamoDbChatStore(IAmazonDynamoDB client, DynamoDbTableOptions tables)
    {
        tables.Validate();
        _client = client;
        _tables = tables;
    }

    public async Task<LocalUser?> FindUserByEmailAsync(string email, CancellationToken cancellationToken)
    {
        var guard = await GetAsync(_tables.ConversationsTable, EmailKey(email), "EMAIL", cancellationToken);
        return guard is null
            ? null
            : await FindUserByIdAsync(guard["userId"].S, cancellationToken);
    }

    public async Task<LocalUser?> FindUserByIdAsync(string userId, CancellationToken cancellationToken)
    {
        var item = await GetAsync(_tables.ConversationsTable, UserKey(userId), "PROFILE", cancellationToken);
        return item is null ? null : ToUser(item);
    }

    public async Task<bool> AddUserAsync(LocalUser user, CancellationToken cancellationToken)
    {
        var failure = await TryTransactAsync(
        [
            Put(_tables.ConversationsTable, new()
            {
                ["pk"] = S(EmailKey(user.Email)),
                ["sk"] = S("EMAIL"),
                ["userId"] = S(user.Id)
            }, "attribute_not_exists(pk)"),
            Put(_tables.ConversationsTable, new()
            {
                ["pk"] = S(UserKey(user.Id)),
                ["sk"] = S("PROFILE"),
                ["id"] = S(user.Id),
                ["email"] = S(user.Email),
                ["passwordHash"] = S(user.PasswordHash),
                ["credits"] = N(user.Credits),
                ["createdAt"] = Date(user.CreatedAt)
            }, "attribute_not_exists(pk)"),
            Audit("user.registered", user.Id, user.Id, user.Id)
        ], cancellationToken);
        return failure is null;
    }

    public async Task<IReadOnlyList<ConversationSummary>> ListConversationsAsync(
        string userId,
        CancellationToken cancellationToken) =>
        (await QueryConversationsAsync(userId, cancellationToken))
            .OrderByDescending(conversation => conversation.UpdatedAt)
            .Select(ToSummary)
            .ToArray();

    public async Task<ConversationSummary> CreateConversationAsync(
        string userId,
        CancellationToken cancellationToken)
    {
        var now = DateTimeOffset.UtcNow;
        var conversation = new Conversation(
            Guid.NewGuid().ToString("N"),
            userId,
            NewConversationTitle,
            now,
            now,
            false,
            null);
        await TransactAsync(
        [
            Put(_tables.ConversationsTable, ToItem(conversation), "attribute_not_exists(pk)"),
            Audit("conversation.created", userId, conversation.Id, userId)
        ], cancellationToken);
        return ToSummary(conversation);
    }

    public async Task<IReadOnlyList<ConversationMessageMatch>> SearchOtherConversationMessagesAsync(
        string userId,
        string currentConversationId,
        string query,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(query) || query.Length > 160)
        {
            throw new InvalidDataException("The conversation search query must be 1 to 160 characters.");
        }

        var conversations = await QueryConversationsAsync(userId, cancellationToken);
        if (!conversations.Any(conversation => conversation.Id == currentConversationId))
        {
            throw new KeyNotFoundException("Conversation not found.");
        }

        // DynamoDB has no full-text search; this reads only the caller's own partitions.
        var matches = new List<(ChatMessage Message, string Title)>();
        foreach (var conversation in conversations.Where(item => item.Id != currentConversationId))
        {
            foreach (var message in await QueryMessagesAsync(conversation.Id, cancellationToken))
            {
                if (message.Content.Contains(query, StringComparison.OrdinalIgnoreCase))
                {
                    matches.Add((message, conversation.Title));
                }
            }
        }

        return matches
            .OrderByDescending(match => match.Message.CreatedAt)
            .ThenBy(match => match.Message.Id, StringComparer.Ordinal)
            .Take(5)
            .Select(match =>
            {
                var content = match.Message.Content;
                var matchIndex = content.IndexOf(query, StringComparison.OrdinalIgnoreCase);
                var start = Math.Max(0, matchIndex - 60);
                return new ConversationMessageMatch(
                    match.Message.ConversationId,
                    match.Title.Length > 100 ? match.Title[..100] : match.Title,
                    match.Message.Id,
                    match.Message.Role,
                    content.Substring(start, Math.Min(300, content.Length - start)),
                    match.Message.CreatedAt);
            })
            .ToArray();
    }

    public async Task<bool> SetConversationArchivedAsync(
        string userId,
        string conversationId,
        bool isArchived,
        CancellationToken cancellationToken)
    {
        var failure = await TryTransactAsync(
        [
            new TransactWriteItem
            {
                Update = new Update
                {
                    TableName = _tables.ConversationsTable,
                    Key = Key(UserKey(userId), ConversationSortKey(conversationId)),
                    ConditionExpression = "attribute_exists(pk)",
                    UpdateExpression = "SET isArchived = :archived, updatedAt = :now",
                    ExpressionAttributeValues = new()
                    {
                        [":archived"] = new AttributeValue { BOOL = isArchived },
                        [":now"] = Date(DateTimeOffset.UtcNow)
                    }
                }
            },
            Audit(isArchived ? "conversation.archived" : "conversation.restored", userId, conversationId, userId)
        ], cancellationToken);
        return failure is null;
    }

    public async Task<IReadOnlyList<UsageHistoryItem>> GetUsageHistoryAsync(
        string userId,
        CancellationToken cancellationToken)
    {
        var response = await _client.QueryAsync(new QueryRequest
        {
            TableName = _tables.UsageTable,
            IndexName = DynamoDbTableOptions.UsageByCreatedAtIndex,
            KeyConditionExpression = "gsi1pk = :pk",
            ExpressionAttributeValues = new() { [":pk"] = S(UserKey(userId)) },
            ScanIndexForward = false,
            Limit = 100
        }, cancellationToken);
        return (response.Items ?? [])
            .Select(ToUsage)
            .Select(usage => new UsageHistoryItem(
                usage.ConversationId,
                usage.CreditsCharged,
                usage.InputTokens,
                usage.OutputTokens,
                usage.Provider,
                usage.Model,
                usage.CreatedAt))
            .ToArray();
    }

    public async Task<IReadOnlyList<ChatMessage>?> ListMessagesAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken)
    {
        if (await GetConversationAsync(userId, conversationId, cancellationToken) is null)
        {
            return null;
        }

        return await QueryMessagesAsync(conversationId, cancellationToken);
    }

    public async Task<ContextSummary?> GetContextSummaryAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken)
    {
        if (await GetConversationAsync(userId, conversationId, cancellationToken) is null)
        {
            return null;
        }

        var item = await GetAsync(
            _tables.ConversationsTable,
            ConversationKey(conversationId),
            "SUMMARY",
            cancellationToken);
        return item is null
            ? null
            : new ContextSummary(
                conversationId,
                Int(item["version"]),
                item["throughMessageId"].S,
                item["content"].S,
                item["model"].S,
                ParseDate(item["createdAt"]));
    }

    public async Task<bool> SaveContextSummaryAsync(
        string userId,
        string conversationId,
        int expectedVersion,
        string throughMessageId,
        string content,
        string model,
        CancellationToken cancellationToken)
    {
        if (await GetConversationAsync(userId, conversationId, cancellationToken) is null)
        {
            return false;
        }

        var messages = await QueryMessagesAsync(conversationId, cancellationToken);
        if (!messages.Any(message => message.Id == throughMessageId))
        {
            throw new InvalidDataException("A context summary must refer to a message in its conversation.");
        }

        var put = Put(_tables.ConversationsTable, new()
        {
            ["pk"] = S(ConversationKey(conversationId)),
            ["sk"] = S("SUMMARY"),
            ["version"] = N(expectedVersion + 1),
            ["throughMessageId"] = S(throughMessageId),
            ["content"] = S(content),
            ["model"] = S(model),
            ["createdAt"] = Date(DateTimeOffset.UtcNow)
        }, expectedVersion == 0 ? "attribute_not_exists(pk)" : "version = :expected");
        if (expectedVersion != 0)
        {
            put.Put.ExpressionAttributeValues = new() { [":expected"] = N(expectedVersion) };
        }

        var failure = await TryTransactAsync(
        [
            ConditionCheck(_tables.ConversationsTable, UserKey(userId), ConversationSortKey(conversationId)),
            put,
            Audit("conversation.context_summarized", userId, conversationId, userId)
        ], cancellationToken);
        return failure is null;
    }

    public async Task<ChatTurnResult?> FindCompletedTurnAsync(
        string userId,
        string idempotencyKey,
        string conversationId,
        string requestFingerprint,
        CancellationToken cancellationToken)
    {
        if (await GetConversationAsync(userId, conversationId, cancellationToken) is null)
        {
            return null;
        }

        var usage = await GetUsageAsync(userId, idempotencyKey, cancellationToken);
        if (usage is null)
        {
            return null;
        }

        EnsureSameRequest(usage.Value.Entry, conversationId, requestFingerprint);
        return await BuildTurnResultAsync(usage.Value, cancellationToken);
    }

    public async Task<ChatTurnResult> CompleteTurnAsync(
        string userId,
        string conversationId,
        string idempotencyKey,
        string requestFingerprint,
        string userContent,
        string assistantContent,
        int inputTokens,
        int outputTokens,
        string provider,
        string model,
        string correlationId,
        CancellationToken cancellationToken)
    {
        var existing = await GetUsageAsync(userId, idempotencyKey, cancellationToken);
        if (existing is not null)
        {
            EnsureSameRequest(existing.Value.Entry, conversationId, requestFingerprint);
            return await BuildTurnResultAsync(existing.Value, cancellationToken);
        }

        var conversation = await GetConversationAsync(userId, conversationId, cancellationToken)
            ?? throw new KeyNotFoundException("Conversation not found.");
        _ = await FindUserByIdAsync(userId, cancellationToken)
            ?? throw new KeyNotFoundException("User not found.");

        const int creditsCharged = 1;
        var now = DateTimeOffset.UtcNow;
        var title = conversation.Title == NewConversationTitle ? MakeTitle(userContent) : conversation.Title;
        var userMessage = new ChatMessage(Guid.NewGuid().ToString("N"), conversationId, "user", userContent, now);
        var assistantMessage = new ChatMessage(
            Guid.NewGuid().ToString("N"),
            conversationId,
            "assistant",
            assistantContent,
            now.AddTicks(1));
        var usage = new UsageEntry(
            idempotencyKey,
            userId,
            conversationId,
            requestFingerprint,
            userMessage.Id,
            assistantMessage.Id,
            creditsCharged,
            inputTokens,
            outputTokens,
            provider,
            model,
            now);
        var domainEvent = new DomainEventEnvelope(
            Guid.NewGuid().ToString("N"),
            "MessageTurnCompleted",
            1,
            now,
            "conversation",
            correlationId,
            conversationId,
            idempotencyKey,
            JsonSerializer.Serialize(new
            {
                conversationId,
                ownerId = userId,
                userMessageId = userMessage.Id,
                assistantMessageId = assistantMessage.Id,
                inputTokens,
                outputTokens,
                provider,
                model,
                creditsCharged
            }));

        // Order matters: cancellation reasons are reported by item index.
        var failure = await TryTransactAsync(
        [
            Put(_tables.UsageTable, ToItem(usage, MessageSortKey(userMessage), MessageSortKey(assistantMessage)),
                "attribute_not_exists(pk)"),
            new TransactWriteItem
            {
                Update = new Update
                {
                    TableName = _tables.ConversationsTable,
                    Key = Key(UserKey(userId), "PROFILE"),
                    ConditionExpression = "attribute_exists(pk) AND credits >= :charge",
                    UpdateExpression = "SET credits = credits - :charge",
                    ExpressionAttributeValues = new() { [":charge"] = N(creditsCharged) }
                }
            },
            new TransactWriteItem
            {
                Update = new Update
                {
                    TableName = _tables.ConversationsTable,
                    Key = Key(UserKey(userId), ConversationSortKey(conversationId)),
                    ConditionExpression = "attribute_exists(pk)",
                    UpdateExpression = "SET title = :title, updatedAt = :now",
                    ExpressionAttributeValues = new() { [":title"] = S(title), [":now"] = Date(now) }
                }
            },
            Put(_tables.ConversationsTable, ToItem(userMessage), "attribute_not_exists(pk)"),
            Put(_tables.ConversationsTable, ToItem(assistantMessage), "attribute_not_exists(pk)"),
            Put(_tables.ConversationsTable, ToItem(domainEvent), "attribute_not_exists(pk)"),
            Audit("message.turn.completed", userId, conversationId, correlationId)
        ], cancellationToken);

        if (failure is not null)
        {
            if (IsConditionFailure(failure, 0))
            {
                // A concurrent request with the same idempotency key won the race.
                var winner = await GetUsageAsync(userId, idempotencyKey, cancellationToken)
                    ?? throw new InvalidOperationException("The idempotent turn could not be read back.");
                EnsureSameRequest(winner.Entry, conversationId, requestFingerprint);
                return await BuildTurnResultAsync(winner, cancellationToken);
            }

            if (IsConditionFailure(failure, 1))
            {
                throw new InsufficientCreditsException();
            }

            throw new KeyNotFoundException("Conversation not found.");
        }

        return await BuildTurnResultAsync(
            (usage, MessageSortKey(userMessage), MessageSortKey(assistantMessage)),
            cancellationToken);
    }

    public async Task<IReadOnlyList<OutboxRecord>> GetPendingEventsAsync(
        int limit,
        CancellationToken cancellationToken)
    {
        if (limit is < 1 or > 100)
        {
            throw new ArgumentOutOfRangeException(nameof(limit), "Event batch size must be between 1 and 100.");
        }

        var response = await _client.QueryAsync(new QueryRequest
        {
            TableName = _tables.ConversationsTable,
            IndexName = DynamoDbTableOptions.PendingOutboxIndex,
            KeyConditionExpression = "gsi1pk = :pending",
            ExpressionAttributeValues = new() { [":pending"] = S(PendingOutboxPartition) },
            ScanIndexForward = true,
            Limit = limit
        }, cancellationToken);
        return (response.Items ?? []).Select(ToOutboxRecord).ToArray();
    }

    public Task MarkEventDispatchedAsync(string eventId, CancellationToken cancellationToken) =>
        UpdateOutboxAsync(
            eventId,
            "SET attempts = attempts + :one, dispatchedAt = :now REMOVE lastError, gsi1pk, gsi1sk",
            new() { [":one"] = N(1), [":now"] = Date(DateTimeOffset.UtcNow) },
            cancellationToken);

    public Task MarkEventDispatchFailedAsync(
        string eventId,
        string error,
        CancellationToken cancellationToken) =>
        UpdateOutboxAsync(
            eventId,
            "SET attempts = attempts + :one, lastError = :error",
            new() { [":one"] = N(1), [":error"] = S(error.Length <= 500 ? error : error[..500]) },
            cancellationToken);

    public async Task<bool> DeleteConversationAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken)
    {
        var conversation = await GetConversationAsync(userId, conversationId, cancellationToken);
        if (conversation is null)
        {
            return false;
        }

        var items = new List<TransactWriteItem>
        {
            new()
            {
                Delete = new Delete
                {
                    TableName = _tables.ConversationsTable,
                    Key = Key(UserKey(userId), ConversationSortKey(conversationId)),
                    ConditionExpression = "attribute_exists(pk)"
                }
            },
            Audit("conversation.deleted", userId, conversationId, userId)
        };
        if (conversation.ShareTokenHash is { } shareHash)
        {
            items.Add(DeleteItem(_tables.ConversationsTable, ShareKey(shareHash), "SHARE"));
        }

        if (await TryTransactAsync(items, cancellationToken) is not null)
        {
            return false;
        }

        // Children are unreachable once the owner-scoped conversation item is gone; remove them in batches.
        var childKeys = await QueryAllAsync(new QueryRequest
        {
            TableName = _tables.ConversationsTable,
            KeyConditionExpression = "pk = :pk",
            ExpressionAttributeValues = new() { [":pk"] = S(ConversationKey(conversationId)) },
            ProjectionExpression = "pk, sk",
            ConsistentRead = true
        }, cancellationToken);
        foreach (var chunk in childKeys.Chunk(25))
        {
            var requests = chunk
                .Select(key => new WriteRequest { DeleteRequest = new DeleteRequest { Key = key } })
                .ToList();
            await BatchWriteWithRetryAsync(_tables.ConversationsTable, requests, cancellationToken);
        }

        return true;
    }

    public async Task<bool> SetConversationShareAsync(
        string userId,
        string conversationId,
        string tokenHash,
        CancellationToken cancellationToken)
    {
        var conversation = await GetConversationAsync(userId, conversationId, cancellationToken);
        if (conversation is null)
        {
            return false;
        }

        var items = new List<TransactWriteItem>
        {
            new()
            {
                Update = new Update
                {
                    TableName = _tables.ConversationsTable,
                    Key = Key(UserKey(userId), ConversationSortKey(conversationId)),
                    ConditionExpression = "attribute_exists(pk)",
                    UpdateExpression = "SET shareTokenHash = :hash",
                    ExpressionAttributeValues = new() { [":hash"] = S(tokenHash) }
                }
            },
            Put(_tables.ConversationsTable, new()
            {
                ["pk"] = S(ShareKey(tokenHash)),
                ["sk"] = S("SHARE"),
                ["ownerId"] = S(userId),
                ["conversationId"] = S(conversationId)
            }),
            Audit("conversation.share.enabled", userId, conversationId, userId)
        };
        if (conversation.ShareTokenHash is { } previous && previous != tokenHash)
        {
            items.Add(DeleteItem(_tables.ConversationsTable, ShareKey(previous), "SHARE"));
        }

        return await TryTransactAsync(items, cancellationToken) is null;
    }

    public async Task<bool> RevokeConversationShareAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken)
    {
        var conversation = await GetConversationAsync(userId, conversationId, cancellationToken);
        if (conversation is null)
        {
            return false;
        }

        var items = new List<TransactWriteItem>
        {
            new()
            {
                Update = new Update
                {
                    TableName = _tables.ConversationsTable,
                    Key = Key(UserKey(userId), ConversationSortKey(conversationId)),
                    ConditionExpression = "attribute_exists(pk)",
                    UpdateExpression = "REMOVE shareTokenHash"
                }
            },
            Audit("conversation.share.revoked", userId, conversationId, userId)
        };
        if (conversation.ShareTokenHash is { } shareHash)
        {
            items.Add(DeleteItem(_tables.ConversationsTable, ShareKey(shareHash), "SHARE"));
        }

        return await TryTransactAsync(items, cancellationToken) is null;
    }

    public async Task<SharedConversation?> GetSharedConversationAsync(
        string tokenHash,
        CancellationToken cancellationToken)
    {
        var share = await GetAsync(_tables.ConversationsTable, ShareKey(tokenHash), "SHARE", cancellationToken);
        if (share is null)
        {
            return null;
        }

        var conversation = await GetConversationAsync(
            share["ownerId"].S,
            share["conversationId"].S,
            cancellationToken);
        if (conversation is null || conversation.ShareTokenHash != tokenHash)
        {
            return null;
        }

        return new SharedConversation(
            conversation.Title,
            await QueryMessagesAsync(conversation.Id, cancellationToken));
    }

    public async Task<ConversationFile> SaveConversationFileAsync(
        ConversationFile file,
        CancellationToken cancellationToken)
    {
        if (await GetConversationAsync(file.OwnerId, file.ConversationId, cancellationToken) is null)
        {
            throw new KeyNotFoundException("Conversation not found.");
        }

        var failure = await TryTransactAsync(
        [
            ConditionCheck(
                _tables.ConversationsTable,
                UserKey(file.OwnerId),
                ConversationSortKey(file.ConversationId)),
            Put(_tables.ConversationsTable, ToItem(file), "attribute_not_exists(pk)"),
            Audit("conversation.file_added", file.OwnerId, file.ConversationId, file.Id)
        ], cancellationToken);
        if (failure is not null)
        {
            throw IsConditionFailure(failure, 0)
                ? new KeyNotFoundException("Conversation not found.")
                : new InvalidOperationException("The file identifier already exists.");
        }

        return file;
    }

    public async Task<IReadOnlyList<ConversationFile>> GetConversationFilesAsync(
        string userId,
        string conversationId,
        IReadOnlyCollection<string> fileIds,
        CancellationToken cancellationToken)
    {
        var files = await ListConversationFilesAsync(userId, conversationId, cancellationToken);
        var requestedIds = fileIds.ToHashSet(StringComparer.Ordinal);
        var selected = files.Where(file => requestedIds.Contains(file.Id)).ToArray();
        if (selected.Length != requestedIds.Count)
        {
            throw new KeyNotFoundException("One or more attached files were not found in this conversation.");
        }

        return selected;
    }

    public async Task<IReadOnlyList<ConversationFile>> ListConversationFilesAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken)
    {
        if (await GetConversationAsync(userId, conversationId, cancellationToken) is null)
        {
            throw new KeyNotFoundException("Conversation not found.");
        }

        var items = await QueryAllAsync(new QueryRequest
        {
            TableName = _tables.ConversationsTable,
            KeyConditionExpression = "pk = :pk AND begins_with(sk, :file)",
            ExpressionAttributeValues = new()
            {
                [":pk"] = S(ConversationKey(conversationId)),
                [":file"] = S("FILE#")
            },
            ConsistentRead = true
        }, cancellationToken);
        return items
            .Select(ToFile)
            .Where(file => file.OwnerId == userId)
            .OrderBy(file => file.CreatedAt)
            .ToArray();
    }

    public async Task<bool> ApplyTurnToDailyUsageAsync(
        string eventId,
        string userId,
        DateOnly day,
        int inputTokens,
        int outputTokens,
        int creditsCharged,
        CancellationToken cancellationToken)
    {
        var failure = await TryTransactAsync(
        [
            Put(_tables.UsageTable, new()
            {
                ["pk"] = S($"EVENT#{eventId}"),
                ["sk"] = S("PROCESSED"),
                ["expiresAt"] = N(DateTimeOffset.UtcNow.AddDays(30).ToUnixTimeSeconds())
            }, "attribute_not_exists(pk)"),
            new TransactWriteItem
            {
                Update = new Update
                {
                    TableName = _tables.UsageTable,
                    Key = Key(UserKey(userId), DailySortKey(day)),
                    UpdateExpression =
                        "ADD turns :one, inputTokens :input, outputTokens :output, creditsCharged :credits",
                    ExpressionAttributeValues = new()
                    {
                        [":one"] = N(1),
                        [":input"] = N(inputTokens),
                        [":output"] = N(outputTokens),
                        [":credits"] = N(creditsCharged)
                    }
                }
            }
        ], cancellationToken);
        return failure is null;
    }

    public async Task<DailyUsage?> GetDailyUsageAsync(
        string userId,
        DateOnly day,
        CancellationToken cancellationToken)
    {
        var item = await GetAsync(_tables.UsageTable, UserKey(userId), DailySortKey(day), cancellationToken);
        return item is null
            ? null
            : new DailyUsage(
                userId,
                day,
                Int(item["turns"]),
                Long(item["inputTokens"]),
                Long(item["outputTokens"]),
                Long(item["creditsCharged"]));
    }

    private async Task<Conversation?> GetConversationAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken)
    {
        var item = await GetAsync(
            _tables.ConversationsTable,
            UserKey(userId),
            ConversationSortKey(conversationId),
            cancellationToken);
        return item is null ? null : ToConversation(item);
    }

    private async Task<IReadOnlyList<Conversation>> QueryConversationsAsync(
        string userId,
        CancellationToken cancellationToken) =>
        (await QueryAllAsync(new QueryRequest
        {
            TableName = _tables.ConversationsTable,
            KeyConditionExpression = "pk = :pk AND begins_with(sk, :conv)",
            ExpressionAttributeValues = new() { [":pk"] = S(UserKey(userId)), [":conv"] = S("CONV#") },
            ConsistentRead = true
        }, cancellationToken)).Select(ToConversation).ToArray();

    private async Task<IReadOnlyList<ChatMessage>> QueryMessagesAsync(
        string conversationId,
        CancellationToken cancellationToken) =>
        (await QueryAllAsync(new QueryRequest
        {
            TableName = _tables.ConversationsTable,
            KeyConditionExpression = "pk = :pk AND begins_with(sk, :msg)",
            ExpressionAttributeValues = new()
            {
                [":pk"] = S(ConversationKey(conversationId)),
                [":msg"] = S("MSG#")
            },
            ConsistentRead = true
        }, cancellationToken)).Select(ToMessage).ToArray();

    private async Task<(UsageEntry Entry, string UserMessageSortKey, string AssistantMessageSortKey)?> GetUsageAsync(
        string userId,
        string idempotencyKey,
        CancellationToken cancellationToken)
    {
        var item = await GetAsync(
            _tables.UsageTable,
            UserKey(userId),
            $"IDEM#{idempotencyKey}",
            cancellationToken);
        return item is null
            ? null
            : (ToUsage(item), item["userMessageSk"].S, item["assistantMessageSk"].S);
    }

    private async Task<ChatTurnResult> BuildTurnResultAsync(
        (UsageEntry Entry, string UserMessageSortKey, string AssistantMessageSortKey) usage,
        CancellationToken cancellationToken)
    {
        var entry = usage.Entry;
        var conversation = await GetConversationAsync(entry.UserId, entry.ConversationId, cancellationToken)
            ?? throw new InvalidDataException("A usage record refers to a missing conversation.");
        var userMessage = await GetAsync(
                _tables.ConversationsTable,
                ConversationKey(entry.ConversationId),
                usage.UserMessageSortKey,
                cancellationToken)
            ?? throw new InvalidDataException("A usage record refers to a missing user message.");
        var assistantMessage = await GetAsync(
                _tables.ConversationsTable,
                ConversationKey(entry.ConversationId),
                usage.AssistantMessageSortKey,
                cancellationToken)
            ?? throw new InvalidDataException("A usage record refers to a missing assistant message.");
        var user = await FindUserByIdAsync(entry.UserId, cancellationToken)
            ?? throw new InvalidDataException("A usage record refers to a missing user.");
        return new ChatTurnResult(
            conversation,
            ToMessage(userMessage),
            ToMessage(assistantMessage),
            user.Credits,
            entry.CreditsCharged,
            entry.InputTokens,
            entry.OutputTokens,
            entry.Provider,
            entry.Model);
    }

    private static void EnsureSameRequest(UsageEntry usage, string conversationId, string requestFingerprint)
    {
        if (usage.ConversationId != conversationId || usage.RequestFingerprint != requestFingerprint)
        {
            throw new IdempotencyConflictException();
        }
    }

    private async Task UpdateOutboxAsync(
        string eventId,
        string updateExpression,
        Dictionary<string, AttributeValue> values,
        CancellationToken cancellationToken)
    {
        try
        {
            await _client.UpdateItemAsync(new UpdateItemRequest
            {
                TableName = _tables.ConversationsTable,
                Key = Key(OutboxKey(eventId), "EVENT"),
                ConditionExpression = "attribute_exists(pk)",
                UpdateExpression = updateExpression,
                ExpressionAttributeValues = values
            }, cancellationToken);
        }
        catch (ConditionalCheckFailedException)
        {
            throw new KeyNotFoundException($"Outbox event '{eventId}' was not found.");
        }
    }

    private async Task<Dictionary<string, AttributeValue>?> GetAsync(
        string table,
        string pk,
        string sk,
        CancellationToken cancellationToken)
    {
        var response = await _client.GetItemAsync(new GetItemRequest
        {
            TableName = table,
            Key = Key(pk, sk),
            ConsistentRead = true
        }, cancellationToken);
        return response.Item is { Count: > 0 } item ? item : null;
    }

    private async Task<List<Dictionary<string, AttributeValue>>> QueryAllAsync(
        QueryRequest request,
        CancellationToken cancellationToken)
    {
        var items = new List<Dictionary<string, AttributeValue>>();
        do
        {
            var response = await _client.QueryAsync(request, cancellationToken);
            items.AddRange(response.Items ?? []);
            request.ExclusiveStartKey = response.LastEvaluatedKey is { Count: > 0 } next ? next : null;
        }
        while (request.ExclusiveStartKey is not null);

        return items;
    }

    private async Task BatchWriteWithRetryAsync(
        string table,
        List<WriteRequest> requests,
        CancellationToken cancellationToken)
    {
        var pending = new Dictionary<string, List<WriteRequest>> { [table] = requests };
        for (var attempt = 0; pending.Count > 0; attempt++)
        {
            if (attempt > 0)
            {
                if (attempt > 8)
                {
                    throw new InvalidOperationException("DynamoDB batch write did not complete after retries.");
                }

                await Task.Delay(TimeSpan.FromMilliseconds(50 * Math.Pow(2, attempt)), cancellationToken);
            }

            var response = await _client.BatchWriteItemAsync(
                new BatchWriteItemRequest { RequestItems = pending },
                cancellationToken);
            pending = response.UnprocessedItems is { Count: > 0 } unprocessed ? unprocessed : [];
        }
    }

    private async Task TransactAsync(List<TransactWriteItem> items, CancellationToken cancellationToken)
    {
        if (await TryTransactAsync(items, cancellationToken) is not null)
        {
            throw new InvalidOperationException("The DynamoDB write was rejected by a condition check.");
        }
    }

    private async Task<TransactionCanceledException?> TryTransactAsync(
        List<TransactWriteItem> items,
        CancellationToken cancellationToken)
    {
        try
        {
            await _client.TransactWriteItemsAsync(
                new TransactWriteItemsRequest { TransactItems = items },
                cancellationToken);
            return null;
        }
        catch (TransactionCanceledException exception)
            when (exception.CancellationReasons?.Any(reason => reason.Code == ConditionalCheckFailed) == true)
        {
            return exception;
        }
    }

    private static bool IsConditionFailure(TransactionCanceledException exception, int index) =>
        exception.CancellationReasons is { } reasons
        && reasons.Count > index
        && reasons[index].Code == ConditionalCheckFailed;

    private TransactWriteItem Audit(string eventType, string actorId, string resourceId, string correlationId)
    {
        var now = DateTimeOffset.UtcNow;
        var eventId = Guid.NewGuid().ToString("N");
        return Put(_tables.AuditTable, new()
        {
            ["pk"] = S($"RESOURCE#{resourceId}"),
            ["sk"] = S($"{Ticks(now)}#{eventId}"),
            ["eventId"] = S(eventId),
            ["correlationId"] = S(correlationId),
            ["eventType"] = S(eventType),
            ["actorId"] = S(actorId),
            ["resourceId"] = S(resourceId),
            ["occurredAt"] = Date(now),
            ["expiresAt"] = N(now.AddDays(_tables.AuditRetentionDays).ToUnixTimeSeconds())
        });
    }

    private static TransactWriteItem Put(
        string table,
        Dictionary<string, AttributeValue> item,
        string? condition = null) =>
        new() { Put = new Put { TableName = table, Item = item, ConditionExpression = condition } };

    private static TransactWriteItem DeleteItem(string table, string pk, string sk) =>
        new() { Delete = new Delete { TableName = table, Key = Key(pk, sk) } };

    private static TransactWriteItem ConditionCheck(string table, string pk, string sk) =>
        new()
        {
            ConditionCheck = new ConditionCheck
            {
                TableName = table,
                Key = Key(pk, sk),
                ConditionExpression = "attribute_exists(pk)"
            }
        };

    private static Dictionary<string, AttributeValue> ToItem(Conversation conversation)
    {
        var item = new Dictionary<string, AttributeValue>
        {
            ["pk"] = S(UserKey(conversation.OwnerId)),
            ["sk"] = S(ConversationSortKey(conversation.Id)),
            ["id"] = S(conversation.Id),
            ["ownerId"] = S(conversation.OwnerId),
            ["title"] = S(conversation.Title),
            ["createdAt"] = Date(conversation.CreatedAt),
            ["updatedAt"] = Date(conversation.UpdatedAt),
            ["isArchived"] = new AttributeValue { BOOL = conversation.IsArchived }
        };
        if (conversation.ShareTokenHash is { } hash)
        {
            item["shareTokenHash"] = S(hash);
        }

        return item;
    }

    private static Dictionary<string, AttributeValue> ToItem(ChatMessage message) => new()
    {
        ["pk"] = S(ConversationKey(message.ConversationId)),
        ["sk"] = S(MessageSortKey(message)),
        ["id"] = S(message.Id),
        ["conversationId"] = S(message.ConversationId),
        ["role"] = S(message.Role),
        ["content"] = S(message.Content),
        ["createdAt"] = Date(message.CreatedAt)
    };

    private static Dictionary<string, AttributeValue> ToItem(
        UsageEntry usage,
        string userMessageSortKey,
        string assistantMessageSortKey) => new()
    {
        ["pk"] = S(UserKey(usage.UserId)),
        ["sk"] = S($"IDEM#{usage.IdempotencyKey}"),
        ["gsi1pk"] = S(UserKey(usage.UserId)),
        ["gsi1sk"] = S($"{Ticks(usage.CreatedAt)}#{usage.IdempotencyKey}"),
        ["idempotencyKey"] = S(usage.IdempotencyKey),
        ["userId"] = S(usage.UserId),
        ["conversationId"] = S(usage.ConversationId),
        ["requestFingerprint"] = S(usage.RequestFingerprint),
        ["userMessageId"] = S(usage.UserMessageId),
        ["assistantMessageId"] = S(usage.AssistantMessageId),
        ["userMessageSk"] = S(userMessageSortKey),
        ["assistantMessageSk"] = S(assistantMessageSortKey),
        ["creditsCharged"] = N(usage.CreditsCharged),
        ["inputTokens"] = N(usage.InputTokens),
        ["outputTokens"] = N(usage.OutputTokens),
        ["provider"] = S(usage.Provider),
        ["model"] = S(usage.Model),
        ["createdAt"] = Date(usage.CreatedAt)
    };

    private static Dictionary<string, AttributeValue> ToItem(DomainEventEnvelope domainEvent) => new()
    {
        ["pk"] = S(OutboxKey(domainEvent.EventId)),
        ["sk"] = S("EVENT"),
        ["gsi1pk"] = S(PendingOutboxPartition),
        ["gsi1sk"] = S($"{Ticks(domainEvent.OccurredAt)}#{domainEvent.EventId}"),
        ["eventId"] = S(domainEvent.EventId),
        ["eventType"] = S(domainEvent.EventType),
        ["eventVersion"] = N(domainEvent.EventVersion),
        ["occurredAt"] = Date(domainEvent.OccurredAt),
        ["producer"] = S(domainEvent.Producer),
        ["correlationId"] = S(domainEvent.CorrelationId),
        ["aggregateId"] = S(domainEvent.AggregateId),
        ["idempotencyKey"] = S(domainEvent.IdempotencyKey),
        ["dataJson"] = S(domainEvent.DataJson),
        ["attempts"] = N(0)
    };

    private static Dictionary<string, AttributeValue> ToItem(ConversationFile file) => new()
    {
        ["pk"] = S(ConversationKey(file.ConversationId)),
        ["sk"] = S($"FILE#{file.Id}"),
        ["id"] = S(file.Id),
        ["ownerId"] = S(file.OwnerId),
        ["conversationId"] = S(file.ConversationId),
        ["fileName"] = S(file.FileName),
        ["contentType"] = S(file.ContentType),
        ["sizeBytes"] = N(file.SizeBytes),
        ["sha256"] = S(file.Sha256),
        ["extractedText"] = S(file.ExtractedText),
        ["createdAt"] = Date(file.CreatedAt)
    };

    private static LocalUser ToUser(Dictionary<string, AttributeValue> item) => new(
        item["id"].S,
        item["email"].S,
        item["passwordHash"].S,
        Int(item["credits"]),
        ParseDate(item["createdAt"]));

    private static Conversation ToConversation(Dictionary<string, AttributeValue> item) => new(
        item["id"].S,
        item["ownerId"].S,
        item["title"].S,
        ParseDate(item["createdAt"]),
        ParseDate(item["updatedAt"]),
        item["isArchived"].BOOL == true,
        item.TryGetValue("shareTokenHash", out var hash) ? hash.S : null);

    private static ChatMessage ToMessage(Dictionary<string, AttributeValue> item) => new(
        item["id"].S,
        item["conversationId"].S,
        item["role"].S,
        item["content"].S,
        ParseDate(item["createdAt"]));

    private static UsageEntry ToUsage(Dictionary<string, AttributeValue> item) => new(
        item["idempotencyKey"].S,
        item["userId"].S,
        item["conversationId"].S,
        item["requestFingerprint"].S,
        item["userMessageId"].S,
        item["assistantMessageId"].S,
        Int(item["creditsCharged"]),
        Int(item["inputTokens"]),
        Int(item["outputTokens"]),
        item["provider"].S,
        item["model"].S,
        ParseDate(item["createdAt"]));

    private static ConversationFile ToFile(Dictionary<string, AttributeValue> item) => new(
        item["id"].S,
        item["ownerId"].S,
        item["conversationId"].S,
        item["fileName"].S,
        item["contentType"].S,
        Long(item["sizeBytes"]),
        item["sha256"].S,
        item["extractedText"].S,
        ParseDate(item["createdAt"]));

    private static OutboxRecord ToOutboxRecord(Dictionary<string, AttributeValue> item) => new(
        new DomainEventEnvelope(
            item["eventId"].S,
            item["eventType"].S,
            Int(item["eventVersion"]),
            ParseDate(item["occurredAt"]),
            item["producer"].S,
            item["correlationId"].S,
            item["aggregateId"].S,
            item["idempotencyKey"].S,
            item["dataJson"].S),
        Int(item["attempts"]),
        item.TryGetValue("dispatchedAt", out var dispatchedAt) ? ParseDate(dispatchedAt) : null,
        item.TryGetValue("lastError", out var lastError) ? lastError.S : null);

    private static ConversationSummary ToSummary(Conversation conversation) => new(
        conversation.Id,
        conversation.Title,
        conversation.CreatedAt,
        conversation.UpdatedAt,
        conversation.IsArchived);

    private static string MakeTitle(string content)
    {
        var normalized = string.Join(' ', content.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        return normalized.Length <= 48 ? normalized : $"{normalized[..45]}...";
    }

    private static string UserKey(string userId) => $"USER#{userId}";
    private static string EmailKey(string email) => $"EMAIL#{email.ToUpperInvariant()}";
    private static string ConversationKey(string conversationId) => $"CONV#{conversationId}";
    private static string ConversationSortKey(string conversationId) => $"CONV#{conversationId}";
    private static string ShareKey(string tokenHash) => $"SHARE#{tokenHash}";
    private static string OutboxKey(string eventId) => $"OUTBOX#{eventId}";
    private static string MessageSortKey(ChatMessage message) => $"MSG#{Ticks(message.CreatedAt)}#{message.Id}";
    private static string DailySortKey(DateOnly day) => $"DAILY#{day:yyyy-MM-dd}";
    private static string Ticks(DateTimeOffset value) =>
        value.UtcTicks.ToString("D19", CultureInfo.InvariantCulture);

    private static Dictionary<string, AttributeValue> Key(string pk, string sk) => new()
    {
        ["pk"] = S(pk),
        ["sk"] = S(sk)
    };

    private static AttributeValue S(string value) => new() { S = value };
    private static AttributeValue N(long value) => new() { N = value.ToString(CultureInfo.InvariantCulture) };
    private static AttributeValue Date(DateTimeOffset value) =>
        S(value.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture));
    private static DateTimeOffset ParseDate(AttributeValue value) =>
        DateTimeOffset.Parse(value.S, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);
    private static int Int(AttributeValue value) => int.Parse(value.N, CultureInfo.InvariantCulture);
    private static long Long(AttributeValue value) => long.Parse(value.N, CultureInfo.InvariantCulture);
}
