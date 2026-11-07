# ai-chatinator

An incrementally built AI chat platform. The approved target is a .NET 10 modular application, a React/Vite web client, background workers, service-owned DynamoDB data, an MCP integration boundary, and C# AWS CDK. **This repository is being built and validated locally; it does not provision, deploy, or push anything to AWS.**

## Current implementation

- .NET 10 Domain, Application, Infrastructure, and ASP.NET Core API projects.
- Responsive same-origin chat UI.
- Local account registration and cookie sign-in.
- Conversation/message persistence, credit tracking, usage metadata, audit records, idempotency, and revocable read-only share links.
- Configurable per-user API throttle (default: 10 requests/minute; local process only).
- Explicit local demo provider, plus an opt-in OpenAI-compatible provider that reports actual provider token usage. No provider is called unless configured.
- An internal LLM adapter boundary with OpenAI-compatible streaming; the authenticated chat API relays SSE deltas to the React client, which renders text as it arrives. The regular JSON send endpoint remains available for compatibility.
- Versioned conversation-context summaries for providers that implement summarization; original messages remain in local history, and summary updates use optimistic version checks.
- A persistent local outbox: completed turns atomically enqueue a versioned, prompt-free domain event; a hosted dispatcher retries failures and records delivery state. The local publisher logs event metadata only and never contacts AWS.
- An opt-in Amazon SNS outbox publisher for later AWS use. The default event transport remains `Local`; SNS is only used when explicitly selected and configured.
- Conversation attachments for UTF-8 text, Markdown, and text-based PDFs. Uploads are size/type/signature checked, text is bounded, attached files are owner-scoped, and extracted content is labeled untrusted when included in model context.
- A bounded model tool-call loop with allow-listed `search_conversation` and
  `search_other_conversations` tools and strict output/round limits. The latter
  searches only the caller's other conversations (including archived chats),
  returning up to five recent matching messages with source IDs and bounded excerpts.
  Both are exposed through the authenticated `/mcp` endpoint. Chat orchestration
  discovers and invokes them using the MCP HTTP client, reusing the local search logic.
- AWS Lambda HTTP API hosting is registered for the ASP.NET Core API; local development continues to use the normal ASP.NET Core server.
- A code-only C# CDK foundation for DynamoDB, private/versioned S3, Cognito, encrypted SNS, and an encrypted SQS worker queue with a DLQ. Its template can be synthesized and asserted locally; it is not deployed.
- A React/Vite client can run as a separate local development server or be built into the API's static client directory.
- Architecture and ADRs in [docs/architecture.md](docs/architecture.md) and [docs/adrs/README.md](docs/adrs/README.md).

Some product requirements remain in progress. This is not yet a production deployment: the current local JSON persistence and in-process throttling are single-instance development adapters. The CDK stack defines foundation resources only; there is no Lambda function/API Gateway resource integration, application IAM wiring, or AWS persistence/event adapter. Arbitrary external MCP server registration, the standalone worker, production identity flow, PDF image/OCR extraction, and asynchronous/quarantined uploads are not implemented.

## Run locally

Requires .NET 10 SDK. The API can serve the legacy static client by default. To use the React client in development, open two terminals in the `ai-chatinator` directory:

```powershell
dotnet run --project .\src\AiChatinator.Api
```

```powershell
Push-Location .\frontend
npm ci
npm run dev
Pop-Location
```

The Vite server proxies `/api` and `/mcp` to `http://localhost:5055` by default; override that target with `AI_CHATINATOR_API_URL` if the API uses another address. Open the Vite URL it prints. Create an account with a valid email address and a password of at least 10 characters. Local data is written to `src/AiChatinator.Api/App_Data/chat-data.json` (Git-ignored); set `LocalData__Path` to override it.

To build the React client into the API's static files (the API then serves it at `/`), run from `ai-chatinator`:

```powershell
npm ci --prefix .\frontend
npm run build --prefix .\frontend
dotnet run --project .\src\AiChatinator.Api
```

The default provider is `LocalDemo`. It only echoes the submitted message and says it is not a real AI response. The repository includes an ignored `.env` for local settings and a tracked [.env.example](.env.example). Fill in `OPENAI_API_KEY` and select the provider when you are ready to send data to OpenAI:

```powershell
# In .env:
# AI_PROVIDER=OpenAiCompatible
# OPENAI_API_KEY=<your-provider-key>
# OPENAI_MODEL=gpt-4o-mini
dotnet run --project .\src\AiChatinator.Api
```

The app loads `.env` when started from the `ai-chatinator` directory. `OPENAI_API_KEY` and `OPENAI_MODEL` map to `Ai:OpenAiCompatible:ApiKey` and `Ai:OpenAiCompatible:Model`. Shell/process environment values with those names take precedence. `OPENAI_MODEL` defaults in `.env` to `gpt-4o-mini`; change it if needed. The OpenAI-compatible base URL defaults to `https://api.openai.com/v1/`; remote endpoints must use HTTPS, while HTTP is accepted only for loopback. Keep the provider set to `LocalDemo` for fully offline local testing. Selecting OpenAI sends conversation and attachment context to that provider.

Change the local per-user API limit with `RateLimiting__RequestsPerMinute`; default is `10`. The local limiter is process-local, not a distributed production limiter.

The OpenAI-compatible adapter sends `reasoning_effort="none"` for streaming and
non-streaming requests (including summaries). Use a model that supports this value.

Context compaction defaults to starting around 6,144 estimated tokens while retaining the latest 12 messages:

```powershell
$env:Ai__Context__CompactAtTokens = "6144"
$env:Ai__Context__KeepRecentMessages = "12"
```

The outbox uses its local logging publisher by default. To explicitly use SNS in an AWS environment, configure `DomainEvents__Transport=Sns` and `DomainEvents__TopicArn`, and provide AWS region and credentials through the standard AWS SDK provider chain. Do not set these options for local-only runs; selecting SNS causes the dispatcher to publish pending events to the configured topic.

Only providers that implement `SummarizeAsync` can compact history. The OpenAI-compatible adapter does; the offline demo provider intentionally does not. When compaction is unavailable or the retained recent messages alone exceed the configured budget, the API reports an explicit error rather than dropping history. Summary text and all original messages are persisted locally; compaction never deletes the conversation transcript.

Attach `.txt`, `.md`, or text-based `.pdf` files using the chat composer. The default maximum upload size is 10 MiB (`Files:MaximumBytes`). Extracted text is capped at 40,000 characters per file; scanned/image-only PDFs require OCR, which is not included. Local extracted content is stored in the same private development JSON file as conversations.

## MCP conversation tools

`/mcp` exposes instance status, `search_conversation`,
`search_other_conversations`, `list_conversation_ids`, and
`get_conversation_messages`. External MCP clients must authenticate using the
same local session cookie or configured Cognito bearer token as the API, and
provide `X-Conversation-Id` with a conversation they own when calling search tools.
Search arguments are `{ "query": "phrase" }`; user IDs are never tool arguments.

- `list_conversation_ids`: optional `offset` (default 0), pages of 20 owned IDs,
  including archived conversations. Follow `nextOffset` until null.
- `get_conversation_messages`: `conversationId`, optional `messageOffset` and
  `contentOffset` (both default 0). Returns chronological content fragments with
  message IDs, roles, timestamps and `isComplete`. Follow **both**
  `nextMessageOffset` and `nextContentOffset` until null. Pages contain at most
  3,000 content characters and 10 fragments; long messages are split without
  removing content. These tools read persisted messages, not original file binaries.

For "Find the number I sent in another conversation", orchestration instructs the
model to list IDs, exclude the active conversation, and read other conversations
and their remaining pages until it finds the number or finishes the investigation.
Tool selection is model-driven, not a guarantee of exhaustive model behavior.
Ambiguous matches should be reported rather than guessed.

Investigation limits are configurable: `Ai__Tools__MaximumRounds` (default 16),
`Ai__Tools__MaximumCalls` (default 30), and
`Ai__Tools__MaximumResultCharacters` (default 24000). Each model round permits at
most three tool calls. The existing `Ai__Context__CompactAtTokens` context budget
also applies. Exceeding a budget reports an explicit incomplete-investigation
error; oversized tool results are no longer silently truncated. Failed turns are
not persisted as successful answers.

The chat backend uses an HTTP MCP client against its own listening address,
forwards the current request's authentication and conversation context, discovers
the four allow-listed tool schemas, and invokes them through MCP. Search logic
and ownership checks remain shared in `LocalChatToolCatalog`; original messages
and attachments are not sent to a separate external MCP service.
MCP traffic also counts toward the process-local API rate limit; a search may
consume several requests for initialization, discovery, and invocation.
The MCP connection is reused only within one authenticated chat request and
disposed afterwards. The currently running local profile's 20 requests/minute
limit can stop a long investigation before the tool-call budget; this is reported
explicitly, rather than as an OpenAI failure.
Loopback MCP connections use the trusted `localhost` hostname, including when
the incoming connection uses IPv6, to work with hostname-bound local listeners.

## Tests

Rejected model requests log HTTP status and structured provider error fields
(`message`, `type`, `code`, `param`) in the API console. Diagnostics are bounded,
API-key values are redacted, and raw response bodies, request headers, prompts,
and attachments are not logged. The frontend continues to show a generic provider error.

```powershell
dotnet test .\tests\AiChatinator.Tests
```

Tests use temporary files, fake HTTP/SNS responses, and CDK template assertions; they require neither AWS nor provider credentials. Current tests cover account/chat persistence, idempotency, sharing, context compaction, outbox retry state, SNS event serialization/limits, local text attachments, file validation, tool-call orchestration, and the synthesized infrastructure foundation.

To synthesize the CDK template locally without deploying it:

```powershell
$env:CDK_OUTDIR = Join-Path (Get-Location) "infra\AiChatinator.Cdk\cdk.out"
dotnet run --project .\infra\AiChatinator.Cdk\AiChatinator.Cdk.csproj
```

Synthesis is not deployment and does not create AWS resources.

## AWS boundary

No AWS deployment has been run and no resources have been provisioned. Keep deployment and public URL access out of scope unless separately authorized.
