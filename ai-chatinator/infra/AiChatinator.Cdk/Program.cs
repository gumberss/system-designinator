using Amazon.CDK;

var app = new App();
string? Context(string key) => app.Node.TryGetContext(key)?.ToString();

var account = Context("account") ?? System.Environment.GetEnvironmentVariable("CDK_DEFAULT_ACCOUNT");
var region = Context("region") ?? System.Environment.GetEnvironmentVariable("CDK_DEFAULT_REGION");
var stage = Context("stage") ?? "dev";
var defaults = new AiChatinatorStackProps();

_ = new AiChatinatorStack(app, $"AiChatinator-{stage}", new AiChatinatorStackProps
{
    Env = string.IsNullOrWhiteSpace(account) || string.IsNullOrWhiteSpace(region)
        ? null
        : new Amazon.CDK.Environment { Account = account, Region = region },
    Description = $"ai-chatinator ({stage}).",
    Stage = stage,
    ApiAssetPath = Context("apiAsset") ?? defaults.ApiAssetPath,
    WorkerAssetPath = Context("workerAsset") ?? defaults.WorkerAssetPath,
    AiProvider = Context("aiProvider") ?? defaults.AiProvider
});

app.Synth();
