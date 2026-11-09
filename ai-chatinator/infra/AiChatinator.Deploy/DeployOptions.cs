namespace AiChatinator.Deploy;

public enum DeployCommand
{
    Deploy,
    Destroy
}

public sealed class DeployException(string message) : Exception(message);

public sealed record DeployOptions(
    DeployCommand Command,
    string Stage = "dev",
    string AiProvider = "LocalDemo",
    string? AwsProfile = null,
    string? Region = null,
    bool SkipBootstrap = false,
    bool Force = false,
    bool ShowHelp = false)
{
    public const string Usage = """
        Usage:
          dotnet run --project infra/AiChatinator.Deploy -- deploy  [options]
          dotnet run --project infra/AiChatinator.Deploy -- destroy [options]

        Options:
          --stage dev|prod                       Target stage (default: dev)
          --ai-provider LocalDemo|OpenAiCompatible  AI provider for deploy (default: LocalDemo)
          --profile <name>                       AWS named profile (otherwise the default credential chain)
          --region <region>                      AWS region (otherwise the profile/env region)
          --skip-bootstrap                       deploy: skip 'cdk bootstrap'
          --force                                destroy: skip the confirmation prompt (dev only)
          -h, --help                             Show this help

        Environment:
          OPENAI_API_KEY   When set with --ai-provider OpenAiCompatible, it is stored in Secrets Manager.
        """;

    public string StackName => $"AiChatinator-{Stage}";

    public static DeployOptions Parse(IReadOnlyList<string> args)
    {
        if (args.Count == 0 || args.Any(arg => arg is "-h" or "--help"))
        {
            return new DeployOptions(DeployCommand.Deploy, ShowHelp: true);
        }

        var command = args[0].ToLowerInvariant() switch
        {
            "deploy" => DeployCommand.Deploy,
            "destroy" => DeployCommand.Destroy,
            _ => throw new DeployException($"Unknown command '{args[0]}'. Use 'deploy' or 'destroy'.")
        };
        var options = new DeployOptions(command);

        for (var i = 1; i < args.Count; i++)
        {
            var arg = args[i];
            options = arg.ToLowerInvariant() switch
            {
                "--stage" => options with { Stage = OneOf(arg, Value(args, ref i), "dev", "prod") },
                "--ai-provider" => options with
                {
                    AiProvider = OneOf(arg, Value(args, ref i), "LocalDemo", "OpenAiCompatible")
                },
                "--profile" => options with { AwsProfile = Value(args, ref i) },
                "--region" => options with { Region = Value(args, ref i) },
                "--skip-bootstrap" => options with { SkipBootstrap = true },
                "--force" => options with { Force = true },
                _ => throw new DeployException($"Unknown option '{arg}'. Use --help.")
            };
        }

        return options;
    }

    private static string Value(IReadOnlyList<string> args, ref int index)
    {
        if (index + 1 >= args.Count || args[index + 1].StartsWith("--", StringComparison.Ordinal))
        {
            throw new DeployException($"Option '{args[index]}' needs a value.");
        }

        return args[++index];
    }

    private static string OneOf(string option, string value, params string[] allowed) =>
        allowed.FirstOrDefault(candidate => candidate.Equals(value, StringComparison.OrdinalIgnoreCase))
        ?? throw new DeployException($"Invalid value '{value}' for {option}. Allowed: {string.Join(", ", allowed)}.");
}
