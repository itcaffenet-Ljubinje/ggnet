using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class LocalDisksTests
{
    private readonly FakeHandler _server = new();
    private readonly FakeLocalDisks _disks = new();

    private AgentWorker Worker(bool hide = true) => new(
        _server.Client(),
        new FakeIscsi(),
        Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.10:8088", HideLocalDisks = hide }),
        NullLogger<AgentWorker>.Instance,
        TimeProvider.System,
        localDisks: _disks);

    private void BootMode() => _server.Body = ConfigJson.Replace("\"mode\": \"disk\"", "\"mode\": \"boot\"");

    [Fact]
    public async Task Boot_mode_takes_the_local_disks_offline_once()
    {
        BootMode();
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(1, _disks.Calls);
    }

    [Fact]
    public async Task Disk_mode_leaves_the_local_disks_alone()
    {
        await Worker().TickAsync(CancellationToken.None);   // the sample config is Disk Mode
        Assert.Equal(0, _disks.Calls);
    }

    [Fact]
    public async Task A_virtual_machine_keeps_its_disks_online()
    {
        // The server's VMs: the game writeback is the VM's own SATA disk.
        _server.Body = ConfigJson.Replace("\"mode\": \"disk\"", "\"mode\": \"vm\"");
        await Worker().TickAsync(CancellationToken.None);
        Assert.Equal(0, _disks.Calls);
    }

    [Fact]
    public async Task Nothing_is_hidden_when_the_setting_is_off()
    {
        BootMode();
        await Worker(hide: false).TickAsync(CancellationToken.None);
        Assert.Equal(0, _disks.Calls);
    }

    [Fact]
    public async Task Nothing_is_hidden_while_the_server_is_unreachable()
    {
        _server.Throw = new HttpRequestException("connection refused");
        await Worker().TickAsync(CancellationToken.None);
        Assert.Equal(0, _disks.Calls);
    }

    [Fact]
    public async Task A_failure_is_retried_on_the_next_heartbeat()
    {
        BootMode();
        _disks.Fail = new ScriptException("Set-Disk: access denied");
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        _disks.Fail = null;
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(2, _disks.Calls);
        Assert.Equal(3, _server.Requests.Count);   // heartbeats go on regardless
    }

    [Fact]
    public void The_script_takes_only_internal_disks_offline()
    {
        var script = WindowsLocalDisks.Script;
        Assert.Contains("Set-Disk -Number $_.Number -IsOffline $true", script);
        Assert.Contains("-not $_.IsBoot -and -not $_.IsSystem", script);
        Assert.Contains("'NVMe'", script);
        Assert.DoesNotContain("iSCSI", script);   // the server's disks
        Assert.DoesNotContain("USB", script);     // a player's stick
    }

    [Fact]
    public async Task Reports_one_line_per_disk_taken_offline()
    {
        var runner = new RecordingRunner("0: KINGSTON SNV2S500G (NVMe)\r\n1: ST2000DM008 (SATA)\r\n");
        var hidden = await new WindowsLocalDisks(runner).HideAsync(CancellationToken.None);
        Assert.Equal(["0: KINGSTON SNV2S500G (NVMe)", "1: ST2000DM008 (SATA)"], hidden);
    }
}
