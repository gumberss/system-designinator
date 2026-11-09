# ai-chatinator

An incrementally built AI chat platform. The approved target is a .NET 10 modular application, a React/Vite web client, background workers, service-owned DynamoDB data, an MCP integration boundary, and C# AWS CDK.

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
- A code-only C# CDK stack (`AiChatinator-{stage}`) for DynamoDB, private/versioned S3, Cognito, encrypted SNS/SQS(+DLQ), Secrets Manager, an HTTP API + API Lambda, an SQS worker Lambda, and a scheduled outbox-relay Lambda, with `dev` (delete everything) and `prod` (retain data) stages. One-command deploy/destroy scripts exist (see [Deploy to / remove from AWS](#deploy-to--remove-from-aws)).
- A React/Vite client can run as a separate local development server or be built into the API's static client directory.
- Architecture and ADRs in [docs/architecture.md](docs/architecture.md) and [docs/adrs/README.md](docs/adrs/README.md).

Some product requirements remain in progress. The current local JSON persistence and in-process throttling are single-instance development adapters. Arbitrary external MCP server registration, production identity flow, PDF image/OCR extraction, and asynchronous/quarantined uploads are not implemented.

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

## AWS integration (code-only, opt-in)

Local remains the default. AWS adapters are selected by configuration.

| Setting | Values | Effect |
|---|---|---|
| `Persistence:Provider` | `Local` (default) / `DynamoDb` | `DynamoDbChatStore` implements `IChatStore`, `IOutboxStore`, `IUsageRollupStore` |
| `Persistence:DynamoDb:ServiceUrl` | e.g. `http://localhost:8000` | Optional; points at DynamoDB Local instead of AWS |
| `Persistence:DynamoDb:ConversationsTable` / `UsageTable` / `AuditTable` | table names | Defaults match the CDK stack (`ai-chatinator-*`) |
| `DomainEvents:Transport` / `DomainEvents:TopicArn` | `Sns` + ARN | Outbox events published to SNS |
| `Outbox:DispatcherEnabled` | bool | Background relay drains pending outbox events |
| `Worker:Enabled` / `Worker:QueueUrl` | bool + SQS URL | `SqsDomainEventWorker` long-polls SQS (requires `DynamoDb`) |

Flow: chat turn → transactional write (messages + credits + outbox event) → `OutboxRelay` → SNS →
SQS → worker → `UsageRollupHandler` (daily usage counters). Delivery is **at-least-once**; consumers
dedupe by `eventId` (processed markers with 30-day TTL). Failed messages return to the queue and go to
the DLQ after 5 receives. Keys: every table uses `pk`/`sk`; conversations and usage have a sparse
`gsi1` (pending outbox / usage by date); usage and audit use TTL `expiresAt`.

Run against DynamoDB Local (Java required):

```powershell
java -jar C:\tools\dynamodb-local\DynamoDBLocal.jar -inMemory -port 8000
$env:Persistence__Provider = 'DynamoDb'
$env:Persistence__DynamoDb__ServiceUrl = 'http://localhost:8000'
```

The tables must already exist (in AWS they come from the CDK stack; against DynamoDB Local, create them with the
same pk/sk + gsi1 schema, as DynamoDbLocalFixture.CreateTablesAsync does in the tests).

`ChatStoreContractTests` runs the same suite against `LocalDataStore` and DynamoDB Local
(`DYNAMODB_LOCAL_DIR`, default `C:\tools\dynamodb-local`); DynamoDB tests are skipped if Java/JAR is missing.
The CDK stack (`infra/AiChatinator.Cdk`) defines the matching tables, GSIs, TTL, SNS→SQS(+DLQ), and a
least-privilege runtime role, with outputs for table names, topic ARN, and queue URL.
S3 is not yet used for attachment text (≤40k chars fits the 400 KB item limit); DynamoDB Streams is a
possible future replacement for the outbox poller.

## Deploy to / remove from AWS

Deploy/destroy is a C# console app (`infra/AiChatinator.Deploy`) run with `dotnet`; no PowerShell scripts
and no AWS CLI are needed (identity and Secrets Manager calls use the AWS SDK for .NET).
Prerequisites: .NET 10 SDK, AWS credentials in the default chain (env vars, `~/.aws` profile, or SSO), and
the CDK CLI (`npm install -g aws-cdk`, requires Node.js) which performs synth, asset upload and CloudFormation.

```bash
# Deploy (publishes linux-x64 Lambda bundles to artifacts/lambda, bootstraps CDK, deploys AiChatinator-dev)
dotnet run --project infra/AiChatinator.Deploy -- deploy

# With a real model; the key is written to Secrets Manager after deploy, never into the template
OPENAI_API_KEY=<key> dotnet run --project infra/AiChatinator.Deploy -- deploy --ai-provider OpenAiCompatible

# Remove everything from the dev stage
dotnet run --project infra/AiChatinator.Deploy -- destroy
```

Both commands accept `--stage dev|prod`, `--profile`, and `--region`; `deploy` also accepts
`--skip-bootstrap`, `destroy` accepts `--force` (skips the dev prompt). `prod` always asks for confirmation.
Run with `--help` for the full usage.
Outputs (API URL, user pool, table names, queue URL, …) are saved to `artifacts/cdk-outputs-{stage}.json`.

| Stage | On destroy |
|---|---|
| `dev` (default) | Everything is deleted: tables, bucket and its objects, user pool, topic/queues, secret, Lambdas, log groups. The KMS key enters the 7-day AWS minimum pending-deletion window. |
| `prod` | Tables (with PITR), bucket, user pool, and KMS key are **retained**; delete them manually if needed. |

Not removed in any stage: the shared `CDKToolkit` bootstrap stack.

Current limitations of the AWS shape: API Gateway HTTP APIs time out at 30 s and buffer responses, so
SSE answers arrive at once instead of streaming; the React client is not hosted (run it locally against
the API URL); in Lambda the API expects Cognito JWTs rather than the local cookie sessions.

Synthesize without deploying (no AWS calls; placeholder asset folders are enough):

```powershell
cd .\infra\AiChatinator.Cdk
cdk synth -c stage=dev -c apiAsset=<folder> -c workerAsset=<folder>
```
