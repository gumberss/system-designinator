using System.ComponentModel.DataAnnotations;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.RateLimiting;
using AiChatinator.Application;
using AiChatinator.Api.Outbox;
using AiChatinator.Domain;
using AiChatinator.Infrastructure;
using AiChatinator.Llm;
using AiChatinator.Api.Mcp;
using AiChatinator.Api.Worker;
using AiChatinator.Infrastructure.DynamoDb;
using AiChatinator.Infrastructure.Messaging;
using Amazon.DynamoDBv2;
using Amazon.Lambda.AspNetCoreServer.Hosting;
using Amazon.SimpleNotificationService;
using Amazon.SQS;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.RateLimiting;
using ModelContextProtocol.AspNetCore;
using ModelContextProtocol.Server;

DotNetEnv.Env.Load();
var builder = WebApplication.CreateBuilder(args);
builder.Services.AddAWSLambdaHosting(LambdaEventSource.HttpApi);
if (Environment.GetEnvironmentVariable("AI_PROVIDER") is { } providerSetting)
{
    builder.Configuration["Ai:Provider"] = providerSetting;
}

if (Environment.GetEnvironmentVariable("OPENAI_API_KEY") is { } apiKey)
{
    builder.Configuration["Ai:OpenAiCompatible:ApiKey"] = apiKey;
}

if (Environment.GetEnvironmentVariable("OPENAI_MODEL") is { } modelSetting)
{
    builder.Configuration["Ai:OpenAiCompatible:Model"] = modelSetting;
}
var apiKeySecretArn = builder.Configuration["Ai:OpenAiCompatible:ApiKeySecretArn"];
if (!string.IsNullOrWhiteSpace(apiKeySecretArn)
    && builder.Configuration["Ai:Provider"] == "OpenAiCompatible"
    && string.IsNullOrWhiteSpace(builder.Configuration["Ai:OpenAiCompatible:ApiKey"]))
{
    using var secrets = new Amazon.SecretsManager.AmazonSecretsManagerClient();
    var secret = await secrets.GetSecretValueAsync(
        new Amazon.SecretsManager.Model.GetSecretValueRequest { SecretId = apiKeySecretArn });
    builder.Configuration["Ai:OpenAiCompatible:ApiKey"] = secret.SecretString?.Trim();
}

var requestsPerMinute = builder.Configuration.GetValue("RateLimiting:RequestsPerMinute", 10);
if (requestsPerMinute < 1)
{
    throw new InvalidOperationException("RateLimiting:RequestsPerMinute must be greater than zero.");
}

var contextOptions = new ChatContextOptions(
    builder.Configuration.GetValue("Ai:Context:CompactAtTokens", 6144),
    builder.Configuration.GetValue("Ai:Context:KeepRecentMessages", 12));
if (contextOptions.CompactAtTokens < 1 || contextOptions.KeepRecentMessages < 2)
{
    throw new InvalidOperationException(
        "Ai:Context:CompactAtTokens must be positive and Ai:Context:KeepRecentMessages must be at least 2.");
}

var fileOptions = new ConversationFileOptions(
    builder.Configuration.GetValue("Files:MaximumBytes", 10 * 1024 * 1024));
if (fileOptions.MaximumBytes is < 1 or > 50 * 1024 * 1024)
{
    throw new InvalidOperationException("Files:MaximumBytes must be between 1 byte and 50 MiB.");
}

var authMode = builder.Configuration["Auth:Mode"] ?? "Local";
if (authMode is not ("Local" or "CognitoJwt"))
{
    throw new InvalidOperationException("Auth:Mode must be Local or CognitoJwt.");
}

var cognitoAuthority = builder.Configuration["Auth:Cognito:Authority"];
var cognitoClientId = builder.Configuration["Auth:Cognito:ClientId"];
if (authMode == "CognitoJwt"
    && (string.IsNullOrWhiteSpace(cognitoAuthority)
        || !Uri.TryCreate(cognitoAuthority, UriKind.Absolute, out var authorityUri)
        || authorityUri.Scheme != Uri.UriSchemeHttps
        || string.IsNullOrWhiteSpace(cognitoClientId)))
{
    throw new InvalidOperationException(
        "CognitoJwt auth requires an HTTPS Auth:Cognito:Authority and Auth:Cognito:ClientId.");
}

var dataPath = builder.Configuration["LocalData:Path"]
    ?? Path.Combine(builder.Environment.ContentRootPath, "App_Data", "chat-data.json");

builder.Services.AddProblemDetails();
var persistenceProvider = builder.Configuration["Persistence:Provider"] ?? "Local";
if (persistenceProvider == "Local")
{
    builder.Services.AddSingleton(new LocalDataStore(dataPath));
    builder.Services.AddSingleton<IChatStore>(services => services.GetRequiredService<LocalDataStore>());
    builder.Services.AddSingleton<IConversationFileStore>(services => services.GetRequiredService<LocalDataStore>());
    builder.Services.AddSingleton<IOutboxStore>(services => services.GetRequiredService<LocalDataStore>());
}
else if (persistenceProvider == "DynamoDb")
{
    var dynamoSection = builder.Configuration.GetSection("Persistence:DynamoDb");
    var tableOptions = new DynamoDbTableOptions(
        dynamoSection["ConversationsTable"] ?? "ai-chatinator-conversations",
        dynamoSection["UsageTable"] ?? "ai-chatinator-usage",
        dynamoSection["AuditTable"] ?? "ai-chatinator-audit",
        dynamoSection.GetValue("AuditRetentionDays", 365));
    tableOptions.Validate();
    var dynamoServiceUrl = dynamoSection["ServiceUrl"];
    if (!string.IsNullOrWhiteSpace(dynamoServiceUrl)
        && !Uri.TryCreate(dynamoServiceUrl, UriKind.Absolute, out _))
    {
        throw new InvalidOperationException("Persistence:DynamoDb:ServiceUrl must be an absolute URL.");
    }

    builder.Services.AddSingleton<IAmazonDynamoDB>(_ => string.IsNullOrWhiteSpace(dynamoServiceUrl)
        ? new AmazonDynamoDBClient()
        : new AmazonDynamoDBClient(new AmazonDynamoDBConfig { ServiceURL = dynamoServiceUrl }));
    builder.Services.AddSingleton(tableOptions);
    builder.Services.AddSingleton<DynamoDbChatStore>();
    builder.Services.AddSingleton<IChatStore>(services => services.GetRequiredService<DynamoDbChatStore>());
    builder.Services.AddSingleton<IConversationFileStore>(services => services.GetRequiredService<DynamoDbChatStore>());
    builder.Services.AddSingleton<IOutboxStore>(services => services.GetRequiredService<DynamoDbChatStore>());
    builder.Services.AddSingleton<IUsageRollupStore>(services => services.GetRequiredService<DynamoDbChatStore>());
}
else
{
    throw new InvalidOperationException("Persistence:Provider must be Local or DynamoDb.");
}
var eventTransport = builder.Configuration["DomainEvents:Transport"] ?? "Local";
if (eventTransport == "Local")
{
    builder.Services.AddSingleton<IDomainEventPublisher, LocalDomainEventPublisher>();
}
else if (eventTransport == "Sns")
{
    var topicArn = builder.Configuration["DomainEvents:TopicArn"];
    if (string.IsNullOrWhiteSpace(topicArn))
    {
        throw new InvalidOperationException(
            "DomainEvents:TopicArn is required when DomainEvents:Transport is Sns.");
    }

    builder.Services.AddSingleton<IAmazonSimpleNotificationService, AmazonSimpleNotificationServiceClient>();
    builder.Services.AddSingleton<IDomainEventPublisher>(services =>
        new SnsDomainEventPublisher(
            services.GetRequiredService<IAmazonSimpleNotificationService>(),
            topicArn));
}
else
{
    throw new InvalidOperationException("DomainEvents:Transport must be Local or Sns.");
}
builder.Services.AddScoped<OutboxRelay>();
if (builder.Configuration.GetValue("Outbox:DispatcherEnabled", true))
{
    builder.Services.AddHostedService<OutboxDispatcher>();
}

if (builder.Configuration.GetValue("Worker:Enabled", false))
{
    if (persistenceProvider != "DynamoDb")
    {
        throw new InvalidOperationException(
            "Worker:Enabled requires Persistence:Provider DynamoDb (the usage rollup is stored in DynamoDB).");
    }

    var workerOptions = new SqsWorkerOptions(
        builder.Configuration["Worker:QueueUrl"] ?? "",
        builder.Configuration.GetValue("Worker:MaxMessages", 10),
        builder.Configuration.GetValue("Worker:WaitTimeSeconds", 20));
    workerOptions.Validate();
    builder.Services.AddSingleton(workerOptions);
    builder.Services.AddSingleton<IAmazonSQS, AmazonSQSClient>();
    builder.Services.AddSingleton<IDomainEventHandler, UsageRollupHandler>();
    builder.Services.AddScoped<DomainEventMessageProcessor>();
    builder.Services.AddHostedService<SqsDomainEventWorker>();
}
builder.Services.AddSingleton<IFileTextExtractor, PdfPigTextExtractor>();
builder.Services.AddSingleton(fileOptions);
builder.Services.AddSingleton<ConversationFileService>();
builder.Services.AddHttpContextAccessor();
builder.Services.AddSingleton<LocalChatToolCatalog>();
builder.Services.AddScoped<ConversationTools>();
// Lambda has no listening socket, so the API cannot reach its own /mcp endpoint there.
var toolTransport = builder.Configuration["Mcp:ToolTransport"]
    ?? (Environment.GetEnvironmentVariable("AWS_LAMBDA_FUNCTION_NAME") is null ? "Http" : "InProcess");
if (toolTransport == "Http")
{
    builder.Services.AddScoped<IChatToolCatalog, McpChatToolCatalog>();
}
else if (toolTransport == "InProcess")
{
    builder.Services.AddScoped<IChatToolCatalog, InProcessChatToolCatalog>();
}
else
{
    throw new InvalidOperationException("Mcp:ToolTransport must be Http or InProcess.");
}
builder.Services.AddSingleton<IPasswordHasher<LocalUser>, PasswordHasher<LocalUser>>();
var aiProvider = builder.Configuration["Ai:Provider"] ?? "LocalDemo";
if (string.Equals(aiProvider, "LocalDemo", StringComparison.OrdinalIgnoreCase))
{
    builder.Services.AddSingleton<IChatProvider, LocalDemoChatProvider>();
}
else if (string.Equals(aiProvider, "OpenAiCompatible", StringComparison.OrdinalIgnoreCase))
{
    builder.Services.Configure<OpenAiCompatibleOptions>(
        builder.Configuration.GetSection("Ai:OpenAiCompatible"));
    builder.Services.AddHttpClient<OpenAiCompatibleChatProvider>();
    builder.Services.AddTransient<IChatProvider>(services =>
        services.GetRequiredService<OpenAiCompatibleChatProvider>());
}
else
{
    throw new InvalidOperationException(
        "Ai:Provider must be LocalDemo or OpenAiCompatible. No external provider is enabled by default.");
}
builder.Services.AddSingleton(contextOptions);
var toolOptions = new ChatToolWorkflowOptions(
    builder.Configuration.GetValue("Ai:Tools:MaximumRounds", 16),
    builder.Configuration.GetValue("Ai:Tools:MaximumCalls", 30),
    builder.Configuration.GetValue("Ai:Tools:MaximumResultCharacters", 24_000));
if (toolOptions.MaximumRounds is < 2 or > 100
    || toolOptions.MaximumCalls is < 1 or > 300
    || toolOptions.MaximumResultCharacters is < 1 or > 100_000)
{
    throw new InvalidOperationException("Ai:Tools limits must be valid: rounds 2-100, calls 1-300, result characters 1-100000.");
}
builder.Services.AddSingleton(toolOptions);
builder.Services.AddScoped<ChatService>();
if (authMode == "Local")
{
    builder.Services
        .AddAuthentication(CookieAuthenticationDefaults.AuthenticationScheme)
        .AddCookie(options =>
        {
            options.Cookie.Name = "ai-chatinator.session";
            options.Cookie.HttpOnly = true;
            options.Cookie.SameSite = SameSiteMode.Strict;
            options.Cookie.SecurePolicy = CookieSecurePolicy.SameAsRequest;
            options.SlidingExpiration = true;
            options.ExpireTimeSpan = TimeSpan.FromHours(12);
            options.Events.OnRedirectToLogin = context =>
            {
                context.Response.StatusCode = StatusCodes.Status401Unauthorized;
                return Task.CompletedTask;
            };
            options.Events.OnRedirectToAccessDenied = context =>
            {
                context.Response.StatusCode = StatusCodes.Status403Forbidden;
                return Task.CompletedTask;
            };
        });
}
else
{
    builder.Services
        .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
        .AddJwtBearer(options =>
        {
            options.Authority = cognitoAuthority;
            options.RequireHttpsMetadata = true;
            options.MapInboundClaims = false;
            options.TokenValidationParameters.ValidateAudience = false;
            options.Events.OnTokenValidated = async context =>
            {
                var principal = context.Principal
                    ?? throw new InvalidOperationException("The validated token has no principal.");
                var subject = principal.FindFirst("sub")?.Value;
                var tokenUse = principal.FindFirst("token_use")?.Value;
                var tokenClientId = principal.FindFirst("client_id")?.Value;
                if (string.IsNullOrWhiteSpace(subject)
                    || tokenUse != "access"
                    || !string.Equals(tokenClientId, cognitoClientId, StringComparison.Ordinal))
                {
                    context.Fail("The bearer token is not an access token for this Cognito app client.");
                    return;
                }

                var userId = subject;
                var scopeFactory = context.HttpContext.RequestServices.GetRequiredService<IServiceScopeFactory>();
                using var scope = scopeFactory.CreateScope();
                var store = scope.ServiceProvider.GetRequiredService<IChatStore>();
                if (await store.FindUserByIdAsync(userId, context.HttpContext.RequestAborted) is null)
                {
                    var identityLabel = principal.FindFirst("email")?.Value
                        ?? principal.FindFirst("username")?.Value
                        ?? subject;
                    var user = new LocalUser(
                        userId,
                        identityLabel,
                        string.Empty,
                        0,
                        DateTimeOffset.UtcNow);
                    if (!await store.AddUserAsync(user, context.HttpContext.RequestAborted))
                    {
                        _ = await store.FindUserByIdAsync(userId, context.HttpContext.RequestAborted)
                            ?? throw new InvalidOperationException(
                                "The Cognito identity profile could not be provisioned in the local store.");
                    }
                }
            };
        });
}
builder.Services.AddAuthorization();
builder.Services
    .AddMcpServer()
    .WithHttpTransport(options =>
    {
        options.SessionMode = HttpServerSessionMode.Stateless;
    })
    .WithToolsFromAssembly();
builder.Services.AddRateLimiter(options =>
{
    options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    options.OnRejected = (context, _) =>
    {
        context.HttpContext.Response.Headers.RetryAfter = "60";
        return ValueTask.CompletedTask;
    };
    options.AddPolicy("per-user", context =>
    {
        var userId = context.User.FindFirstValue(ClaimTypes.NameIdentifier)
            ?? context.Connection.RemoteIpAddress?.ToString()
            ?? "unknown";
        return RateLimitPartition.GetFixedWindowLimiter(
            userId,
            _ => new FixedWindowRateLimiterOptions
            {
                PermitLimit = requestsPerMinute,
                Window = TimeSpan.FromMinutes(1),
                QueueLimit = 0,
                AutoReplenishment = true
            });
    });
});

var app = builder.Build();
app.UseExceptionHandler();
app.Use(async (context, next) =>
{
    context.Response.Headers["X-Content-Type-Options"] = "nosniff";
    context.Response.Headers["Referrer-Policy"] = "no-referrer";
    context.Response.Headers["Content-Security-Policy"] =
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        + "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'";
    await next();
});
var reactClientEntry = Path.Combine(
    app.Environment.WebRootPath ?? Path.Combine(app.Environment.ContentRootPath, "wwwroot"),
    "client",
    "index.html");
var reactClientBuilt = File.Exists(reactClientEntry);
if (reactClientBuilt)
{
    var defaultFiles = new DefaultFilesOptions();
    defaultFiles.DefaultFileNames.Clear();
    defaultFiles.DefaultFileNames.Add("client/index.html");
    app.UseDefaultFiles(defaultFiles);
}
else
{
    app.UseDefaultFiles();
}
app.UseStaticFiles();
app.UseAuthentication();
app.UseRateLimiter();
app.UseAuthorization();
app.MapMcp("/mcp").RequireAuthorization().RequireRateLimiting("per-user");

if (authMode == "Local")
{
var auth = app.MapGroup("/api/auth").RequireRateLimiting("per-user");
auth.MapPost("/register", async (
    RegisterRequest request,
    HttpContext context,
    IChatStore store,
    IPasswordHasher<LocalUser> hasher) =>
{
    var email = request.Email?.Trim();
    if (string.IsNullOrWhiteSpace(email)
        || !new EmailAddressAttribute().IsValid(email)
        || email.Length > 254
        || string.IsNullOrWhiteSpace(request.Password)
        || request.Password.Length < 10
        || request.Password.Length > 128)
    {
        return Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["credentials"] = ["Enter a valid email address and a password between 10 and 128 characters."]
        });
    }

    var user = new LocalUser(
        Guid.NewGuid().ToString("N"),
        email,
        string.Empty,
        1000,
        DateTimeOffset.UtcNow);
    user = user with { PasswordHash = hasher.HashPassword(user, request.Password) };
    if (!await store.AddUserAsync(user, context.RequestAborted))
    {
        return Results.Conflict(new { error = "An account with this email already exists." });
    }

    await SignInAsync(context, user);
    return Results.Created("/api/me", ToUserResponse(user));
});

auth.MapPost("/login", async (
    LoginRequest request,
    HttpContext context,
    IChatStore store,
    IPasswordHasher<LocalUser> hasher) =>
{
    if (string.IsNullOrWhiteSpace(request.Email) || string.IsNullOrEmpty(request.Password))
    {
        return Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["credentials"] = ["Email and password are required."]
        });
    }

    var user = await store.FindUserByEmailAsync(request.Email.Trim(), context.RequestAborted);
    if (user is null
        || hasher.VerifyHashedPassword(user, user.PasswordHash, request.Password)
            == PasswordVerificationResult.Failed)
    {
        return Results.Problem(
            title: "Invalid email or password.",
            statusCode: StatusCodes.Status401Unauthorized);
    }

    await SignInAsync(context, user);
    return Results.Ok(ToUserResponse(user));
});

auth.MapPost("/logout", async (HttpContext context) =>
{
    await context.SignOutAsync(CookieAuthenticationDefaults.AuthenticationScheme);
    return Results.NoContent();
});
}

var api = app.MapGroup("/api").RequireAuthorization().RequireRateLimiting("per-user");
api.MapGet("/me", async (ClaimsPrincipal principal, IChatStore store, CancellationToken cancellationToken) =>
{
    var user = await store.FindUserByIdAsync(GetUserId(principal), cancellationToken);
    return user is null ? Results.Unauthorized() : Results.Ok(ToUserResponse(user));
});

api.MapGet("/conversations", async (
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
    Results.Ok(await store.ListConversationsAsync(GetUserId(principal), cancellationToken)));

api.MapPost("/conversations", async (
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
{
    var conversation = await store.CreateConversationAsync(GetUserId(principal), cancellationToken);
    return Results.Created($"/api/conversations/{conversation.Id}", conversation);
});

api.MapPut("/conversations/{conversationId}/archive", async (
    string conversationId,
    ArchiveConversationRequest request,
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
    await store.SetConversationArchivedAsync(
            GetUserId(principal),
            conversationId,
            request.IsArchived,
            cancellationToken)
        ? Results.NoContent()
        : Results.NotFound());

api.MapGet("/usage", async (
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
    Results.Ok(await store.GetUsageHistoryAsync(GetUserId(principal), cancellationToken)));

api.MapGet("/conversations/{conversationId}/messages", async (
    string conversationId,
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
{
    var messages = await store.ListMessagesAsync(
        GetUserId(principal),
        conversationId,
        cancellationToken);
    return messages is null ? Results.NotFound() : Results.Ok(messages);
});

api.MapGet("/conversations/{conversationId}/files", async (
    string conversationId,
    ClaimsPrincipal principal,
    IConversationFileStore fileStore,
    CancellationToken cancellationToken) =>
{
    try
    {
        var files = await fileStore.ListConversationFilesAsync(
            GetUserId(principal),
            conversationId,
            cancellationToken);
        return Results.Ok(files.Select(ToFileResponse));
    }
    catch (KeyNotFoundException)
    {
        return Results.NotFound(new { error = "Conversation not found." });
    }
});

api.MapPost("/conversations/{conversationId}/files", async (
    string conversationId,
    HttpRequest request,
    ClaimsPrincipal principal,
    ConversationFileService fileService,
    ConversationFileOptions options,
    CancellationToken cancellationToken) =>
{
    var encodedFileName = request.Headers["X-File-Name"].FirstOrDefault();
    var contentType = request.ContentType ?? string.Empty;
    if (string.IsNullOrWhiteSpace(encodedFileName) || request.ContentLength is > 0
        && request.ContentLength > options.MaximumBytes)
    {
        return Results.BadRequest(new { error = $"Provide a file name; files cannot exceed {options.MaximumBytes} bytes." });
    }

    try
    {
        var file = await fileService.UploadAsync(
            GetUserId(principal),
            conversationId,
            Uri.UnescapeDataString(encodedFileName),
            contentType,
            request.Body,
            cancellationToken);
        return Results.Created(
            $"/api/conversations/{conversationId}/files/{file.Id}",
            ToFileResponse(file));
    }
    catch (KeyNotFoundException)
    {
        return Results.NotFound(new { error = "Conversation not found." });
    }
    catch (InvalidDataException exception)
    {
        return Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["file"] = [exception.Message]
        });
    }
});

api.MapPost("/conversations/{conversationId}/messages", async (
    string conversationId,
    SendMessageRequest request,
    HttpContext context,
    ChatService chatService) =>
{
    if (string.IsNullOrWhiteSpace(request.Content) || request.Content.Length > 6000)
    {
        return Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["content"] = ["Message content must be between 1 and 6000 characters."]
        });
    }

    var idempotencyKey = context.Request.Headers["Idempotency-Key"].FirstOrDefault();
    if (idempotencyKey is { Length: > 128 })
    {
        return Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["Idempotency-Key"] = ["The idempotency key cannot exceed 128 characters."]
        });
    }

    if (request.FileIds is { Count: > 5 }
        || request.FileIds?.Any(fileId => string.IsNullOrWhiteSpace(fileId) || fileId.Length > 64) == true)
    {
        return Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["fileIds"] = ["Attach no more than five files with valid file identifiers."]
        });
    }

    try
    {
        var result = await chatService.SendAsync(
            GetUserId(context.User),
            conversationId,
            request.Content.Trim(),
            string.IsNullOrWhiteSpace(idempotencyKey) ? Guid.NewGuid().ToString("N") : idempotencyKey,
            context.TraceIdentifier,
            context.RequestAborted,
            request.FileIds ?? []);
        return Results.Ok(result);
    }
    catch (KeyNotFoundException)
    {
        return Results.NotFound(new { error = "Conversation not found." });
    }
    catch (InsufficientCreditsException)
    {
        return Results.Problem(
            title: "Not enough credits.",
            detail: "This local demo charges one credit per completed message.",
            statusCode: StatusCodes.Status402PaymentRequired);
    }
    catch (IdempotencyConflictException)
    {
        return Results.Conflict(new { error = "That idempotency key was already used for another request." });
    }
    catch (ContextCompactionConflictException exception)
    {
        return Results.Conflict(new { error = exception.Message });
    }
    catch (ContextWindowExceededException exception)
    {
        return Results.Problem(
            title: "Conversation context is too large.",
            detail: exception.Message,
            statusCode: StatusCodes.Status413PayloadTooLarge);
    }
    catch (NotSupportedException exception)
    {
        return Results.Problem(
            title: "Conversation context compaction is unavailable.",
            detail: exception.Message,
            statusCode: StatusCodes.Status503ServiceUnavailable);
    }
    catch (ToolExecutionLimitException exception)
    {
        return Results.Problem(
            title: "The model could not complete its tool workflow.",
            detail: exception.Message,
            statusCode: StatusCodes.Status502BadGateway);
    }
});

api.MapPost("/conversations/{conversationId}/messages/stream", async (
    string conversationId,
    SendMessageRequest request,
    HttpContext context,
    ChatService chatService) =>
{
    var idempotencyKey = context.Request.Headers["Idempotency-Key"].FirstOrDefault();
    if (string.IsNullOrWhiteSpace(request.Content) || request.Content.Length > 6000)
    {
        await Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["content"] = ["Message content must be between 1 and 6000 characters."]
        }).ExecuteAsync(context);
        return;
    }

    if (idempotencyKey is { Length: > 128 })
    {
        await Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["Idempotency-Key"] = ["The idempotency key cannot exceed 128 characters."]
        }).ExecuteAsync(context);
        return;
    }

    if (request.FileIds is { Count: > 5 }
        || request.FileIds?.Any(fileId => string.IsNullOrWhiteSpace(fileId) || fileId.Length > 64) == true)
    {
        await Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["fileIds"] = ["Attach no more than five files with valid file identifiers."]
        }).ExecuteAsync(context);
        return;
    }

    context.Response.ContentType = "text/event-stream";
    context.Response.Headers.CacheControl = "no-cache, no-transform";
    context.Response.Headers["X-Accel-Buffering"] = "no";
    var jsonOptions = new JsonSerializerOptions(JsonSerializerDefaults.Web);

    async Task WriteEventAsync(string eventName, object data)
    {
        await context.Response.WriteAsync(
            $"event: {eventName}\ndata: {JsonSerializer.Serialize(data, jsonOptions)}\n\n",
            context.RequestAborted);
        await context.Response.Body.FlushAsync(context.RequestAborted);
    }

    try
    {
        await foreach (var streamEvent in chatService.StreamAsync(
                           GetUserId(context.User),
                           conversationId,
                           request.Content.Trim(),
                           string.IsNullOrWhiteSpace(idempotencyKey)
                               ? Guid.NewGuid().ToString("N")
                               : idempotencyKey,
                           context.TraceIdentifier,
                           context.RequestAborted,
                           request.FileIds ?? []))
        {
            await WriteEventAsync(streamEvent.Type, streamEvent);
        }
    }
    catch (OperationCanceledException) when (context.RequestAborted.IsCancellationRequested)
    {
    }
    catch (KeyNotFoundException)
    {
        await WriteEventAsync("error", new { type = "error", title = "Conversation not found.", status = 404 });
    }
    catch (InsufficientCreditsException)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "Not enough credits.",
            detail = "This local demo charges one credit per completed message.",
            status = 402
        });
    }
    catch (IdempotencyConflictException)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "That idempotency key was already used for another request.",
            status = 409
        });
    }
    catch (ContextCompactionConflictException exception)
    {
        await WriteEventAsync("error", new { type = "error", title = exception.Message, status = 409 });
    }
    catch (ContextWindowExceededException exception)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "Conversation context is too large.",
            detail = exception.Message,
            status = 413
        });
    }
    catch (NotSupportedException exception)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "Conversation context compaction is unavailable.",
            detail = exception.Message,
            status = 503
        });
    }
    catch (ToolExecutionLimitException exception)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "The model could not complete its tool workflow.",
            detail = exception.Message,
            status = 502
        });
    }
    catch (HttpRequestException)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "The configured model provider could not complete the request.",
            status = 502
        });
    }
    catch (InvalidDataException)
    {
        await WriteEventAsync("error", new
        {
            type = "error",
            title = "The configured model provider returned an invalid response.",
            status = 502
        });
    }
});

api.MapDelete("/conversations/{conversationId}", async (
    string conversationId,
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
    await store.DeleteConversationAsync(GetUserId(principal), conversationId, cancellationToken)
        ? Results.NoContent()
        : Results.NotFound());

api.MapPost("/conversations/{conversationId}/share", async (
    string conversationId,
    ClaimsPrincipal principal,
    IChatStore store,
    HttpRequest request,
    CancellationToken cancellationToken) =>
{
    var token = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32))
        .TrimEnd('=')
        .Replace('+', '-')
        .Replace('/', '_');
    var tokenHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(token)));
    if (!await store.SetConversationShareAsync(
            GetUserId(principal),
            conversationId,
            tokenHash,
            cancellationToken))
    {
        return Results.NotFound();
    }

    var baseUrl = $"{request.Scheme}://{request.Host}";
    return Results.Ok(new { url = $"{baseUrl}/shared/{token}" });
});

api.MapDelete("/conversations/{conversationId}/share", async (
    string conversationId,
    ClaimsPrincipal principal,
    IChatStore store,
    CancellationToken cancellationToken) =>
    await store.RevokeConversationShareAsync(GetUserId(principal), conversationId, cancellationToken)
        ? Results.NoContent()
        : Results.NotFound());

app.MapGet("/api/shared/{token}", async (
    string token,
    IChatStore store,
    CancellationToken cancellationToken) =>
{
    var tokenHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(token)));
    var shared = await store.GetSharedConversationAsync(tokenHash, cancellationToken);
    return shared is null ? Results.NotFound() : Results.Ok(shared);
});

app.MapFallbackToFile(reactClientBuilt ? "client/index.html" : "index.html");
app.Run();

static string GetUserId(ClaimsPrincipal principal) =>
    principal.FindFirstValue(ClaimTypes.NameIdentifier)
    ?? principal.FindFirst("sub")?.Value
    ?? throw new InvalidOperationException("The authenticated principal has no user identifier.");

static UserResponse ToUserResponse(LocalUser user) => new(user.Id, user.Email, user.Credits);

static FileUploadResponse ToFileResponse(ConversationFile file) => new(
    file.Id,
    file.FileName,
    file.ContentType,
    file.SizeBytes,
    file.Sha256,
    file.CreatedAt);

static Task SignInAsync(HttpContext context, LocalUser user)
{
    var claims = new[]
    {
        new Claim(ClaimTypes.NameIdentifier, user.Id),
        new Claim(ClaimTypes.Name, user.Email),
        new Claim(ClaimTypes.Email, user.Email)
    };
    var identity = new ClaimsIdentity(claims, CookieAuthenticationDefaults.AuthenticationScheme);
    return context.SignInAsync(
        CookieAuthenticationDefaults.AuthenticationScheme,
        new ClaimsPrincipal(identity));
}

public sealed record RegisterRequest(string? Email, string? Password);
public sealed record LoginRequest(string? Email, string? Password);
public sealed record SendMessageRequest(string? Content, IReadOnlyCollection<string>? FileIds = null);
public sealed record ArchiveConversationRequest(bool IsArchived);
public sealed record UserResponse(string Id, string Email, int Credits);
public sealed record FileUploadResponse(
    string Id,
    string FileName,
    string ContentType,
    long SizeBytes,
    string Sha256,
    DateTimeOffset CreatedAt);
