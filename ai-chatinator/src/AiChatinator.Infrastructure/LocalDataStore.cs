using System.Text.Json;
using AiChatinator.Application;
using AiChatinator.Domain;

namespace AiChatinator.Infrastructure;

public sealed class LocalDataStore : IChatStore, IOutboxStore
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true
    };

    private readonly string _dataPath;
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private DatabaseState _state;

    public LocalDataStore(string dataPath)
    {
        _dataPath = Path.GetFullPath(dataPath);
        Directory.CreateDirectory(Path.GetDirectoryName(_dataPath)!);
        _state = LoadState(_dataPath);
    }

    public Task<LocalUser?> FindUserByEmailAsync(string email, CancellationToken cancellationToken) =>
        ReadAsync(state => state.Users.FirstOrDefault(
            user => string.Equals(user.Email, email, StringComparison.OrdinalIgnoreCase)), cancellationToken);

    public Task<LocalUser?> FindUserByIdAsync(string userId, CancellationToken cancellationToken) =>
        ReadAsync(state => state.Users.FirstOrDefault(user => user.Id == userId), cancellationToken);

    public Task<bool> AddUserAsync(LocalUser user, CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            if (state.Users.Any(existing =>
                    string.Equals(existing.Email, user.Email, StringComparison.OrdinalIgnoreCase)))
            {
                return false;
            }

            state.Users.Add(user);
            AddAudit(state, "user.registered", user.Id, user.Id, user.Id);
            return true;
        }, cancellationToken);

    public Task<IReadOnlyList<ConversationSummary>> ListConversationsAsync(
        string userId,
        CancellationToken cancellationToken) =>
        ReadAsync<IReadOnlyList<ConversationSummary>>(state => state.Conversations
            .Where(conversation => conversation.OwnerId == userId)
            .OrderByDescending(conversation => conversation.UpdatedAt)
            .Select(conversation => new ConversationSummary(
                conversation.Id,
                conversation.Title,
                conversation.CreatedAt,
                conversation.UpdatedAt,
                conversation.IsArchived))
            .ToArray(), cancellationToken);

    public Task<IReadOnlyList<ConversationMessageMatch>> SearchOtherConversationMessagesAsync(
        string userId,
        string currentConversationId,
        string query,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(query) || query.Length > 160)
        {
            throw new InvalidDataException("The conversation search query must be 1 to 160 characters.");
        }

        return ReadAsync<IReadOnlyList<ConversationMessageMatch>>(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == currentConversationId && conversation.OwnerId == userId))
            {
                throw new KeyNotFoundException("Conversation not found.");
            }

            var conversations = state.Conversations
                .Where(conversation => conversation.OwnerId == userId
                    && conversation.Id != currentConversationId)
                .ToDictionary(conversation => conversation.Id);
            return state.Messages
                .Where(message => conversations.ContainsKey(message.ConversationId)
                    && message.Content.Contains(query, StringComparison.OrdinalIgnoreCase))
                .OrderByDescending(message => message.CreatedAt)
                .ThenBy(message => message.Id, StringComparer.Ordinal)
                .Take(5)
                .Select(message =>
                {
                    var matchIndex = message.Content.IndexOf(query, StringComparison.OrdinalIgnoreCase);
                    var start = Math.Max(0, matchIndex - 60);
                    var excerpt = message.Content.Substring(start, Math.Min(300, message.Content.Length - start));
                    var title = conversations[message.ConversationId].Title;
                    return new ConversationMessageMatch(
                        message.ConversationId,
                        title.Length > 100 ? title[..100] : title,
                        message.Id,
                        message.Role,
                        excerpt,
                        message.CreatedAt);
                })
                .ToArray();
        }, cancellationToken);
    }

    public Task<ConversationSummary> CreateConversationAsync(
        string userId,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var now = DateTimeOffset.UtcNow;
            var conversation = new Conversation(
                Guid.NewGuid().ToString("N"),
                userId,
                "New conversation",
                now,
                now,
                false,
                null);
            state.Conversations.Add(conversation);
            AddAudit(state, "conversation.created", userId, conversation.Id, userId);
            return ToSummary(conversation);
        }, cancellationToken);

    public Task<bool> SetConversationArchivedAsync(
        string userId,
        string conversationId,
        bool isArchived,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var index = state.Conversations.FindIndex(conversation =>
                conversation.Id == conversationId && conversation.OwnerId == userId);
            if (index < 0)
            {
                return false;
            }

            var conversation = state.Conversations[index] with
            {
                IsArchived = isArchived,
                UpdatedAt = DateTimeOffset.UtcNow
            };
            state.Conversations[index] = conversation;
            AddAudit(
                state,
                isArchived ? "conversation.archived" : "conversation.restored",
                userId,
                conversationId,
                userId);
            return true;
        }, cancellationToken);

    public Task<IReadOnlyList<UsageHistoryItem>> GetUsageHistoryAsync(
        string userId,
        CancellationToken cancellationToken) =>
        ReadAsync<IReadOnlyList<UsageHistoryItem>>(state => state.Usage
            .Where(usage => usage.UserId == userId)
            .OrderByDescending(usage => usage.CreatedAt)
            .Take(100)
            .Select(usage => new UsageHistoryItem(
                usage.ConversationId,
                usage.CreditsCharged,
                usage.InputTokens,
                usage.OutputTokens,
                usage.Provider,
                usage.Model,
                usage.CreatedAt))
            .ToArray(), cancellationToken);

    public Task<ConversationFile> SaveConversationFileAsync(
        ConversationFile file,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == file.ConversationId && conversation.OwnerId == file.OwnerId))
            {
                throw new KeyNotFoundException("Conversation not found.");
            }

            if (state.ConversationFiles.Any(existing => existing.Id == file.Id))
            {
                throw new InvalidOperationException("The file identifier already exists.");
            }

            state.ConversationFiles.Add(file);
            AddAudit(state, "conversation.file_added", file.OwnerId, file.ConversationId, file.Id);
            return file;
        }, cancellationToken);

    public Task<IReadOnlyList<ConversationFile>> GetConversationFilesAsync(
        string userId,
        string conversationId,
        IReadOnlyCollection<string> fileIds,
        CancellationToken cancellationToken) =>
        ReadAsync<IReadOnlyList<ConversationFile>>(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == conversationId && conversation.OwnerId == userId))
            {
                throw new KeyNotFoundException("Conversation not found.");
            }

            var requestedIds = fileIds.ToHashSet(StringComparer.Ordinal);
            var files = state.ConversationFiles
                .Where(file => file.OwnerId == userId
                    && file.ConversationId == conversationId
                    && requestedIds.Contains(file.Id))
                .ToArray();
            if (files.Length != requestedIds.Count)
            {
                throw new KeyNotFoundException("One or more attached files were not found in this conversation.");
            }

            return files;
        }, cancellationToken);

    public Task<IReadOnlyList<ConversationFile>> ListConversationFilesAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken) =>
        ReadAsync<IReadOnlyList<ConversationFile>>(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == conversationId && conversation.OwnerId == userId))
            {
                throw new KeyNotFoundException("Conversation not found.");
            }

            return state.ConversationFiles
                .Where(file => file.OwnerId == userId && file.ConversationId == conversationId)
                .OrderBy(file => file.CreatedAt)
                .ToArray();
        }, cancellationToken);

    public Task<IReadOnlyList<ChatMessage>?> ListMessagesAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken) =>
        ReadAsync<IReadOnlyList<ChatMessage>?>(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == conversationId && conversation.OwnerId == userId))
            {
                return null;
            }

            return state.Messages
                .Where(message => message.ConversationId == conversationId)
                .OrderBy(message => message.CreatedAt)
                .ThenBy(message => message.Id, StringComparer.Ordinal)
                .ToArray();
        }, cancellationToken);

    public Task<ContextSummary?> GetContextSummaryAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken) =>
        ReadAsync<ContextSummary?>(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == conversationId && conversation.OwnerId == userId))
            {
                return null;
            }

            return state.ContextSummaries.FirstOrDefault(
                summary => summary.ConversationId == conversationId);
        }, cancellationToken);

    public Task<bool> SaveContextSummaryAsync(
        string userId,
        string conversationId,
        int expectedVersion,
        string throughMessageId,
        string content,
        string model,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == conversationId && conversation.OwnerId == userId))
            {
                return false;
            }

            if (!state.Messages.Any(message =>
                    message.Id == throughMessageId && message.ConversationId == conversationId))
            {
                throw new InvalidDataException(
                    "A context summary must refer to a message in its conversation.");
            }

            var existingIndex = state.ContextSummaries.FindIndex(
                summary => summary.ConversationId == conversationId);
            var actualVersion = existingIndex < 0
                ? 0
                : state.ContextSummaries[existingIndex].Version;
            if (actualVersion != expectedVersion)
            {
                return false;
            }

            var summary = new ContextSummary(
                conversationId,
                actualVersion + 1,
                throughMessageId,
                content,
                model,
                DateTimeOffset.UtcNow);
            if (existingIndex < 0)
            {
                state.ContextSummaries.Add(summary);
            }
            else
            {
                state.ContextSummaries[existingIndex] = summary;
            }

            AddAudit(state, "conversation.context_summarized", userId, conversationId, userId);
            return true;
        }, cancellationToken);

    public Task<ChatTurnResult?> FindCompletedTurnAsync(
        string userId,
        string idempotencyKey,
        string conversationId,
        string requestFingerprint,
        CancellationToken cancellationToken) =>
        ReadAsync<ChatTurnResult?>(state =>
        {
            if (!state.Conversations.Any(conversation =>
                    conversation.Id == conversationId && conversation.OwnerId == userId))
            {
                return null;
            }

            var usage = state.Usage.FirstOrDefault(entry =>
                entry.UserId == userId && entry.IdempotencyKey == idempotencyKey);
            if (usage is null)
            {
                return null;
            }

            if (usage.ConversationId != conversationId)
            {
                throw new IdempotencyConflictException();
            }

            if (usage.RequestFingerprint != requestFingerprint)
            {
                throw new IdempotencyConflictException();
            }

            return BuildTurnResult(state, usage);
        }, cancellationToken);

    public Task<ChatTurnResult> CompleteTurnAsync(
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
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var existing = state.Usage.FirstOrDefault(entry =>
                entry.UserId == userId && entry.IdempotencyKey == idempotencyKey);
            if (existing is not null)
            {
                if (existing.ConversationId != conversationId)
                {
                    throw new IdempotencyConflictException();
                }

                if (existing.RequestFingerprint != requestFingerprint)
                {
                    throw new IdempotencyConflictException();
                }

                return BuildTurnResult(state, existing);
            }

            var conversationIndex = state.Conversations.FindIndex(conversation =>
                conversation.Id == conversationId && conversation.OwnerId == userId);
            if (conversationIndex < 0)
            {
                throw new KeyNotFoundException("Conversation not found.");
            }

            var userIndex = state.Users.FindIndex(user => user.Id == userId);
            if (userIndex < 0)
            {
                throw new KeyNotFoundException("User not found.");
            }

            var user = state.Users[userIndex];
            const int creditsCharged = 1;
            if (user.Credits < creditsCharged)
            {
                throw new InsufficientCreditsException();
            }

            var now = DateTimeOffset.UtcNow;
            var conversation = state.Conversations[conversationIndex];
            var title = conversation.Title == "New conversation"
                ? MakeTitle(userContent)
                : conversation.Title;
            conversation = conversation with { Title = title, UpdatedAt = now };
            state.Conversations[conversationIndex] = conversation;

            var userMessage = new ChatMessage(
                Guid.NewGuid().ToString("N"),
                conversationId,
                "user",
                userContent,
                now);
            var assistantMessage = new ChatMessage(
                Guid.NewGuid().ToString("N"),
                conversationId,
                "assistant",
                assistantContent,
                now.AddTicks(1));
            state.Messages.Add(userMessage);
            state.Messages.Add(assistantMessage);
            state.Users[userIndex] = user with { Credits = user.Credits - creditsCharged };

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
            state.Usage.Add(usage);
            AddAudit(state, "message.turn.completed", userId, conversationId, correlationId);
            state.Outbox.Add(new OutboxRecord(
                new DomainEventEnvelope(
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
                    })),
                0,
                null,
                null));
            return BuildTurnResult(state, usage);
        }, cancellationToken);

    public Task<IReadOnlyList<OutboxRecord>> GetPendingEventsAsync(
        int limit,
        CancellationToken cancellationToken)
    {
        if (limit is < 1 or > 100)
        {
            throw new ArgumentOutOfRangeException(nameof(limit), "Event batch size must be between 1 and 100.");
        }

        return ReadAsync<IReadOnlyList<OutboxRecord>>(state => state.Outbox
            .Where(record => record.DispatchedAt is null)
            .OrderBy(record => record.Event.OccurredAt)
            .Take(limit)
            .ToArray(), cancellationToken);
    }

    public Task MarkEventDispatchedAsync(string eventId, CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var index = state.Outbox.FindIndex(record => record.Event.EventId == eventId);
            if (index < 0)
            {
                throw new KeyNotFoundException($"Outbox event '{eventId}' was not found.");
            }

            var record = state.Outbox[index];
            state.Outbox[index] = record with
            {
                Attempts = record.Attempts + 1,
                DispatchedAt = DateTimeOffset.UtcNow,
                LastError = null
            };
            return true;
        }, cancellationToken);

    public Task MarkEventDispatchFailedAsync(
        string eventId,
        string error,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var index = state.Outbox.FindIndex(record => record.Event.EventId == eventId);
            if (index < 0)
            {
                throw new KeyNotFoundException($"Outbox event '{eventId}' was not found.");
            }

            var record = state.Outbox[index];
            state.Outbox[index] = record with
            {
                Attempts = record.Attempts + 1,
                LastError = error.Length <= 500 ? error : error[..500]
            };
            return true;
        }, cancellationToken);

    public Task<bool> DeleteConversationAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var removed = state.Conversations.RemoveAll(conversation =>
                conversation.Id == conversationId && conversation.OwnerId == userId);
            if (removed == 0)
            {
                return false;
            }

            state.Messages.RemoveAll(message => message.ConversationId == conversationId);
            state.ContextSummaries.RemoveAll(summary => summary.ConversationId == conversationId);
            state.ConversationFiles.RemoveAll(file => file.ConversationId == conversationId);
            AddAudit(state, "conversation.deleted", userId, conversationId, userId);
            return true;
        }, cancellationToken);

    public Task<bool> SetConversationShareAsync(
        string userId,
        string conversationId,
        string tokenHash,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var index = state.Conversations.FindIndex(conversation =>
                conversation.Id == conversationId && conversation.OwnerId == userId);
            if (index < 0)
            {
                return false;
            }

            state.Conversations[index] = state.Conversations[index] with { ShareTokenHash = tokenHash };
            AddAudit(state, "conversation.share.enabled", userId, conversationId, userId);
            return true;
        }, cancellationToken);

    public Task<bool> RevokeConversationShareAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken) =>
        WriteAsync(state =>
        {
            var index = state.Conversations.FindIndex(conversation =>
                conversation.Id == conversationId && conversation.OwnerId == userId);
            if (index < 0)
            {
                return false;
            }

            state.Conversations[index] = state.Conversations[index] with { ShareTokenHash = null };
            AddAudit(state, "conversation.share.revoked", userId, conversationId, userId);
            return true;
        }, cancellationToken);

    public Task<SharedConversation?> GetSharedConversationAsync(
        string tokenHash,
        CancellationToken cancellationToken) =>
        ReadAsync<SharedConversation?>(state =>
        {
            var conversation = state.Conversations.FirstOrDefault(candidate =>
                candidate.ShareTokenHash == tokenHash);
            if (conversation is null)
            {
                return null;
            }

            var messages = state.Messages
                .Where(message => message.ConversationId == conversation.Id)
                .OrderBy(message => message.CreatedAt)
                .ThenBy(message => message.Id, StringComparer.Ordinal)
                .ToArray();
            return new SharedConversation(conversation.Title, messages);
        }, cancellationToken);

    private async Task<T> ReadAsync<T>(Func<DatabaseState, T> action, CancellationToken cancellationToken)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            return action(_state);
        }
        finally
        {
            _mutex.Release();
        }
    }

    private async Task<T> WriteAsync<T>(Func<DatabaseState, T> action, CancellationToken cancellationToken)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var nextState = JsonSerializer.Deserialize<DatabaseState>(
                    JsonSerializer.SerializeToUtf8Bytes(_state, JsonOptions),
                    JsonOptions)
                ?? throw new InvalidDataException("Unable to copy the local database state.");
            var result = action(nextState);
            await SaveStateAsync(nextState, cancellationToken);
            _state = nextState;
            return result;
        }
        finally
        {
            _mutex.Release();
        }
    }

    private async Task SaveStateAsync(DatabaseState state, CancellationToken cancellationToken)
    {
        var temporaryPath = $"{_dataPath}.tmp";
        await using (var stream = new FileStream(
                         temporaryPath,
                         FileMode.Create,
                         FileAccess.Write,
                         FileShare.None,
                         4096,
                         FileOptions.Asynchronous | FileOptions.WriteThrough))
        {
            await JsonSerializer.SerializeAsync(stream, state, JsonOptions, cancellationToken);
            await stream.FlushAsync(cancellationToken);
        }

        File.Move(temporaryPath, _dataPath, overwrite: true);
    }

    private static DatabaseState LoadState(string dataPath)
    {
        if (!File.Exists(dataPath))
        {
            return new DatabaseState();
        }

        using var stream = File.OpenRead(dataPath);
        return JsonSerializer.Deserialize<DatabaseState>(stream, JsonOptions)
            ?? throw new InvalidDataException($"Local data file '{dataPath}' contains no database state.");
    }

    private static ChatTurnResult BuildTurnResult(DatabaseState state, UsageEntry usage)
    {
        var conversation = state.Conversations.FirstOrDefault(item => item.Id == usage.ConversationId)
            ?? throw new InvalidDataException("A usage record refers to a missing conversation.");
        var userMessage = state.Messages.FirstOrDefault(item => item.Id == usage.UserMessageId)
            ?? throw new InvalidDataException("A usage record refers to a missing user message.");
        var assistantMessage = state.Messages.FirstOrDefault(item => item.Id == usage.AssistantMessageId)
            ?? throw new InvalidDataException("A usage record refers to a missing assistant message.");
        var creditsRemaining = state.Users.FirstOrDefault(item => item.Id == usage.UserId)?.Credits
            ?? throw new InvalidDataException("A usage record refers to a missing user.");
        return new ChatTurnResult(
            conversation,
            userMessage,
            assistantMessage,
            creditsRemaining,
            usage.CreditsCharged,
            usage.InputTokens,
            usage.OutputTokens,
            usage.Provider,
            usage.Model);
    }

    private static ConversationSummary ToSummary(Conversation conversation) => new(
        conversation.Id,
        conversation.Title,
        conversation.CreatedAt,
        conversation.UpdatedAt,
        conversation.IsArchived);

    private static string MakeTitle(string content)
    {
        var normalized = string.Join(' ', content.Split(
            (char[]?)null,
            StringSplitOptions.RemoveEmptyEntries));
        return normalized.Length <= 48 ? normalized : $"{normalized[..45]}...";
    }

    private static void AddAudit(
        DatabaseState state,
        string eventType,
        string actorId,
        string resourceId,
        string correlationId)
    {
        state.Audit.Add(new AuditEntry(
            Guid.NewGuid().ToString("N"),
            correlationId,
            eventType,
            actorId,
            resourceId,
            DateTimeOffset.UtcNow));
        if (state.Audit.Count > 5000)
        {
            state.Audit.RemoveRange(0, state.Audit.Count - 5000);
        }
    }

    private sealed class DatabaseState
    {
        public List<LocalUser> Users { get; set; } = [];
        public List<Conversation> Conversations { get; set; } = [];
        public List<ChatMessage> Messages { get; set; } = [];
        public List<ContextSummary> ContextSummaries { get; set; } = [];
        public List<OutboxRecord> Outbox { get; set; } = [];
        public List<ConversationFile> ConversationFiles { get; set; } = [];
        public List<UsageEntry> Usage { get; set; } = [];
        public List<AuditEntry> Audit { get; set; } = [];
    }
}
