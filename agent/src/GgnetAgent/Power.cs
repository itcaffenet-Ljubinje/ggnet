using System.Diagnostics;

namespace GgnetAgent;

/// <summary>Shuts the PC down or restarts it when the server asks (ggRock's Shutdown / Reboot).</summary>
public interface IPowerControl
{
    Task RunAsync(string command, CancellationToken ct);
}

/// <summary>shutdown.exe with a short delay and a message for whoever sits at the PC.</summary>
public sealed class WindowsPowerControl : IPowerControl
{
    public static readonly IReadOnlyList<string> Commands = ["shutdown", "reboot"];

    internal static string Arguments(string command) => command switch
    {
        "shutdown" => "/s /t 10 /c \"ggNet: this PC shuts down in 10 seconds.\"",
        "reboot" => "/r /t 10 /c \"ggNet: this PC restarts in 10 seconds.\"",
        _ => throw new ArgumentException($"Unknown power command: {command}"),
    };

    public async Task RunAsync(string command, CancellationToken ct)
    {
        var psi = new ProcessStartInfo("shutdown.exe", Arguments(command))
        {
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        using var process = Process.Start(psi) ?? throw new InvalidOperationException("Cannot start shutdown.exe");
        await process.WaitForExitAsync(ct);
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException($"shutdown.exe exited with code {process.ExitCode}");
        }
    }
}
