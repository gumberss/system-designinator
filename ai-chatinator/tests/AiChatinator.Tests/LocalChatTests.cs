using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure;
using AiChatinator.Llm;
using System.Net;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace AiChatinator.Tests;

public sealed class LocalChatTests : IDisposable
{
    private readonly string _dataPath = Path.Combine(
        Path.GetTempPath(),
        $"ai-chatinator-tests-{Guid.NewGuid():N}.json");

    [Fact]
    public async Task SendAsync_persists_a_turn_and_charges_once_for_retries()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-1", "test@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        Assert.True(await store.AddUserAsync(user, CancellationToken.None));
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var service = new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions());

        var first = await service.SendAsync(
            user.Id,
            conversation.Id,
            "Help me plan a local test",
            "request-1",
            "trace-1",
            CancellationToken.None);
        var retry = await service.SendAsync(
            user.Id,
            conversation.Id,
            "Help me plan a local test",
            "request-1",
            "trace-1",
            CancellationToken.None);

        Assert.Equal(first.UserMessage.Id, retry.UserMessage.Id);
        Assert.Equal(first.AssistantMessage.Id, retry.AssistantMessage.Id);
        Assert.Equal(999, retry.CreditsRemaining);
        Assert.Equal("local-demo", retry.Provider);
        Assert.Equal("echo-v1", retry.Model);
        Assert.Equal(2, (await store.ListMessagesAsync(user.Id, conversation.Id, CancellationToken.None))!.Count);
        var usage = await store.GetUsageHistoryAsync(user.Id, CancellationToken.None);
        Assert.Single(usage);
        Assert.Equal("local-demo", usage[0].Provider);
    }

    [Fact]
    public async Task Completed_turn_survives_restarting_the_local_store()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-2", "persist@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        await new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions()).SendAsync(
            user.Id,
            conversation.Id,
            "Persist this",
            "request-2",
            "trace-2",
            CancellationToken.None);

        var restartedStore = new LocalDataStore(_dataPath);
        var messages = await restartedStore.ListMessagesAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        var reloadedUser = await restartedStore.FindUserByIdAsync(user.Id, CancellationToken.None);

        Assert.Equal(2, messages!.Count);
        Assert.Equal(999, reloadedUser!.Credits);
    }

    [Fact]
    public async Task Context_compaction_saves_a_versioned_summary_without_deleting_messages()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-compact", "compact@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var seedProvider = new CapturingChatProvider();
        var seedService = new ChatService(store, seedProvider, new ChatContextOptions());
        for (var index = 0; index < 3; index++)
        {
            await seedService.SendAsync(
                user.Id,
                conversation.Id,
                index < 2 ? $"old-{index}-{new string('x', 100)}" : $"old-{index}",
                $"seed-{index}",
                $"trace-{index}",
                CancellationToken.None);
        }

        var originalMessages = await store.ListMessagesAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        var compactingProvider = new CapturingChatProvider();
        var compactingService = new ChatService(
            store,
            compactingProvider,
            new ChatContextOptions(CompactAtTokens: 58, KeepRecentMessages: 2));
        await compactingService.SendAsync(
            user.Id,
            conversation.Id,
            "new-question",
            "compact-request",
            "compact-trace",
            CancellationToken.None);

        var savedSummary = await store.GetContextSummaryAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        var allMessages = await store.ListMessagesAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        Assert.NotNull(savedSummary);
        Assert.Equal(1, savedSummary.Version);
        Assert.Equal(originalMessages![3].Id, savedSummary.ThroughMessageId);
        Assert.Equal(originalMessages.Count + 2, allMessages!.Count);
        Assert.Contains(compactingProvider.SummaryInput!, item =>
            item.Content.StartsWith("old-0-", StringComparison.Ordinal));
        Assert.Contains(compactingProvider.LastReplyHistory!, item =>
            item.Role == "system" && item.Content.Contains("earlier conversation messages", StringComparison.Ordinal));
        Assert.DoesNotContain(compactingProvider.LastReplyHistory!, item =>
            item.Content.StartsWith("old-0-", StringComparison.Ordinal));
        Assert.Contains(compactingProvider.LastReplyHistory!, item => item.Content == "old-2");
    }

    [Fact]
    public async Task Stale_context_summary_version_is_rejected_without_removing_history()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-cas", "cas@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var service = new ChatService(store, new CapturingChatProvider(), new ChatContextOptions());
        await service.SendAsync(
            user.Id,
            conversation.Id,
            "first",
            "cas-request",
            "cas-trace",
            CancellationToken.None);
        var messagesBefore = await store.ListMessagesAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        Assert.NotNull(messagesBefore);

        Assert.True(await store.SaveContextSummaryAsync(
            user.Id,
            conversation.Id,
            expectedVersion: 0,
            throughMessageId: messagesBefore[0].Id,
            content: "summary",
            model: "test",
            CancellationToken.None));
        Assert.False(await store.SaveContextSummaryAsync(
            user.Id,
            conversation.Id,
            expectedVersion: 0,
            throughMessageId: messagesBefore[0].Id,
            content: "stale summary",
            model: "test",
            CancellationToken.None));

        var messagesAfter = await store.ListMessagesAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        var summary = await store.GetContextSummaryAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None);
        Assert.Equal(messagesBefore.Count, messagesAfter!.Count);
        Assert.Equal("summary", summary!.Content);
        Assert.Equal(1, summary.Version);
    }

    [Fact]
    public async Task Completed_turn_adds_minimized_versioned_event_to_durable_outbox()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-outbox", "outbox@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var service = new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions());
        await service.SendAsync(
            user.Id,
            conversation.Id,
            "private prompt content",
            "outbox-request",
            "outbox-trace",
            CancellationToken.None);
        await service.SendAsync(
            user.Id,
            conversation.Id,
            "private prompt content",
            "outbox-request",
            "outbox-trace",
            CancellationToken.None);

        var pending = await store.GetPendingEventsAsync(10, CancellationToken.None);
        Assert.Single(pending);
        Assert.Equal("MessageTurnCompleted", pending[0].Event.EventType);
        Assert.Equal(1, pending[0].Event.EventVersion);
        Assert.Equal("outbox-trace", pending[0].Event.CorrelationId);
        Assert.Equal(conversation.Id, pending[0].Event.AggregateId);
        Assert.DoesNotContain("private prompt content", pending[0].Event.DataJson, StringComparison.Ordinal);

        await store.MarkEventDispatchFailedAsync(
            pending[0].Event.EventId,
            "TestFailure",
            CancellationToken.None);
        var retry = Assert.Single(await store.GetPendingEventsAsync(10, CancellationToken.None));
        Assert.Equal(1, retry.Attempts);
        Assert.Equal("TestFailure", retry.LastError);

        await store.MarkEventDispatchedAsync(retry.Event.EventId, CancellationToken.None);
        Assert.Empty(await store.GetPendingEventsAsync(10, CancellationToken.None));
        var restartedStore = new LocalDataStore(_dataPath);
        Assert.Empty(await restartedStore.GetPendingEventsAsync(10, CancellationToken.None));
    }

    [Fact]
    public async Task Text_attachment_is_validated_scoped_to_owner_and_included_as_untrusted_context()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-file", "file@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var fileService = new ConversationFileService(
            store,
            new PdfPigTextExtractor(),
            new ConversationFileOptions(MaximumBytes: 1024));
        var bytes = System.Text.Encoding.UTF8.GetBytes("Project deadline: 2026-12-01");
        var file = await fileService.UploadAsync(
            user.Id,
            conversation.Id,
            @"C:\fakepath\plan.txt",
            "text/plain",
            new MemoryStream(bytes),
            CancellationToken.None);

        Assert.Equal("plan.txt", file.FileName);
        Assert.Equal(bytes.LongLength, file.SizeBytes);
        Assert.Equal("Project deadline: 2026-12-01", file.ExtractedText);
        Assert.Single(await store.ListConversationFilesAsync(user.Id, conversation.Id, CancellationToken.None));
        await Assert.ThrowsAsync<KeyNotFoundException>(() => store.GetConversationFilesAsync(
            "different-user",
            conversation.Id,
            [file.Id],
            CancellationToken.None));

        var provider = new CapturingChatProvider();
        await new ChatService(store, provider, new ChatContextOptions()).SendAsync(
            user.Id,
            conversation.Id,
            "When is the deadline?",
            "file-request",
            "file-trace",
            CancellationToken.None,
            [file.Id]);
        Assert.Contains(
            provider.LastReplyHistory!,
            message => message.Role == "system"
                && message.Content.Contains("Project deadline: 2026-12-01", StringComparison.Ordinal)
                && message.Content.Contains("untrusted user-provided data", StringComparison.Ordinal));
    }

    [Fact]
    public async Task File_upload_rejects_mismatched_type_and_oversized_content()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-file-invalid", "file-invalid@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var service = new ConversationFileService(
            store,
            new PdfPigTextExtractor(),
            new ConversationFileOptions(MaximumBytes: 4));

        await Assert.ThrowsAsync<InvalidDataException>(() => service.UploadAsync(
            user.Id,
            conversation.Id,
            "fake.pdf",
            "application/pdf",
            new MemoryStream(System.Text.Encoding.UTF8.GetBytes("not a PDF")),
            CancellationToken.None));
        await Assert.ThrowsAsync<InvalidDataException>(() => service.UploadAsync(
            user.Id,
            conversation.Id,
            "large.txt",
            "text/plain",
            new MemoryStream(System.Text.Encoding.UTF8.GetBytes("oversized")),
            CancellationToken.None));

        Assert.Empty(await store.ListConversationFilesAsync(user.Id, conversation.Id, CancellationToken.None));
    }

    [Fact]
    public async Task Model_tool_calls_are_limited_to_registered_tools_and_return_bounded_results()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-tool", "tool@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        await new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions()).SendAsync(
            user.Id,
            conversation.Id,
            "The meeting is Friday",
            "tool-seed",
            "tool-seed-trace",
            CancellationToken.None);

        var provider = new ToolCallingProvider();
        var service = new ChatService(
            store,
            provider,
            new ChatContextOptions(),
            new LocalChatToolCatalog(store));
        var result = await service.SendAsync(
            user.Id,
            conversation.Id,
            "When is the meeting?",
            "tool-request",
            "tool-trace",
            CancellationToken.None);

        Assert.Equal("The meeting is Friday.", result.AssistantMessage.Content);
        Assert.Equal(2, provider.RequestCount);
        Assert.Contains(provider.SecondRequest!, message =>
            message.Role == "tool"
            && message.ToolCallId == "tool-call-1"
            && message.Content.Contains("Friday", StringComparison.Ordinal));
        Assert.Contains(provider.FirstToolNames!, name => name == "search_conversation");
    }

    [Fact]
    public async Task Reusing_idempotency_key_for_different_content_is_rejected()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-4", "idempotency@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var service = new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions());
        await service.SendAsync(
            user.Id,
            conversation.Id,
            "First content",
            "reused-key",
            "trace-4",
            CancellationToken.None);

        await Assert.ThrowsAsync<IdempotencyConflictException>(() => service.SendAsync(
            user.Id,
            conversation.Id,
            "Different content",
            "reused-key",
            "trace-5",
            CancellationToken.None));

        Assert.Equal(999, (await store.FindUserByIdAsync(user.Id, CancellationToken.None))!.Credits);
    }

    [Fact]
    public async Task Share_link_exposes_messages_and_can_be_revoked()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-3", "share@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        await new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions()).SendAsync(
            user.Id,
            conversation.Id,
            "A shared message",
            "request-3",
            "trace-3",
            CancellationToken.None);
        Assert.True(await store.SetConversationShareAsync(
            user.Id,
            conversation.Id,
            "hashed-share-token",
            CancellationToken.None));

        var shared = await store.GetSharedConversationAsync("hashed-share-token", CancellationToken.None);
        Assert.NotNull(shared);
        Assert.Equal(2, shared.Messages.Count);
        Assert.True(await store.RevokeConversationShareAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None));
        Assert.Null(await store.GetSharedConversationAsync("hashed-share-token", CancellationToken.None));
    }

    [Fact]
    public async Task Archive_state_is_owner_scoped_and_audited()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("user-5", "archive@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);

        Assert.False(await store.SetConversationArchivedAsync(
            "another-user",
            conversation.Id,
            true,
            CancellationToken.None));
        Assert.True(await store.SetConversationArchivedAsync(
            user.Id,
            conversation.Id,
            true,
            CancellationToken.None));
        Assert.True((await store.ListConversationsAsync(user.Id, CancellationToken.None)).Single().IsArchived);
    }

    [Fact]
    public async Task Local_provider_identifies_demo_mode_and_does_not_call_external_services()
    {
        var provider = new LocalDemoChatProvider();

        var response = await provider.GetReplyAsync(
            [("user", "Hello")],
            CancellationToken.None);

        Assert.Contains("local demo mode", response.Content, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("Hello", response.Content, StringComparison.Ordinal);
        Assert.Equal("local-demo", response.Provider);
    }

    [Fact]
    public async Task Local_provider_streams_deltas_and_persists_the_completed_turn()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("stream-user", "stream@example.com", "hash", 1000, DateTimeOffset.UtcNow);
        Assert.True(await store.AddUserAsync(user, CancellationToken.None));
        var conversation = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var service = new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions());
        var events = new List<ChatStreamEvent>();

        await foreach (var streamEvent in service.StreamAsync(
                           user.Id,
                           conversation.Id,
                           "Stream this response",
                           "stream-request",
                           "stream-correlation",
                           CancellationToken.None))
        {
            events.Add(streamEvent);
        }

        Assert.Contains(events, streamEvent => streamEvent.Type == "delta");
        var completed = Assert.Single(events, streamEvent => streamEvent.Type == "complete");
        Assert.NotNull(completed.Result);
        Assert.Equal(
            completed.Result.AssistantMessage.Content,
            string.Concat(events.Where(streamEvent => streamEvent.Type == "delta")
                .Select(streamEvent => streamEvent.Content)));
        Assert.Equal(2, (await store.ListMessagesAsync(
            user.Id,
            conversation.Id,
            CancellationToken.None))!.Count);
    }

    [Fact]
    public async Task OpenAi_compatible_provider_sends_chat_and_tracks_reported_usage()
    {
        var handler = new StubHttpMessageHandler(async request =>
        {
            Assert.Equal(HttpMethod.Post, request.Method);
            Assert.Equal("/v1/chat/completions", request.RequestUri!.AbsolutePath);
            Assert.Equal("Bearer", request.Headers.Authorization!.Scheme);
            Assert.Equal("test-key", request.Headers.Authorization.Parameter);
            using var requestJson = await JsonDocument.ParseAsync(
                await request.Content!.ReadAsStreamAsync());
            Assert.Equal("none", requestJson.RootElement.GetProperty("reasoning_effort").GetString());
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(
                    """
                    {
                      "model": "test-model",
                      "choices": [{ "message": { "content": "A real adapter reply." } }],
                      "usage": { "prompt_tokens": 23, "completion_tokens": 7 }
                    }
                    """)
            };
        });
        var provider = new OpenAiCompatibleChatProvider(
            new HttpClient(handler),
            Options.Create(new OpenAiCompatibleOptions
            {
                BaseUrl = "https://example.test/v1/",
                ApiKey = "test-key",
                Model = "test-model"
            }));

        var completion = await provider.GetReplyAsync(
            [("user", "Hello")],
            CancellationToken.None);

        Assert.Equal("A real adapter reply.", completion.Content);
        Assert.Equal(23, completion.InputTokens);
        Assert.Equal(7, completion.OutputTokens);
        Assert.Equal("test-model", completion.Model);
    }

    [Fact]
    public async Task OpenAi_compatible_provider_emits_streamed_content_and_final_usage()
    {
        var handler = new StubHttpMessageHandler(async request =>
        {
            Assert.Equal(HttpMethod.Post, request.Method);
            Assert.Equal("/v1/chat/completions", request.RequestUri!.AbsolutePath);
            using var requestJson = await JsonDocument.ParseAsync(
                await request.Content!.ReadAsStreamAsync());
            Assert.True(requestJson.RootElement.GetProperty("stream").GetBoolean());
            Assert.Equal("none", requestJson.RootElement.GetProperty("reasoning_effort").GetString());
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(
                    """
                    data: {"model":"test-model","choices":[{"delta":{"content":"Hello "}}]}

                    data: {"model":"test-model","choices":[{"delta":{"content":"stream"}}]}

                    data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2}}

                    data: [DONE]

                    """)
            };
        });
        var provider = new OpenAiCompatibleChatProvider(
            new HttpClient(handler),
            Options.Create(new OpenAiCompatibleOptions
            {
                BaseUrl = "https://example.test/v1/",
                ApiKey = "test-key",
                Model = "test-model"
            }));
        var events = new List<ChatProviderStreamEvent>();

        await foreach (var streamEvent in provider.StreamReplyWithToolsAsync(
                           [new ChatPromptMessage("user", "Hello")],
                           [],
                           CancellationToken.None))
        {
            events.Add(streamEvent);
        }

        Assert.Equal(["Hello ", "stream"], events
            .Where(streamEvent => streamEvent.ContentDelta is not null)
            .Select(streamEvent => streamEvent.ContentDelta!));
        var completion = Assert.Single(events, streamEvent => streamEvent.Completion is not null)
            .Completion!;
        Assert.Equal("Hello stream", completion.Content);
        Assert.Equal(12, completion.InputTokens);
        Assert.Equal(2, completion.OutputTokens);
    }

    [Fact]
    public void OpenAi_compatible_provider_rejects_non_tls_remote_endpoints()
    {
        var error = Assert.Throws<InvalidOperationException>(
            () => new OpenAiCompatibleChatProvider(
                new HttpClient(),
                Options.Create(new OpenAiCompatibleOptions
                {
                    BaseUrl = "http://example.test/v1/",
                    ApiKey = "test-key",
                    Model = "test-model"
                })));

        Assert.Contains("must use HTTPS", error.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Provider_errors_are_logged_with_diagnostics_but_without_the_api_key(bool streaming)
    {
        var logger = new CapturingProviderLogger();
        var provider = new OpenAiCompatibleChatProvider(
            new HttpClient(new StubHttpMessageHandler(_ => new HttpResponseMessage(HttpStatusCode.BadRequest)
            {
                Content = new StringContent(
                    """{"error":{"message":"Unsupported parameter; secret-test-key","type":"invalid_request_error","code":"unsupported_parameter","param":"stream_options"}}""")
            })),
            Options.Create(new OpenAiCompatibleOptions
            {
                ApiKey = "secret-test-key",
                Model = "test-model"
            }),
            logger);

        var exception = await Assert.ThrowsAsync<HttpRequestException>(async () =>
        {
            if (streaming)
            {
                await foreach (var update in provider.StreamReplyWithToolsAsync(
                                   [new ChatPromptMessage("user", "Private prompt")], [], CancellationToken.None))
                {
                }
            }
            else
            {
                await provider.GetReplyAsync([("user", "Private prompt")], CancellationToken.None);
            }
        });

        Assert.Equal(HttpStatusCode.BadRequest, exception.StatusCode);
        Assert.Contains("unsupported_parameter", logger.Message);
        Assert.Contains("stream_options", logger.Message);
        Assert.Contains("[REDACTED]", logger.Message);
        Assert.DoesNotContain("secret-test-key", logger.Message);
        Assert.DoesNotContain("Private prompt", logger.Message);
    }

    private sealed class CapturingProviderLogger : ILogger<OpenAiCompatibleChatProvider>
    {
        public string Message { get; private set; } = string.Empty;
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel logLevel) => true;
        public void Log<TState>(
            LogLevel logLevel,
            EventId eventId,
            TState state,
            Exception? exception,
            Func<TState, Exception?, string> formatter) => Message = formatter(state, exception);
    }

    [Fact]
    public async Task Cross_conversation_search_is_owner_scoped_excludes_current_and_returns_bounded_sources()
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("cross-user", "cross@example.test", "hash", 1000, DateTimeOffset.UtcNow);
        var otherUser = new LocalUser("private-user", "private@example.test", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        await store.AddUserAsync(otherUser, CancellationToken.None);
        var current = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var archived = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var privateConversation = await store.CreateConversationAsync(otherUser.Id, CancellationToken.None);
        var service = new ChatService(store, new CapturingChatProvider(), new ChatContextOptions());
        foreach (var conversation in new[] { current, archived, privateConversation })
        {
            var owner = conversation.Id == privateConversation.Id ? otherUser.Id : user.Id;
            for (var i = 0; i < 6; i++)
            {
                await service.SendAsync(owner, conversation.Id,
                    new string('x', 600) + " CROSS-NEEDLE " + i,
                    $"{conversation.Id}-{i}", "cross-test", CancellationToken.None);
            }
        }
        await store.SetConversationArchivedAsync(user.Id, archived.Id, true, CancellationToken.None);
        var catalog = new LocalChatToolCatalog(store);
        Assert.Contains(catalog.Definitions, tool => tool.Name == "search_other_conversations");
        var call = new ChatToolCall("cross-call", "search_other_conversations", """{"query":"cross-needle"}""");
        var json = await catalog.ExecuteAsync(user.Id, current.Id, call, CancellationToken.None);
        var matches = JsonSerializer.Deserialize<ConversationMessageMatch[]>(json)!;
        Assert.Equal(5, matches.Length);
        Assert.All(matches, match =>
        {
            Assert.Equal(archived.Id, match.ConversationId);
            Assert.NotEmpty(match.MessageId);
            Assert.NotEmpty(match.ConversationTitle);
            Assert.True(match.Excerpt.Length <= 300);
            Assert.Contains("CROSS-NEEDLE", match.Excerpt);
        });
        Assert.Equal(matches.OrderByDescending(match => match.CreatedAt), matches);
        Assert.Equal("[]", await catalog.ExecuteAsync(user.Id, current.Id,
            call with { ArgumentsJson = """{"query":"no-such-phrase"}""" }, CancellationToken.None));
        await Assert.ThrowsAsync<KeyNotFoundException>(() =>
            catalog.ExecuteAsync(user.Id, privateConversation.Id, call, CancellationToken.None));
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("{\"query\":\"\"}")]
    [InlineData("{\"query\":123}")]
    [InlineData("{\"query\":\"valid\",\"userId\":\"someone-else\"}")]
    [InlineData("not-json")]
    public async Task Cross_conversation_search_rejects_invalid_arguments(string arguments)
    {
        var catalog = new LocalChatToolCatalog(new LocalDataStore(_dataPath));
        await Assert.ThrowsAsync<InvalidDataException>(() => catalog.ExecuteAsync(
            "user", "conversation", new ChatToolCall("id", "search_other_conversations", arguments),
            CancellationToken.None));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Cross_conversation_search_is_available_in_model_tool_workflows(bool streaming)
    {
        var store = new LocalDataStore(_dataPath);
        var user = new LocalUser("workflow-user", "workflow@example.test", "hash", 1000, DateTimeOffset.UtcNow);
        await store.AddUserAsync(user, CancellationToken.None);
        var earlier = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        var current = await store.CreateConversationAsync(user.Id, CancellationToken.None);
        await new ChatService(store, new CapturingChatProvider(), new ChatContextOptions()).SendAsync(
            user.Id, earlier.Id, "The meeting is Friday.", "seed", "seed", CancellationToken.None);
        var provider = new ToolCallingProvider("search_other_conversations");
        var service = new ChatService(store, provider, new ChatContextOptions(), new LocalChatToolCatalog(store));
        if (streaming)
        {
            var events = new List<ChatStreamEvent>();
            await foreach (var update in service.StreamAsync(
                               user.Id, current.Id, "Find the meeting in another chat",
                               "workflow", "workflow", CancellationToken.None))
            {
                events.Add(update);
            }
            Assert.Single(events, update => update.Type == "complete");
        }
        else
        {
            await service.SendAsync(user.Id, current.Id, "Find the meeting in another chat",
                "workflow", "workflow", CancellationToken.None);
        }
        Assert.Contains("search_other_conversations", provider.FirstToolNames!);
        Assert.Contains(provider.SecondRequest!, message =>
            message.Role == "tool"
            && message.Content.Contains(earlier.Id, StringComparison.Ordinal)
            && message.Content.Contains("Friday", StringComparison.Ordinal));
    }

    public void Dispose()
    {
        if (File.Exists(_dataPath))
        {
            File.Delete(_dataPath);
        }

        if (File.Exists($"{_dataPath}.tmp"))
        {
            File.Delete($"{_dataPath}.tmp");
        }
    }

    private sealed class StubHttpMessageHandler(
        Func<HttpRequestMessage, Task<HttpResponseMessage>> responseFactory) : HttpMessageHandler
    {
        public StubHttpMessageHandler(Func<HttpRequestMessage, HttpResponseMessage> responseFactory)
            : this(request => Task.FromResult(responseFactory(request)))
        {
        }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken) => responseFactory(request);
    }

    private sealed class CapturingChatProvider : IChatProvider
    {
        public IReadOnlyList<(string Role, string Content)>? SummaryInput { get; private set; }
        public IReadOnlyList<(string Role, string Content)>? LastReplyHistory { get; private set; }

        public Task<ChatCompletion> GetReplyAsync(
            IReadOnlyList<(string Role, string Content)> history,
            CancellationToken cancellationToken)
        {
            LastReplyHistory = history.ToArray();
            return Task.FromResult(new ChatCompletion("reply", "test", "test-v1", 1, 1));
        }

        public Task<ChatCompletion> SummarizeAsync(
            IReadOnlyList<(string Role, string Content)> history,
            CancellationToken cancellationToken)
        {
            SummaryInput = history.ToArray();
            return Task.FromResult(new ChatCompletion("summary", "test", "test-v1", 1, 1));
        }
    }

    private sealed class ToolCallingProvider(string toolName = "search_conversation") : IStreamingChatProvider
    {
        public int RequestCount { get; private set; }
        public IReadOnlyList<ChatPromptMessage>? SecondRequest { get; private set; }
        public IReadOnlyList<string>? FirstToolNames { get; private set; }

        public Task<ChatCompletion> GetReplyAsync(
            IReadOnlyList<(string Role, string Content)> history,
            CancellationToken cancellationToken) =>
            throw new NotSupportedException("The test expects the tool-capable provider interface.");

        public Task<ChatCompletion> GetReplyWithToolsAsync(
            IReadOnlyList<ChatPromptMessage> history,
            IReadOnlyList<ChatToolDefinition> tools,
            CancellationToken cancellationToken)
        {
            RequestCount++;
            if (RequestCount == 1)
            {
                FirstToolNames = tools.Select(tool => tool.Name).ToArray();
                return Task.FromResult(new ChatCompletion(
                    string.Empty,
                    "test",
                    "tool-model",
                    3,
                    1,
                    [new ChatToolCall(
                        "tool-call-1",
                        toolName,
                        """{"query":"meeting"}""")]));
            }

            SecondRequest = history.ToArray();
            return Task.FromResult(new ChatCompletion(
                "The meeting is Friday.",
                "test",
                "tool-model",
                4,
                2));
        }

        public async IAsyncEnumerable<ChatProviderStreamEvent> StreamReplyWithToolsAsync(
            IReadOnlyList<ChatPromptMessage> history,
            IReadOnlyList<ChatToolDefinition> tools,
            [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken)
        {
            var completion = await GetReplyWithToolsAsync(history, tools, cancellationToken);
            if (completion.Content.Length > 0)
            {
                yield return new ChatProviderStreamEvent(ContentDelta: completion.Content);
            }
            yield return new ChatProviderStreamEvent(Completion: completion);
        }
    }
}
