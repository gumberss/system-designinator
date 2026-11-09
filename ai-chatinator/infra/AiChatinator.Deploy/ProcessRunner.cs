using System.Diagnostics;

namespace AiChatinator.Deploy;

public sealed record ProcessStep(string Description, string FileName, IReadOnlyList<string> Arguments, string WorkingDirectory);

public interface IProcessRunner
{
    bool Exists(string fileName);

    Task<int> RunAsync(ProcessStep step, CancellationToken cancellationToken);
}

/// <summary>Runs child processes with the console inherited so dotnet/cdk output streams live.</summary>
public sealed class ProcessRunner : IProcessRunner
{
    public bool Exists(string fileName) => Resolve(fileName) is not null;

    public async Task<int> RunAsync(ProcessStep step, CancellationToken cancellationToken)
    {
        var executable = Resolve(step.FileName)
            ?? throw new DeployException($"'{step.FileName}' was not found on PATH.");
        var info = new ProcessStartInfo(executable)
        {
            WorkingDirectory = step.WorkingDirectory,
            UseShellExecute = false
        };
        foreach (var argument in step.Arguments)
        {
            info.ArgumentList.Add(argument);
        }

        using var process = Process.Start(info)
            ?? throw new DeployException($"Could not start '{step.FileName}'.");
        try
        {
            await process.WaitForExitAsync(cancellationToken);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            throw;
        }

        return process.ExitCode;
    }

    // npm installs cdk as cdk.cmd on Windows, which Process.Start cannot find without the extension.
    private static string? Resolve(string fileName)
    {
        if (Path.IsPathRooted(fileName))
        {
            return File.Exists(fileName) ? fileName : null;
        }

        var extensions = OperatingSystem.IsWindows()
            ? (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD;.BAT").Split(';', StringSplitOptions.RemoveEmptyEntries)
            : [];
        var directories = (Environment.GetEnvironmentVariable("PATH") ?? string.Empty)
            .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries);
        foreach (var directory in directories)
        {
            foreach (var candidate in extensions.Select(ext => fileName + ext).Prepend(fileName))
            {
                var path = Path.Combine(directory.Trim('"'), candidate);
                if (File.Exists(path) && (!OperatingSystem.IsWindows() || Path.HasExtension(path)))
                {
                    return path;
                }
            }
        }

        return null;
    }
}
