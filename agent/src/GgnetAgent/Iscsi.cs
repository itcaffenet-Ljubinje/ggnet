using System.Diagnostics;
using System.Text;

namespace GgnetAgent;

/// <summary>The Windows iSCSI initiator, as far as the agent needs it.</summary>
public interface IIscsiInitiator
{
    Task<string?> GetInitiatorNameAsync(CancellationToken ct);
    Task SetInitiatorNameAsync(string currentIqn, string newIqn, CancellationToken ct);
    Task<bool> IsConnectedAsync(string targetIqn, CancellationToken ct);
    /// <summary>Connects the target and returns the drive letter the game disk got.</summary>
    Task<string> ConnectAsync(string portalIp, int portalPort, string targetIqn, string driveLetter, CancellationToken ct);
    Task DisconnectAsync(string targetIqn, CancellationToken ct);
}

public interface IScriptRunner
{
    /// <summary>Runs a PowerShell script and returns its trimmed stdout; throws on failure.</summary>
    Task<string> RunAsync(string script, CancellationToken ct);
}

public sealed class ScriptException(string message) : Exception(message);

/// <summary>Runs scripts with Windows PowerShell (always present on Windows 11).</summary>
public sealed class PowerShellRunner : IScriptRunner
{
    /// <summary>
    /// The whole script as one -EncodedCommand (base64 UTF-16LE), so it is
    /// parsed like a .ps1 file and nothing in it is read as a separate
    /// command-line argument. Not `-Command -`: PowerShell reads stdin like an
    /// interactive console, and a multi-line statement at the end of the
    /// input (with no empty line after it) was never run.
    /// </summary>
    internal static string Arguments(string script)
    {
        var full = "$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n" + script;
        return "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
               + Convert.ToBase64String(Encoding.Unicode.GetBytes(full));
    }

    public async Task<string> RunAsync(string script, CancellationToken ct)
    {
        var psi = new ProcessStartInfo("powershell.exe", Arguments(script))
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        using var process = Process.Start(psi) ?? throw new ScriptException("Cannot start powershell.exe");

        // Nothing to read: an open stdin can make PowerShell wait for input.
        process.StandardInput.Close();

        var stdout = process.StandardOutput.ReadToEndAsync(ct);
        var stderr = process.StandardError.ReadToEndAsync(ct);
        await process.WaitForExitAsync(ct);
        if (process.ExitCode != 0)
        {
            throw new ScriptException((await stderr).Trim() is { Length: > 0 } err
                ? err : $"PowerShell exited with code {process.ExitCode}");
        }
        return (await stdout).Trim();
    }
}

/// <summary>
/// IIscsiInitiator on top of the built-in Windows iSCSI cmdlets. Every value
/// put into a script is validated by IscsiNames first.
/// </summary>
public sealed class WindowsIscsiInitiator(IScriptRunner runner) : IIscsiInitiator
{
    // Seconds to wait for Windows to show the new disk after a login.
    internal const int DiskWaitSeconds = 30;

    public async Task<string?> GetInitiatorNameAsync(CancellationToken ct)
    {
        var name = await runner.RunAsync(Scripts.GetInitiatorName(), ct);
        return name.Length == 0 ? null : name.ToLowerInvariant();
    }

    public Task SetInitiatorNameAsync(string currentIqn, string newIqn, CancellationToken ct) =>
        runner.RunAsync(Scripts.SetInitiatorName(currentIqn, newIqn), ct);

    public async Task<bool> IsConnectedAsync(string targetIqn, CancellationToken ct) =>
        await runner.RunAsync(Scripts.IsConnected(targetIqn), ct) == "True";

    public async Task<string> ConnectAsync(string portalIp, int portalPort, string targetIqn, string driveLetter, CancellationToken ct)
    {
        var letter = await runner.RunAsync(Scripts.Connect(portalIp, portalPort, targetIqn, driveLetter), ct);
        // The letter is the script's last output line.
        return letter.Split('\n')[^1].Trim();
    }

    public Task DisconnectAsync(string targetIqn, CancellationToken ct) =>
        runner.RunAsync(Scripts.Disconnect(targetIqn), ct);

    internal static class Scripts
    {
        public static string GetInitiatorName() =>
            "(Get-InitiatorPort | Where-Object ConnectionType -eq 'iSCSI' | Select-Object -First 1).NodeAddress";

        public static string SetInitiatorName(string currentIqn, string newIqn)
        {
            RequireIqn(currentIqn);
            RequireIqn(newIqn);
            return $"Set-InitiatorPort -NodeAddress '{currentIqn}' -NewNodeAddress '{newIqn}' | Out-Null";
        }

        public static string IsConnected(string targetIqn)
        {
            RequireIqn(targetIqn);
            return "[bool](Get-IscsiTarget | Where-Object { " +
                   $"$_.NodeAddress -eq '{targetIqn}' -and $_.IsConnected }})";
        }

        public static string Connect(string portalIp, int portalPort, string targetIqn, string driveLetter)
        {
            RequireIqn(targetIqn);
            if (!IscsiNames.IsPortalIp(portalIp)) throw new ArgumentException($"Invalid portal IP: {portalIp}");
            if (!IscsiNames.IsPort(portalPort)) throw new ArgumentException($"Invalid portal port: {portalPort}");
            if (!IscsiNames.IsDriveLetter(driveLetter)) throw new ArgumentException($"Invalid drive letter: {driveLetter}");

            return $$"""
                Set-Service MSiSCSI -StartupType Automatic
                Start-Service MSiSCSI
                if (Get-IscsiTargetPortal | Where-Object TargetPortalAddress -eq '{{portalIp}}') {
                    Update-IscsiTargetPortal -TargetPortalAddress '{{portalIp}}' | Out-Null
                } else {
                    New-IscsiTargetPortal -TargetPortalAddress '{{portalIp}}' -TargetPortalPortNumber {{portalPort}} | Out-Null
                }
                # Log in only once: after a service restart or a failed earlier
                # attempt the session can already exist, and a second login fails.
                $loggedIn = Get-IscsiTarget | Where-Object { $_.NodeAddress -eq '{{targetIqn}}' -and $_.IsConnected }
                if (-not $loggedIn) {
                    Connect-IscsiTarget -NodeAddress '{{targetIqn}}' -TargetPortalAddress '{{portalIp}}' -TargetPortalPortNumber {{portalPort}} -IsPersistent $false | Out-Null
                }

                # Windows needs a moment to show the disk after the login.
                $disk = $null
                for ($i = 0; $i -lt {{DiskWaitSeconds}} -and -not $disk; $i++) {
                    $disk = Get-IscsiSession | Where-Object TargetNodeAddress -eq '{{targetIqn}}' |
                        Get-Disk -ErrorAction SilentlyContinue | Select-Object -First 1
                    if (-not $disk) { Start-Sleep -Seconds 1 }
                }
                if (-not $disk) { throw "Connected to {{targetIqn}} but no disk appeared" }

                # The SAN policy keeps new shared disks offline/read-only.
                if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false }
                if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }

                # An empty disk (a new master) has no partitions; Get-Partition then errors.
                $part = Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue |
                    Where-Object { $_.Type -ne 'Reserved' -and $_.Type -ne 'System' } |
                    Sort-Object Size -Descending | Select-Object -First 1
                if (-not $part) {
                    throw "Disk $($disk.Number) has no data partition; initialize and format it in Disk Management"
                }

                # Prefer the configured letter. When another drive holds it (DVD,
                # second partition, USB stick), keep the letter Windows already gave
                # the partition, else take the first free one. Prints the letter used.
                $used = @(Get-CimInstance Win32_LogicalDisk | ForEach-Object { [string]$_.DeviceID[0] }) +
                    @(Get-Partition | Where-Object { $_.DriveLetter -match '^[A-Za-z]$' } |
                        ForEach-Object { [string]$_.DriveLetter })
                $mine = if ($part.DriveLetter -match '^[A-Za-z]$') { ([string]$part.DriveLetter).ToUpper() } else { '' }
                $letter = '{{driveLetter}}'
                if ($mine -ne $letter -and $used -contains $letter) {
                    $letter = $mine
                    if (-not $letter) {
                        $letter = [char[]]'{{LetterOrder(driveLetter)}}' | ForEach-Object { [string]$_ } |
                            Where-Object { $used -notcontains $_ } | Select-Object -First 1
                    }
                    if (-not $letter) { throw "No free drive letter for the game disk" }
                }
                if ($mine -ne $letter) {
                    Set-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber -NewDriveLetter $letter
                }
                $letter
                """;
        }

        /// <summary>
        /// Letters to try when the preferred one is taken: from the preferred
        /// letter to Z, then from D up to it (A-C are never used).
        /// </summary>
        public static string LetterOrder(string preferred)
        {
            var all = "DEFGHIJKLMNOPQRSTUVWXYZ";
            var i = all.IndexOf(preferred, StringComparison.Ordinal);
            return all[i..] + all[..i];
        }

        public static string Disconnect(string targetIqn)
        {
            RequireIqn(targetIqn);
            return $"Disconnect-IscsiTarget -NodeAddress '{targetIqn}' -Confirm:$false | Out-Null";
        }

        private static void RequireIqn(string iqn)
        {
            if (!IscsiNames.IsIqn(iqn)) throw new ArgumentException($"Invalid IQN: {iqn}");
        }
    }
}
