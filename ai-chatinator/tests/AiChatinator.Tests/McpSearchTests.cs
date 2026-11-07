using System.Net;
using System.Net.Http.Headers;
using System.Security.Claims;
using System.Text.Encodings.Web;
using System.Text.Json;
using AiChatinator.Api.Mcp;
using AiChatinator.Application;
using AiChatinator.Domain;
using AiChatinator.Infrastructure;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using ModelContextProtocol.Client;
using ModelContextProtocol.AspNetCore;
using ModelContextProtocol.Server;

namespace AiChatinator.Tests;

public sealed class McpSearchTests
{
    [Theory]
    [InlineData("http://127.0.0.1:0")]
    [InlineData("http://[::1]:0")]
    public async Task Authenticated_http_MCP_discovers_and_executes_owner_scoped_searches(string listeningUrl)
    {
        var dataPath = Path.Combine(Path.GetTempPath(), $"mcp-search-{Guid.NewGuid():N}.json");
        var builder = WebApplication.CreateBuilder();
        builder.WebHost.UseUrls(listeningUrl);
        builder.Logging.ClearProviders();
        builder.Services.AddSingleton(new LocalDataStore(dataPath));
        builder.Services.AddSingleton<IChatStore>(services => services.GetRequiredService<LocalDataStore>());
        builder.Services.AddSingleton<LocalChatToolCatalog>();
        builder.Services.AddHttpContextAccessor();
        builder.Services.AddScoped<IChatToolCatalog, McpChatToolCatalog>();
        builder.Services.AddAuthentication("Test")
            .AddScheme<AuthenticationSchemeOptions, TestAuthenticationHandler>("Test", _ => { });
        builder.Services.AddAuthorization();
        builder.Services.AddMcpServer()
            .WithHttpTransport(options => options.SessionMode = HttpServerSessionMode.Stateless)
            .WithTools<ConversationSearchTools>();
        await using var app = builder.Build();
        app.UseAuthentication();
        app.UseAuthorization();
        app.Use(async (context, next) =>
        {
            if (context.Request.Path == "/mcp" && context.Request.Host.Host != "localhost")
            {
                context.Response.StatusCode = StatusCodes.Status400BadRequest;
                return;
            }
            await next();
        });
        app.MapMcp("/mcp").RequireAuthorization();
        app.MapGet("/chat/{conversationId}", async (HttpContext context, IChatToolCatalog tools) =>
        {
            var definitions = await tools.GetDefinitionsAsync(context.RequestAborted);
            var result = await tools.ExecuteAsync(context.User.FindFirstValue(ClaimTypes.NameIdentifier)!,
                context.Request.RouteValues["conversationId"]!.ToString()!,
                new ChatToolCall("test", "search_other_conversations", """{"query":"meeting"}"""),
                context.RequestAborted);
            return Results.Ok(new { names = definitions.Select(tool => tool.Name), result });
        }).RequireAuthorization();
        try
        {
            var store = app.Services.GetRequiredService<IChatStore>();
            await store.AddUserAsync(new LocalUser("owner", "owner@example.test", "", 1000, DateTimeOffset.UtcNow),
                CancellationToken.None);
            var current = await store.CreateConversationAsync("owner", CancellationToken.None);
            var earlier = await store.CreateConversationAsync("owner", CancellationToken.None);
            await new ChatService(store, new LocalDemoChatProvider(), new ChatContextOptions()).SendAsync(
                "owner", earlier.Id, "The meeting is Friday", "seed", "seed", CancellationToken.None);
            await app.StartAsync();
            var address = app.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            using var http = new HttpClient { BaseAddress = new Uri(address) };
            http.DefaultRequestHeaders.Host = "localhost";
            http.DefaultRequestHeaders.Accept.ParseAdd("application/json");
            http.DefaultRequestHeaders.Accept.ParseAdd("text/event-stream");
            using var unauthorized = await http.PostAsync("/mcp",
                new StringContent("{}", System.Text.Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.Unauthorized, unauthorized.StatusCode);
            http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", "owner");
            using var response = await http.GetAsync($"/chat/{current.Id}");
            var body = await response.Content.ReadAsStringAsync();
            Assert.True(response.IsSuccessStatusCode, body);
            using var json = JsonDocument.Parse(body);
            Assert.Contains("search_other_conversations", json.RootElement.GetProperty("names").ToString());
            Assert.Contains(earlier.Id, json.RootElement.GetProperty("result").GetString());
            Assert.Contains("Friday", json.RootElement.GetProperty("result").GetString());

            await using var ownerClient = await McpClient.CreateAsync(new HttpClientTransport(
                new HttpClientTransportOptions
                {
                    Endpoint = new Uri(address + "/mcp"),
                    AdditionalHeaders = new Dictionary<string, string>
                    {
                        ["Authorization"] = "Bearer owner",
                        ["Host"] = "localhost",
                        ["X-Conversation-Id"] = current.Id
                    }
                }));
            var discovered = await ownerClient.ListToolsAsync();
            Assert.Contains(discovered, tool => tool.Name == "list_conversation_ids");
            Assert.Contains(discovered, tool => tool.Name == "get_conversation_messages");
            var ids = await ownerClient.CallToolAsync("list_conversation_ids",
                new Dictionary<string, object?> { ["offset"] = 0 });
            Assert.False(ids.IsError == true);
            var messages = await ownerClient.CallToolAsync("get_conversation_messages",
                new Dictionary<string, object?> { ["conversationId"] = earlier.Id });
            Assert.False(messages.IsError == true);

            await using var client = await McpClient.CreateAsync(new HttpClientTransport(
                new HttpClientTransportOptions
                {
                    Endpoint = new Uri(address + "/mcp"),
                    AdditionalHeaders = new Dictionary<string, string>
                    {
                        ["Authorization"] = "Bearer intruder",
                        ["Host"] = "localhost",
                        ["X-Conversation-Id"] = current.Id
                    }
                }));
            var rejected = await client.CallToolAsync("search_other_conversations",
                new Dictionary<string, object?> { ["query"] = "meeting" });
            Assert.True(rejected.IsError);
            var rejectedMessages = await client.CallToolAsync("get_conversation_messages",
                new Dictionary<string, object?> { ["conversationId"] = earlier.Id });
            Assert.True(rejectedMessages.IsError);
            var rejectedIds = await client.CallToolAsync("list_conversation_ids",
                new Dictionary<string, object?> { ["offset"] = 0 });
            Assert.True(rejectedIds.IsError);
        }
        finally
        {
            await app.StopAsync();
            File.Delete(dataPath);
            File.Delete(dataPath + ".tmp");
        }
    }

    private sealed class TestAuthenticationHandler(
        IOptionsMonitor<AuthenticationSchemeOptions> options,
        ILoggerFactory logger,
        UrlEncoder encoder) : AuthenticationHandler<AuthenticationSchemeOptions>(options, logger, encoder)
    {
        protected override Task<AuthenticateResult> HandleAuthenticateAsync()
        {
            var authorization = Request.Headers.Authorization.ToString();
            if (!authorization.StartsWith("Bearer ", StringComparison.Ordinal))
            {
                return Task.FromResult(AuthenticateResult.NoResult());
            }
            var identity = new ClaimsIdentity(
                [new Claim(ClaimTypes.NameIdentifier, authorization["Bearer ".Length..])], Scheme.Name);
            return Task.FromResult(AuthenticateResult.Success(
                new AuthenticationTicket(new ClaimsPrincipal(identity), Scheme.Name)));
        }
    }
}
