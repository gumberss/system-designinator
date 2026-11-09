using System.Text.Json;

namespace AiChatinator.Deploy;

public interface IDeployConsole
{
    void Info(string message);

    void Step(string message);

    void Success(string message);

    void Warn(string message);

    string? Prompt(string message);
}

public sealed class SystemDeployConsole : IDeployConsole
{
    public void Info(string message) => Console.WriteLine(message);

    public void Step(string message) => Write(ConsoleColor.Cyan, $"==> {message}");

    public void Success(string message) => Write(ConsoleColor.Green, message);

    public void Warn(string message) => Write(ConsoleColor.Yellow, $"WARNING: {message}");

    public string? Prompt(string message)
    {
        Console.Write($"{message}: ");
        return Console.ReadLine();
    }

    private static void Write(ConsoleColor color, string message)
    {
        var previous = Console.ForegroundColor;
        Console.ForegroundColor = color;
        Console.WriteLine(message);
        Console.ForegroundColor = previous;
    }
}

/// <summary>
/// One-command deploy/destroy of the AiChatinator CDK stack. Uses the AWS SDK for identity and
/// secrets (no AWS CLI needed) and the CDK CLI for synth, asset upload and CloudFormation.
/// </summary>
public sealed class Deployer(
    DeployPaths paths,
    IProcessRunner runner,
    IAwsAccess aws,
    IDeployConsole console,
    Func<string, string?> getEnvironment,
    Action<string, string?> setEnvironment)
{
    public async Task RunAsync(DeployOptions options, CancellationToken cancellationToken)
    {
        RequireTools(options.Command == DeployCommand.Deploy ? ["dotnet", "cdk"] : ["cdk"]);
        ApplyAwsEnvironment(options);

        console.Step("Checking AWS credentials");
        var identity = await aws.GetIdentityAsync(cancellationToken);
        console.Info($"    Account {identity.Account}, region {identity.Region}, stage {options.Stage}");

        if (options.Command == DeployCommand.Deploy)
        {
            await DeployAsync(options, identity, cancellationToken);
        }
        else
        {
            await DestroyAsync(options, identity, cancellationToken);
        }
    }

    public IReadOnlyList<ProcessStep> BuildDeployPlan(DeployOptions options, AwsIdentity identity)
    {
        var steps = new List<ProcessStep>
        {
            Publish("Publishing API Lambda bundle", paths.ApiProject, paths.ApiOut),
            Publish("Publishing Worker Lambda bundle", paths.WorkerProject, paths.WorkerOut)
        };
        var context = Context(options, identity, includeAiProvider: true);
        if (!options.SkipBootstrap)
        {
            steps.Add(new ProcessStep("Bootstrapping CDK (idempotent)", "cdk",
                ["bootstrap", $"aws://{identity.Account}/{identity.Region}", .. context], paths.CdkDir));
        }

        steps.Add(new ProcessStep($"Deploying {options.StackName}", "cdk",
            ["deploy", options.StackName, .. context, "--require-approval", "never",
                "--outputs-file", paths.OutputsFile(options.Stage)], paths.CdkDir));
        return steps;
    }

    public ProcessStep BuildDestroyStep(DeployOptions options, AwsIdentity identity) =>
        new($"Destroying {options.StackName}", "cdk",
            ["destroy", options.StackName, "--force", .. Context(options, identity, includeAiProvider: false)],
            paths.CdkDir);

    private async Task DeployAsync(DeployOptions options, AwsIdentity identity, CancellationToken cancellationToken)
    {
        var apiKey = getEnvironment("OPENAI_API_KEY");
        if (options.AiProvider == "OpenAiCompatible" && string.IsNullOrWhiteSpace(apiKey))
        {
            console.Warn("OPENAI_API_KEY is not set. The secret keeps a generated value; set OPENAI_API_KEY and deploy again to store it.");
        }

        if (options.Stage == "prod")
        {
            Confirm($"Deploy '{options.StackName}' to account {identity.Account} ({identity.Region})? Type the stack name to confirm", options);
        }

        foreach (var dir in new[] { paths.ApiOut, paths.WorkerOut })
        {
            if (Directory.Exists(dir))
            {
                Directory.Delete(dir, recursive: true);
            }

            Directory.CreateDirectory(dir);
        }

        foreach (var step in BuildDeployPlan(options, identity))
        {
            await RunStepAsync(step, cancellationToken);
        }

        var outputs = ReadOutputs(paths.OutputsFile(options.Stage), options.StackName);
        if (options.AiProvider == "OpenAiCompatible" && !string.IsNullOrWhiteSpace(apiKey))
        {
            console.Step("Storing OPENAI_API_KEY in Secrets Manager");
            var secretArn = outputs.GetValueOrDefault("OpenAiSecretArn")
                ?? throw new DeployException("The stack outputs do not include OpenAiSecretArn.");
            await aws.PutSecretAsync(secretArn, apiKey, cancellationToken);
        }

        console.Info(string.Empty);
        console.Success($"Deployed {options.StackName}");
        console.Info($"  API URL:        {outputs.GetValueOrDefault("ApiUrl")}");
        console.Info($"  User pool:      {outputs.GetValueOrDefault("UserPoolId")} (client {outputs.GetValueOrDefault("UserPoolClientId")})");
        console.Info($"  Outputs file:   {paths.OutputsFile(options.Stage)}");
        console.Info($"  Remove it with: dotnet run --project infra/AiChatinator.Deploy -- destroy --stage {options.Stage}");
    }

    private async Task DestroyAsync(DeployOptions options, AwsIdentity identity, CancellationToken cancellationToken)
    {
        if (!options.Force || options.Stage == "prod")
        {
            Confirm($"Delete '{options.StackName}' from account {identity.Account} ({identity.Region})? Type the stack name to confirm", options);
        }

        EnsurePlaceholderAssets();
        await RunStepAsync(BuildDestroyStep(options, identity), cancellationToken);

        console.Info(string.Empty);
        console.Success($"Destroyed {options.StackName}");
        if (options.Stage == "prod")
        {
            console.Warn("prod retains DynamoDB tables, the S3 bucket, the Cognito user pool and the KMS key. Delete them manually if needed.");
        }
        else
        {
            console.Info("  The KMS key is pending deletion for 7 days (AWS minimum) and is not billed meanwhile.");
        }

        console.Info("  The CDKToolkit bootstrap stack is shared and was kept (remove it via CloudFormation if unused).");
    }

    // CDK must synthesize the app to destroy it; placeholder assets are enough when bundles were never built.
    public void EnsurePlaceholderAssets()
    {
        foreach (var dir in new[] { paths.ApiOut, paths.WorkerOut })
        {
            if (!Directory.Exists(dir) || !Directory.EnumerateFileSystemEntries(dir).Any())
            {
                Directory.CreateDirectory(dir);
                File.WriteAllText(Path.Combine(dir, "placeholder.txt"), "placeholder for cdk destroy");
            }
        }
    }

    public static IReadOnlyDictionary<string, string> ReadOutputs(string outputsFile, string stackName)
    {
        if (!File.Exists(outputsFile))
        {
            throw new DeployException($"CDK outputs file '{outputsFile}' was not written.");
        }

        using var document = JsonDocument.Parse(File.ReadAllText(outputsFile));
        if (!document.RootElement.TryGetProperty(stackName, out var stack))
        {
            throw new DeployException($"CDK outputs file has no entry for '{stackName}'.");
        }

        return stack.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.ToString());
    }

    private void RequireTools(IEnumerable<string> tools)
    {
        foreach (var tool in tools)
        {
            if (!runner.Exists(tool))
            {
                throw new DeployException(tool == "cdk"
                    ? "'cdk' was not found on PATH. Install the CDK CLI: npm install -g aws-cdk"
                    : $"'{tool}' was not found on PATH.");
            }
        }
    }

    private void ApplyAwsEnvironment(DeployOptions options)
    {
        if (options.AwsProfile is { Length: > 0 } profile)
        {
            setEnvironment("AWS_PROFILE", profile);
        }

        if (options.Region is { Length: > 0 } region)
        {
            setEnvironment("AWS_REGION", region);
            setEnvironment("AWS_DEFAULT_REGION", region);
        }
    }

    private void Confirm(string message, DeployOptions options)
    {
        if (console.Prompt(message)?.Trim() != options.StackName)
        {
            throw new DeployException("Aborted.");
        }
    }

    private async Task RunStepAsync(ProcessStep step, CancellationToken cancellationToken)
    {
        console.Step(step.Description);
        var exitCode = await runner.RunAsync(step, cancellationToken);
        if (exitCode != 0)
        {
            throw new DeployException($"{step.Description} failed (exit code {exitCode}).");
        }
    }

    private ProcessStep Publish(string description, string project, string output) =>
        new(description, "dotnet",
            ["publish", project, "-c", "Release", "-r", "linux-x64", "--self-contained", "false", "-o", output],
            paths.Root);

    private string[] Context(DeployOptions options, AwsIdentity identity, bool includeAiProvider)
    {
        var values = new List<string> { $"stage={options.Stage}" };
        if (includeAiProvider)
        {
            values.Add($"aiProvider={options.AiProvider}");
        }

        values.AddRange([
            $"apiAsset={paths.ApiOut}",
            $"workerAsset={paths.WorkerOut}",
            $"account={identity.Account}",
            $"region={identity.Region}"
        ]);
        return values.SelectMany(value => new[] { "-c", value }).ToArray();
    }
}
