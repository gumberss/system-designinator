using Amazon.CDK;

var app = new App();
var account = app.Node.TryGetContext("account")?.ToString()
    ?? System.Environment.GetEnvironmentVariable("CDK_DEFAULT_ACCOUNT");
var region = app.Node.TryGetContext("region")?.ToString()
    ?? System.Environment.GetEnvironmentVariable("CDK_DEFAULT_REGION");

_ = new AiChatinatorStack(app, "AiChatinatorStack", new StackProps
{
    Env = string.IsNullOrWhiteSpace(account) || string.IsNullOrWhiteSpace(region)
        ? null
        : new Amazon.CDK.Environment { Account = account, Region = region },
    Description = "Code-only foundation for ai-chatinator. Synthesis does not deploy resources."
});

app.Synth();
