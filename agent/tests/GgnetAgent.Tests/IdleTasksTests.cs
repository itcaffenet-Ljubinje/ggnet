using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class IdleTasksTests
{
    private readonly FakeHandler _server = new();
    private readonly FakeIdleTasks _tasks = new();

    private AgentWorker Worker(bool disable = true) => new(
        _server.Client(),
        new FakeIscsi(),
        Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.10:8088", DisableIdleTasks = disable }),
        NullLogger<AgentWorker>.Instance,
        TimeProvider.System,
        idleTasks: _tasks);

    private void BootMode() => _server.Body = ConfigJson.Replace("\"mode\": \"disk\"", "\"mode\": \"boot\"");

    [Fact]
    public async Task Boot_mode_turns_the_tasks_off_once()
    {
        BootMode();
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(1, _tasks.Calls);
    }

    [Fact]
    public async Task Disk_mode_leaves_the_pcs_own_windows_alone()
    {
        await Worker().TickAsync(CancellationToken.None);
        Assert.Equal(0, _tasks.Calls);
    }

    [Fact]
    public async Task Nothing_is_turned_off_when_the_setting_is_off()
    {
        BootMode();
        await Worker(disable: false).TickAsync(CancellationToken.None);
        Assert.Equal(0, _tasks.Calls);
    }

    [Fact]
    public async Task A_failure_is_retried_on_the_next_heartbeat()
    {
        BootMode();
        _tasks.Fail = new ScriptException("access denied");
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        _tasks.Fail = null;
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(2, _tasks.Calls);
    }

    [Fact]
    public void The_script_stops_and_disables_the_listed_tasks_only()
    {
        var script = WindowsIdleTasks.Script;
        Assert.Contains(@"'Application Experience\Microsoft Compatibility Appraiser'", script);
        Assert.Contains("Disable-ScheduledTask", script);
        Assert.Contains("Stop-ScheduledTask", script);
        Assert.Contains("Stop-Process", script);
        Assert.DoesNotContain("UpdateOrchestrator", script);    // "Check for updates" keeps working
        Assert.DoesNotContain("Defender", script);
    }
}
