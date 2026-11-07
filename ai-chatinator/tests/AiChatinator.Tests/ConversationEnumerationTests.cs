using System.Security.Claims;
using System.Text.Json;
using AiChatinator.Api.Mcp;
using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure;
using Microsoft.AspNetCore.Http;

namespace AiChatinator.Tests;

public sealed class ConversationEnumerationTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Investigation_can_continue_past_four_rounds_and_reports_budget_exhaustion(bool streaming)
    {
        var path = Path.Combine(Path.GetTempPath(), $"workflow-{Guid.NewGuid():N}.json");
        try
        {
            var store = new LocalDataStore(path);
            await store.AddUserAsync(new LocalUser("owner", "owner@example.test", "", 1000, DateTimeOffset.UtcNow),
                CancellationToken.None);
            var current = await store.CreateConversationAsync("owner", CancellationToken.None);
            var catalog = new InvestigationCatalog();
            var service = new ChatService(store, new InvestigationProvider(), new ChatContextOptions(), catalog);
            async Task Execute(ChatService chat, string key)
            {
                if (streaming)
                {
                    await foreach (var item in chat.StreamAsync(
                        "owner", current.Id, "Find the number in another chat", key, key, CancellationToken.None)) { }
                }
                else
                {
                    await chat.SendAsync("owner", current.Id, "Find the number in another chat",
                        key, key, CancellationToken.None);
                }
            }
            await Execute(service, "success");
            Assert.Equal(5, catalog.Calls.Count);
            Assert.Equal("list_conversation_ids", catalog.Calls[0]);
            Assert.All(catalog.Calls.Skip(1), name => Assert.Equal("get_conversation_messages", name));
            var limited = new ChatService(store, new InvestigationProvider(), new ChatContextOptions(),
                new InvestigationCatalog(), new ChatToolWorkflowOptions(MaximumCalls: 2));
            var error = await Assert.ThrowsAsync<ToolExecutionLimitException>(() => Execute(limited, "limited"));
            Assert.Contains("Investigation incomplete", error.Message);
            var smallResultBudget = new ChatService(store, new InvestigationProvider(), new ChatContextOptions(),
                new InvestigationCatalog(), new ChatToolWorkflowOptions(MaximumResultCharacters: 10));
            var resultError = await Assert.ThrowsAsync<ToolExecutionLimitException>(() =>
                Execute(smallResultBudget, "oversized"));
            Assert.Contains("No result was silently truncated", resultError.Message);
            Assert.Equal(2, (await store.ListMessagesAsync("owner", current.Id, CancellationToken.None))!.Count);
        }
        finally { File.Delete(path); File.Delete(path + ".tmp"); }
    }

    private sealed class InvestigationCatalog : IChatToolCatalog
    {
        public List<string> Calls { get; } = [];
        public IReadOnlyList<ChatToolDefinition> Definitions { get; } =
        [
            new("list_conversation_ids", "List IDs", """{"type":"object"}"""),
            new("get_conversation_messages", "Read messages", """{"type":"object"}""")
        ];
        public Task<string> ExecuteAsync(string userId, string conversationId, ChatToolCall call,
            CancellationToken cancellationToken)
        {
            Calls.Add(call.Name);
            return Task.FromResult(Calls.Count == 1
                ? """{"conversationIds":["a","b","c","d"],"nextOffset":null}"""
                : Calls.Count == 5
                    ? """{"messages":[{"role":"user","content":"My number is 12345"}],"nextMessageOffset":null}"""
                    : """{"messages":[],"nextMessageOffset":null}""");
        }
    }

    private sealed class InvestigationProvider : IStreamingChatProvider
    {
        private int _round;
        public Task<ChatCompletion> GetReplyAsync(IReadOnlyList<(string Role, string Content)> history,
            CancellationToken cancellationToken) => throw new NotSupportedException();
        public Task<ChatCompletion> GetReplyWithToolsAsync(IReadOnlyList<ChatPromptMessage> history,
            IReadOnlyList<ChatToolDefinition> tools, CancellationToken cancellationToken)
        {
            _round++;
            return Task.FromResult(_round <= 5
                ? new ChatCompletion("", "test", "test", 1, 1,
                    [new ChatToolCall($"call-{_round}", _round == 1
                        ? "list_conversation_ids" : "get_conversation_messages",
                        _round == 1 ? "{}" : $"{{\"conversationId\":\"{(char)('a' + _round - 2)}\"}}")])
                : new ChatCompletion("Your number is 12345, in conversation d.", "test", "test", 1, 1));
        }
        public async IAsyncEnumerable<ChatProviderStreamEvent> StreamReplyWithToolsAsync(
            IReadOnlyList<ChatPromptMessage> history, IReadOnlyList<ChatToolDefinition> tools,
            [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken)
        {
            var completion = await GetReplyWithToolsAsync(history, tools, cancellationToken);
            if (completion.Content.Length > 0) yield return new(ContentDelta: completion.Content);
            yield return new(Completion: completion);
        }
    }

    [Fact]
    public async Task Enumeration_pages_include_all_owned_ids_and_reassemble_full_messages()
    {
        var path = Path.Combine(Path.GetTempPath(), $"enumeration-{Guid.NewGuid():N}.json");
        try
        {
            var store = new LocalDataStore(path);
            await store.AddUserAsync(new LocalUser("owner", "owner@example.test", "", 1000, DateTimeOffset.UtcNow),
                CancellationToken.None);
            var owned = new List<ConversationSummary>();
            for (var i = 0; i < 23; i++)
                owned.Add(await store.CreateConversationAsync("owner", CancellationToken.None));
            var other = await store.CreateConversationAsync("other", CancellationToken.None);
            await store.SetConversationArchivedAsync("owner", owned[1].Id, true, CancellationToken.None);
            var content = new string('x', 5_000) + " NUMBER 987654321";
            var turn = await new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions()).SendAsync(
                "owner", owned[1].Id, content, "seed", "seed", CancellationToken.None);
            var context = new DefaultHttpContext
            {
                User = new ClaimsPrincipal(new ClaimsIdentity(
                    [new Claim(ClaimTypes.NameIdentifier, "owner")], "test"))
            };
            context.Request.Headers["X-Conversation-Id"] = owned[0].Id;
            var tools = new ConversationSearchTools(
                new HttpContextAccessor { HttpContext = context }, new LocalChatToolCatalog(store), store);
            var ids = new List<string>();
            var offset = 0;
            while (true)
            {
                using var page = JsonDocument.Parse(await tools.ListConversationIds(CancellationToken.None, offset));
                ids.AddRange(page.RootElement.GetProperty("conversationIds").EnumerateArray()
                    .Select(id => id.GetString()!));
                var next = page.RootElement.GetProperty("nextOffset");
                if (next.ValueKind == JsonValueKind.Null) break;
                offset = next.GetInt32();
            }
            Assert.Equal(23, ids.Count);
            Assert.Equal(23, ids.Distinct().Count());
            Assert.Contains(owned[1].Id, ids);
            Assert.DoesNotContain(other.Id, ids);
            var reconstructed = new Dictionary<string, string>();
            var messageOffset = 0;
            var contentOffset = 0;
            while (true)
            {
                using var page = JsonDocument.Parse(await tools.GetConversationMessages(
                    owned[1].Id, CancellationToken.None, messageOffset, contentOffset));
                foreach (var fragment in page.RootElement.GetProperty("messages").EnumerateArray())
                {
                    var id = fragment.GetProperty("messageId").GetString()!;
                    reconstructed.TryGetValue(id, out var previous);
                    reconstructed[id] = previous + fragment.GetProperty("content").GetString();
                }
                var next = page.RootElement.GetProperty("nextMessageOffset");
                if (next.ValueKind == JsonValueKind.Null) break;
                messageOffset = next.GetInt32();
                contentOffset = page.RootElement.GetProperty("nextContentOffset").GetInt32();
            }
            Assert.Equal(content, reconstructed[turn.UserMessage.Id]);
            Assert.Equal(turn.AssistantMessage.Content, reconstructed[turn.AssistantMessage.Id]);
            using var empty = JsonDocument.Parse(await tools.GetConversationMessages(
                owned[0].Id, CancellationToken.None));
            Assert.Empty(empty.RootElement.GetProperty("messages").EnumerateArray());
            Assert.Equal(JsonValueKind.Null, empty.RootElement.GetProperty("nextMessageOffset").ValueKind);
            await Assert.ThrowsAsync<KeyNotFoundException>(() =>
                tools.GetConversationMessages(other.Id, CancellationToken.None));
            await Assert.ThrowsAsync<InvalidDataException>(() =>
                tools.GetConversationMessages(owned[1].Id, CancellationToken.None, 99));
            await Assert.ThrowsAsync<InvalidDataException>(() =>
                tools.ListConversationIds(CancellationToken.None, -1));
            context.User = new ClaimsPrincipal(new ClaimsIdentity());
            await Assert.ThrowsAsync<UnauthorizedAccessException>(() =>
                tools.ListConversationIds(CancellationToken.None));
        }
        finally
        {
            File.Delete(path);
            File.Delete(path + ".tmp");
        }
    }
}
