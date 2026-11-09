namespace AiChatinator.Deploy;

public static class DeployProgram
{
    public static async Task<int> Main(string[] args)
    {
        DeployOptions options;
        try
        {
            options = DeployOptions.Parse(args);
        }
        catch (DeployException ex)
        {
            Console.Error.WriteLine(ex.Message);
            Console.Error.WriteLine(DeployOptions.Usage);
            return 2;
        }

        if (options.ShowHelp)
        {
            Console.WriteLine(DeployOptions.Usage);
            return 0;
        }

        using var cancellation = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            cancellation.Cancel();
        };

        try
        {
            var paths = DeployPaths.Discover(Environment.CurrentDirectory, AppContext.BaseDirectory);
            var deployer = new Deployer(
                paths,
                new ProcessRunner(),
                new AwsAccess(),
                new SystemDeployConsole(),
                Environment.GetEnvironmentVariable,
                Environment.SetEnvironmentVariable);
            await deployer.RunAsync(options, cancellation.Token);
            return 0;
        }
        catch (DeployException ex)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.Error.WriteLine(ex.Message);
            Console.ResetColor();
            return 1;
        }
        catch (OperationCanceledException)
        {
            Console.Error.WriteLine("Cancelled.");
            return 130;
        }
    }
}
