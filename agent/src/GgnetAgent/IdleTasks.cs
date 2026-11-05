namespace GgnetAgent;

/// <summary>
/// Boot Mode: Windows' scheduled tasks that scan or tidy up the PC right after
/// it starts (compatibility appraiser, telemetry, defragmentation, disk
/// diagnostics…). Their work is thrown away with the writeback at every
/// restart, so on a diskless PC they only slow the start and fill the
/// writeback (CompatTelRunner alone reads 20 MB/s for minutes). The agent
/// turns them off once per start, in this Windows (the writeback), not in the
/// image; whatever already runs is stopped.
/// </summary>
public interface IIdleTasks
{
    /// <summary>Turns the tasks off; returns a line per task it turned off.</summary>
    Task<IReadOnlyList<string>> DisableAsync(CancellationToken ct);
}

/// <summary>IIdleTasks with the ScheduledTasks module (in Windows 11).</summary>
public sealed class WindowsIdleTasks(IScriptRunner runner) : IIdleTasks
{
    /// <summary>Task paths under \Microsoft\Windows\; ones a Windows does not have are skipped.</summary>
    internal static readonly string[] Tasks =
    [
        @"Application Experience\Microsoft Compatibility Appraiser",
        @"Application Experience\Microsoft Compatibility Appraiser Exp",
        @"Application Experience\ProgramDataUpdater",
        @"Application Experience\StartupAppTask",
        @"Application Experience\PcaPatchDbTask",
        @"Customer Experience Improvement Program\Consolidator",
        @"Customer Experience Improvement Program\UsbCeip",
        @"Defrag\ScheduledDefrag",
        @"Maintenance\WinSAT",
        @"DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector",
        @"Windows Error Reporting\QueueReporting",
        @"Feedback\Siuf\DmClient",
        @"Feedback\Siuf\DmClientOnScenarioDownload",
        @"Power Efficiency Diagnostics\AnalyzeSystem",
    ];

    internal static string Script => $$"""
        $tasks = @({{string.Join(", ", Tasks.Select(t => $"'{t}'"))}})
        foreach ($t in $tasks) {
            $i = $t.LastIndexOf('\')
            $task = Get-ScheduledTask -TaskPath "\Microsoft\Windows\$($t.Substring(0, $i))\" `
                -TaskName $t.Substring($i + 1) -ErrorAction SilentlyContinue
            if ($task -and $task.State -ne 'Disabled') {
                $task | Stop-ScheduledTask -ErrorAction SilentlyContinue
                $task | Disable-ScheduledTask -ErrorAction SilentlyContinue | Out-Null
                $t
            }
        }
        Get-Process CompatTelRunner -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        """;

    public async Task<IReadOnlyList<string>> DisableAsync(CancellationToken ct) =>
        (await runner.RunAsync(Script, ct))
            .Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
}
