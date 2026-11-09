namespace AiChatinator.Deploy;

public sealed record DeployPaths(string Root)
{
    public string CdkDir => Path.Combine(Root, "infra", "AiChatinator.Cdk");
    public string Artifacts => Path.Combine(Root, "artifacts");
    public string ApiOut => Path.Combine(Artifacts, "lambda", "api");
    public string WorkerOut => Path.Combine(Artifacts, "lambda", "worker");
    public string ApiProject => Path.Combine(Root, "src", "AiChatinator.Api", "AiChatinator.Api.csproj");
    public string WorkerProject => Path.Combine(Root, "src", "AiChatinator.Worker", "AiChatinator.Worker.csproj");

    public string OutputsFile(string stage) => Path.Combine(Artifacts, $"cdk-outputs-{stage}.json");

    public static DeployPaths Discover(params string[] startDirectories)
    {
        foreach (var start in startDirectories)
        {
            for (var dir = new DirectoryInfo(start); dir is not null; dir = dir.Parent)
            {
                if (File.Exists(Path.Combine(dir.FullName, "AiChatinator.slnx")))
                {
                    return new DeployPaths(dir.FullName);
                }
            }
        }

        throw new DeployException("Could not find AiChatinator.slnx. Run the command from inside the ai-chatinator folder.");
    }
}
