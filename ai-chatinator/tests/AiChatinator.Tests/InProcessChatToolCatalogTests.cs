using System.Text.Json;
using AiChatinator.Api.Mcp;
using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure;

namespace AiChatinator.Tests;

public sealed class InProcessChatToolCatalogTests
{
    [Fact]
    public async Task Catalog_exposes_the_mcp_tools_and_scopes_execution_to_the_caller()
    {
        var path = Path.Combine(Path.GetTempPath(), $"inprocess-{Guid.NewGuid():N}.json");
        try
        {
            var store = new LocalDataStore(path);
            await store.AddUserAsync(new LocalUser("owner", "owner@example.test", "", 1000, DateTimeOffset.UtcNow),
                CancellationToken.None);
            var current = await store.CreateConversationAsync("owner", CancellationToken.None);
            var older = await store.CreateConversationAsync("owner", CancellationToken.None);
            var foreign = await store.CreateConversationAsync("other", CancellationToken.None);
            await new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions()).SendAsync(
                "owner", older.Id, "My number is 424242", "seed", "seed", CancellationToken.None);
            var catalog = new InProcessChatToolCatalog(new ConversationTools(new LocalChatToolCatalog(store), store));

            var definitions = await catalog.GetDefinitionsAsync(CancellationToken.None);
            Assert.Equal(
                ["search_conversation", "search_other_conversations", "list_conversation_ids", "get_conversation_messages"],
                definitions.Select(d => d.Name));
            Assert.All(definitions, d => JsonDocument.Parse(d.ParametersJson).Dispose());

            using var list = JsonDocument.Parse(await catalog.ExecuteAsync("owner", current.Id,
                new ChatToolCall("1", "list_conversation_ids", "{}"), CancellationToken.None));
            var ids = list.RootElement.GetProperty("conversationIds").EnumerateArray().Select(e => e.GetString()).ToList();
            Assert.Contains(older.Id, ids);
            Assert.DoesNotContain(foreign.Id, ids);

            var messages = await catalog.ExecuteAsync("owner", current.Id,
                new ChatToolCall("2", "get_conversation_messages", $$"""{"conversationId":"{{older.Id}}"}"""),
                CancellationToken.None);
            Assert.Contains("424242", messages);

            var search = await catalog.ExecuteAsync("owner", current.Id,
                new ChatToolCall("3", "search_other_conversations", """{"query":"424242"}"""), CancellationToken.None);
            Assert.Contains("424242", search);

            await Assert.ThrowsAnyAsync<Exception>(() => catalog.ExecuteAsync("owner", current.Id,
                new ChatToolCall("4", "get_conversation_messages", $$"""{"conversationId":"{{foreign.Id}}"}"""),
                CancellationToken.None));
            await Assert.ThrowsAsync<InvalidDataException>(() => catalog.ExecuteAsync("owner", current.Id,
                new ChatToolCall("5", "delete_everything", "{}"), CancellationToken.None));
            await Assert.ThrowsAsync<InvalidDataException>(() => catalog.ExecuteAsync("owner", current.Id,
                new ChatToolCall("6", "list_conversation_ids", "[1]"), CancellationToken.None));
        }
        finally { File.Delete(path); File.Delete(path + ".tmp"); }
    }
}
