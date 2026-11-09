using AiChatinator.Deploy;

namespace AiChatinator.Tests;

public sealed class DeployToolTests : IDisposable
{
    private static readonly AwsIdentity Identity = new("123456789012", "us-east-1");
    private readonly string _root = Directory.CreateTempSubdirectory("deploy-tool-").FullName;
    private readonly FakeRunner _runner = new();
    private readonly FakeAws _aws = new();
    private readonly FakeConsole _console = new();
    private readonly Dictionary<string, string?> _env = new();

    public void Dispose() => Directory.Delete(_root, recursive: true);

    [Fact]
    public void Parse_reads_command_and_options()
    {
        var options = DeployOptions.Parse(["deploy", "--stage", "PROD", "--ai-provider", "openaicompatible",
            "--profile", "p", "--region", "eu-west-1", "--skip-bootstrap"]);

        Assert.Equal(DeployCommand.Deploy, options.Command);
        Assert.Equal("prod", options.Stage);
        Assert.Equal("OpenAiCompatible", options.AiProvider);
        Assert.Equal("p", options.AwsProfile);
        Assert.Equal("eu-west-1", options.Region);
        Assert.True(options.SkipBootstrap);
        Assert.Equal("AiChatinator-prod", options.StackName);
        Assert.True(DeployOptions.Parse([]).ShowHelp);
    }

    [Theory]
    [InlineData("launch")]
    [InlineData("deploy", "--stage", "qa")]
    [InlineData("deploy", "--region")]
    [InlineData("destroy", "--nope")]
    public void Parse_rejects_invalid_input(params string[] args) =>
        Assert.Throws<DeployException>(() => DeployOptions.Parse(args));

    [Fact]
    public void Deploy_plan_publishes_both_bundles_then_bootstraps_and_deploys()
    {
        var plan = CreateDeployer().BuildDeployPlan(DeployOptions.Parse(["deploy"]), Identity);

        Assert.Collection(plan,
            step => Assert.Contains(Path.Combine("lambda", "api"), step.Arguments[^1]),
            step => Assert.Contains(Path.Combine("lambda", "worker"), step.Arguments[^1]),
            step => Assert.Equal(["bootstrap", "aws://123456789012/us-east-1"], step.Arguments.Take(2)),
            step =>
            {
                Assert.Equal(["deploy", "AiChatinator-dev"], step.Arguments.Take(2));
                Assert.Contains("aiProvider=LocalDemo", step.Arguments);
                Assert.Contains("account=123456789012", step.Arguments);
                Assert.Contains("never", step.Arguments);
            });
        Assert.All(plan.Take(2), step => Assert.Equal(["publish"], step.Arguments.Take(1)));
        Assert.All(plan.Skip(2), step => Assert.Equal("cdk", step.FileName));

        var skipped = CreateDeployer().BuildDeployPlan(DeployOptions.Parse(["deploy", "--skip-bootstrap"]), Identity);
        Assert.DoesNotContain(skipped, step => step.Arguments[0] == "bootstrap");
    }

    [Fact]
    public async Task Deploy_runs_plan_sets_aws_env_and_stores_openai_key()
    {
        _env["OPENAI_API_KEY"] = "sk-test";
        WriteOutputs("dev");

        await CreateDeployer().RunAsync(
            DeployOptions.Parse(["deploy", "--ai-provider", "OpenAiCompatible", "--profile", "me", "--region", "us-east-1"]),
            CancellationToken.None);

        Assert.Equal(["publish", "publish", "bootstrap", "deploy"], _runner.Steps.Select(s => s.Arguments[0]));
        Assert.Equal("me", _env["AWS_PROFILE"]);
        Assert.Equal("us-east-1", _env["AWS_DEFAULT_REGION"]);
        Assert.Equal(("arn:aws:secretsmanager:us-east-1:123456789012:secret:x", "sk-test"), _aws.PutSecret);
        Assert.Contains(_console.Lines, line => line.Contains("https://api.example"));
    }

    [Fact]
    public async Task Prod_deploy_requires_typing_stack_name()
    {
        _console.Answer = "wrong";

        await Assert.ThrowsAsync<DeployException>(() =>
            CreateDeployer().RunAsync(DeployOptions.Parse(["deploy", "--stage", "prod"]), CancellationToken.None));
        Assert.Empty(_runner.Steps);
    }

    [Fact]
    public async Task Failed_step_stops_deploy()
    {
        _runner.ExitCode = 1;

        var error = await Assert.ThrowsAsync<DeployException>(() =>
            CreateDeployer().RunAsync(DeployOptions.Parse(["deploy"]), CancellationToken.None));
        Assert.Contains("exit code 1", error.Message);
        Assert.Single(_runner.Steps);
    }

    [Fact]
    public async Task Missing_cdk_is_reported_before_touching_aws()
    {
        _runner.Missing.Add("cdk");

        var error = await Assert.ThrowsAsync<DeployException>(() =>
            CreateDeployer().RunAsync(DeployOptions.Parse(["destroy", "--force"]), CancellationToken.None));
        Assert.Contains("npm install -g aws-cdk", error.Message);
        Assert.False(_aws.IdentityRequested);
    }

    [Fact]
    public async Task Destroy_with_force_creates_placeholders_and_runs_cdk_destroy()
    {
        await CreateDeployer().RunAsync(DeployOptions.Parse(["destroy", "--force"]), CancellationToken.None);

        var step = Assert.Single(_runner.Steps);
        Assert.Equal(["destroy", "AiChatinator-dev", "--force"], step.Arguments.Take(3));
        Assert.DoesNotContain(step.Arguments, arg => arg.StartsWith("aiProvider=", StringComparison.Ordinal));
        Assert.True(File.Exists(Path.Combine(_root, "artifacts", "lambda", "api", "placeholder.txt")));
        Assert.True(File.Exists(Path.Combine(_root, "artifacts", "lambda", "worker", "placeholder.txt")));
        Assert.Null(_console.LastPrompt);
    }

    [Fact]
    public async Task Prod_destroy_always_asks_even_with_force()
    {
        _console.Answer = "AiChatinator-prod";

        await CreateDeployer().RunAsync(DeployOptions.Parse(["destroy", "--stage", "prod", "--force"]), CancellationToken.None);

        Assert.NotNull(_console.LastPrompt);
        Assert.Single(_runner.Steps);
        Assert.Contains(_console.Lines, line => line.Contains("retains"));
    }

    [Fact]
    public void Discover_walks_up_to_solution_root()
    {
        File.WriteAllText(Path.Combine(_root, "AiChatinator.slnx"), "<Solution />");
        var nested = Directory.CreateDirectory(Path.Combine(_root, "a", "b")).FullName;

        Assert.Equal(_root, DeployPaths.Discover(nested).Root);
    }

    private Deployer CreateDeployer() => new(
        new DeployPaths(_root), _runner, _aws, _console,
        name => _env.GetValueOrDefault(name), (name, value) => _env[name] = value);

    private void WriteOutputs(string stage)
    {
        var paths = new DeployPaths(_root);
        Directory.CreateDirectory(paths.Artifacts);
        File.WriteAllText(paths.OutputsFile(stage), $$$"""
            {"AiChatinator-{{{stage}}}": {
              "ApiUrl": "https://api.example",
              "UserPoolId": "pool",
              "UserPoolClientId": "client",
              "OpenAiSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:x"
            }}
            """);
    }

    private sealed class FakeRunner : IProcessRunner
    {
        public List<ProcessStep> Steps { get; } = [];
        public HashSet<string> Missing { get; } = [];
        public int ExitCode { get; set; }

        public bool Exists(string fileName) => !Missing.Contains(fileName);

        public Task<int> RunAsync(ProcessStep step, CancellationToken cancellationToken)
        {
            Steps.Add(step);
            return Task.FromResult(ExitCode);
        }
    }

    private sealed class FakeAws : IAwsAccess
    {
        public bool IdentityRequested { get; private set; }
        public (string Arn, string Value)? PutSecret { get; private set; }

        public Task<AwsIdentity> GetIdentityAsync(CancellationToken cancellationToken)
        {
            IdentityRequested = true;
            return Task.FromResult(Identity);
        }

        public Task PutSecretAsync(string secretArn, string value, CancellationToken cancellationToken)
        {
            PutSecret = (secretArn, value);
            return Task.CompletedTask;
        }
    }

    private sealed class FakeConsole : IDeployConsole
    {
        public List<string> Lines { get; } = [];
        public string? Answer { get; set; }
        public string? LastPrompt { get; private set; }

        public void Info(string message) => Lines.Add(message);
        public void Step(string message) => Lines.Add(message);
        public void Success(string message) => Lines.Add(message);
        public void Warn(string message) => Lines.Add(message);

        public string? Prompt(string message)
        {
            LastPrompt = message;
            return Answer;
        }
    }
}
