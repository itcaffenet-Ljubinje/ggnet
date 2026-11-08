using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace GgnetAgent;

/// <summary>
/// Disk Mode: updates this agent to the package the server offers in its
/// heartbeat answer (Settings -> Client app). Each file is downloaded from
/// the server, checked against its size and SHA-256, and the package's own
/// install.ps1 is started apart from this service: it stops the service,
/// replaces ggnet-agent.exe, keeps appsettings.json and starts the service
/// again (also when the copy fails). The game disk stays connected.
/// </summary>
public sealed partial class AgentUpdater(
    ServerClient server,
    IUpdateLauncher launcher,
    ILogger<AgentUpdater> logger,
    TimeProvider time,
    string? root = null)
{
    /// <summary>Only these files are fetched, whatever else a server lists.</summary>
    internal static readonly IReadOnlySet<string> KnownFiles =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "ggnet-agent.exe", "install.ps1", "uninstall.ps1" };

    internal const string Installer = "install.ps1";
    /// <summary>After a failed update, the same version is tried again this much later.</summary>
    internal static readonly TimeSpan RetryAfter = TimeSpan.FromHours(1);
    internal static readonly TimeSpan DownloadTimeout = TimeSpan.FromMinutes(10);

    // Under Program Files: only administrators may write there, so nobody can
    // swap a checked file before install.ps1 runs as SYSTEM.
    private readonly string _root = root ?? Path.Combine(AppContext.BaseDirectory, "update");
    private string? _failedVersion;
    private DateTimeOffset _retryAt;

    /// <summary>Set once install.ps1 runs: this service is about to be replaced.</summary>
    internal bool Started { get; private set; }

    /// <summary>The update folder, with one subfolder per version.</summary>
    internal string Root => _root;

    /// <summary>Starts an update when the server offers a newer agent; true when install.ps1 runs.</summary>
    public async Task<bool> TryUpdateAsync(AgentConfig? config, string currentVersion, CancellationToken ct)
    {
        if (Started || config?.Update is not { } update || config.Mode != "disk") return false;
        if (!Versions.IsNewer(update.Version, currentVersion)) return false;
        if (_failedVersion == update.Version && time.GetUtcNow() < _retryAt) return false;

        try
        {
            Check(update);
            var folder = Path.Combine(_root, update.Version);
            if (Directory.Exists(_root))
            {
                Directory.Delete(_root, recursive: true);   // older attempts and their logs
            }
            Directory.CreateDirectory(folder);
            foreach (var file in update.Files)
            {
                await DownloadAsync(file, folder, ct);
            }
            logger.LogInformation("Updating ggnet-agent {Current} -> {Version}; log {Log}",
                currentVersion, update.Version, Path.Combine(folder, "update.log"));
            launcher.Launch(Path.Combine(folder, Installer), Path.Combine(folder, "update.log"));
            Started = true;
            return true;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e)
        {
            _failedVersion = update.Version;
            _retryAt = time.GetUtcNow() + RetryAfter;
            logger.LogWarning("Updating ggnet-agent to {Version} failed (next try in {Hours} h): {Error}",
                update.Version, RetryAfter.TotalHours, e.Message);
            return false;
        }
    }

    /// <summary>The offer must be a version and the known files with their checksums, install.ps1 included.</summary>
    internal static void Check(AgentUpdate update)
    {
        if (!Versions.TryParse(update.Version, out _))
        {
            throw new ServerException($"Server offered an invalid agent version: {update.Version}");
        }
        foreach (var f in update.Files)
        {
            if (!KnownFiles.Contains(f.Name)) throw new ServerException($"Server offered an unknown file: {f.Name}");
            if (!Sha256Hex().IsMatch(f.Sha256)) throw new ServerException($"Server sent a bad checksum for {f.Name}");
            if (f.SizeBytes is < 0 or > 1L << 30) throw new ServerException($"Server sent a bad size for {f.Name}");
        }
        foreach (var needed in new[] { "ggnet-agent.exe", Installer })
        {
            if (!update.Files.Any(f => string.Equals(f.Name, needed, StringComparison.OrdinalIgnoreCase)))
            {
                throw new ServerException($"The agent package has no {needed}");
            }
        }
    }

    private async Task DownloadAsync(UpdateFile file, string folder, CancellationToken ct)
    {
        var target = Path.Combine(folder, file.Name);
        var part = target + ".part";
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(DownloadTimeout);
        await using (var output = File.Create(part))
        {
            await server.DownloadAsync(file.Name, output, cts.Token);
        }
        var size = new FileInfo(part).Length;
        if (size != file.SizeBytes)
        {
            throw new ServerException($"{file.Name}: got {size} bytes, expected {file.SizeBytes}");
        }
        string hash;
        await using (var input = File.OpenRead(part))
        {
            hash = Convert.ToHexStringLower(await SHA256.HashDataAsync(input, ct));
        }
        if (!string.Equals(hash, file.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            throw new ServerException($"{file.Name}: SHA-256 mismatch; the download is damaged");
        }
        File.Move(part, target, overwrite: true);
    }

    [GeneratedRegex("^[0-9a-fA-F]{64}$")]
    private static partial Regex Sha256Hex();
}

/// <summary>Starts install.ps1 so that it outlives this service.</summary>
public interface IUpdateLauncher
{
    void Launch(string installScript, string logFile);
}

/// <summary>
/// powershell.exe as a separate process: a service's child processes are not
/// stopped with it, so install.ps1 goes on while it stops and replaces the
/// service, and writes everything it says to the log next to it.
/// </summary>
public sealed class PowerShellUpdateLauncher : IUpdateLauncher
{
    internal static string Command(string installScript, string logFile) =>
        $"& '{installScript.Replace("'", "''")}' -Update *> '{logFile.Replace("'", "''")}'";

    public void Launch(string installScript, string logFile)
    {
        var psi = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(installScript)!,
        };
        foreach (var arg in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-Command", Command(installScript, logFile) })
        {
            psi.ArgumentList.Add(arg);
        }
        using var process = Process.Start(psi) ?? throw new InvalidOperationException("Cannot start powershell.exe");
    }
}

/// <summary>Release versions as the server compares them: 0.2.0, v0.2.0, 0.2.0-rc1 (before 0.2.0).</summary>
public static partial class Versions
{
    public static bool TryParse(string? version, out (int, int, int, int, string) key)
    {
        key = default;
        var m = version is null ? Match.Empty : Tag().Match(version.Trim());
        if (!m.Success) return false;
        var pre = m.Groups[4].Success ? m.Groups[4].Value : "";
        key = (Number(m.Groups[1].Value), Number(m.Groups[2].Value), Number(m.Groups[3].Value),
               pre.Length > 0 ? 0 : 1, pre);
        return true;
    }

    /// <summary>Whether candidate is newer; never for a version either side cannot read.</summary>
    public static bool IsNewer(string? candidate, string? current)
    {
        if (!TryParse(candidate, out var a) || !TryParse(current, out var b)) return false;
        var c = (a.Item1, a.Item2, a.Item3, a.Item4).CompareTo((b.Item1, b.Item2, b.Item3, b.Item4));
        return c != 0 ? c > 0 : string.CompareOrdinal(a.Item5, b.Item5) > 0;
    }

    private static int Number(string s) =>
        int.TryParse(s, NumberStyles.None, CultureInfo.InvariantCulture, out var n) ? n : int.MaxValue;

    [GeneratedRegex(@"^v?(\d+)\.(\d+)\.(\d+)(?:[.-]([A-Za-z0-9.]+))?$")]
    private static partial Regex Tag();
}
