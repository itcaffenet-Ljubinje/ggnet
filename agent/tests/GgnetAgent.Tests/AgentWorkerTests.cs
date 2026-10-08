using System.Net;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class AgentWorkerTests
{
    private readonly FakeHandler _server = new();
    private readonly FakeIscsi _iscsi = new();

    private TimeSpan _uptime = TimeSpan.FromHours(2);

    private AgentWorker Worker(TimeProvider clock) => Worker(true, clock);

    private AgentWorker Worker(bool manageInitiatorName = true, TimeProvider? clock = null) => new(
        _server.Client(),
        _iscsi,
        Options.Create(new AgentOptions
        {
            ServerUrl = "http://192.168.0.10:8088",
            ManageInitiatorName = manageInitiatorName,
        }),
        NullLogger<AgentWorker>.Instance,
        clock ?? TimeProvider.System,
        () => _uptime);

    [Fact]
    public async Task Connects_the_game_disk_and_reports_it_on_the_next_heartbeat()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        Assert.Equal([$"connect {Target} 192.168.0.10:3260 D:"], _iscsi.Calls);
        Assert.Equal(Target, worker.ConnectedTarget);
        Assert.Contains("\"iscsi_connected\":false", _server.Requests[0].Body);

        await worker.TickAsync(CancellationToken.None);
        Assert.Single(_iscsi.Calls);   // already connected, nothing to do
        Assert.Contains("\"iscsi_connected\":true", _server.Requests[1].Body);
    }

    [Fact]
    public async Task Sends_the_same_boot_time_on_every_heartbeat()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);

        string BootedAt(int i) =>
            System.Text.Json.JsonDocument.Parse(_server.Requests[i].Body)
                .RootElement.GetProperty("booted_at").GetString()!;
        Assert.False(string.IsNullOrEmpty(BootedAt(0)));
        Assert.Equal(BootedAt(0), BootedAt(1));
    }

    [Fact]
    public async Task Reports_the_windows_boot_time_so_a_service_restart_is_not_a_reboot()
    {
        var clock = new ManualClock(new DateTimeOffset(2026, 10, 8, 20, 0, 0, TimeSpan.Zero));
        _uptime = TimeSpan.FromHours(2);
        await Worker(clock).TickAsync(CancellationToken.None);

        // The service restarts 10 minutes later in the same Windows boot.
        clock.Now += TimeSpan.FromMinutes(10);
        _uptime += TimeSpan.FromMinutes(10);
        await Worker(clock).TickAsync(CancellationToken.None);

        // Windows reboots an hour later.
        clock.Now += TimeSpan.FromHours(1);
        _uptime = TimeSpan.FromMinutes(1);
        await Worker(clock).TickAsync(CancellationToken.None);

        DateTimeOffset BootedAt(int i) =>
            System.Text.Json.JsonDocument.Parse(_server.Requests[i].Body)
                .RootElement.GetProperty("booted_at").GetDateTimeOffset();
        var boot = new DateTimeOffset(2026, 10, 8, 18, 0, 0, TimeSpan.Zero);
        Assert.Equal(boot, BootedAt(0));
        Assert.Equal(boot, BootedAt(1));
        Assert.Equal(new DateTimeOffset(2026, 10, 8, 21, 9, 0, TimeSpan.Zero), BootedAt(2));
    }

    private sealed class ManualClock(DateTimeOffset now) : TimeProvider
    {
        public DateTimeOffset Now { get; set; } = now;
        public override DateTimeOffset GetUtcNow() => Now;
    }

    [Fact]
    public async Task Disconnects_when_the_disk_is_removed_in_the_ui()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        _server.Body = Samples.ConfigJson.Replace($"\"{Target}\"", "null");
        await worker.TickAsync(CancellationToken.None);

        Assert.Equal($"disconnect {Target}", _iscsi.Calls[^1]);
        Assert.Null(worker.ConnectedTarget);
    }

    [Fact]
    public async Task Reconnects_after_the_session_drops()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        _iscsi.Connected.Clear();   // e.g. the server was reset

        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(2, _iscsi.Calls.Count(c => c.StartsWith("connect ")));
        Assert.Equal(Target, worker.ConnectedTarget);
    }

    [Fact]
    public async Task Keeps_the_disk_when_the_server_is_unreachable()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        _server.Throw = new HttpRequestException("connection refused");
        await worker.TickAsync(CancellationToken.None);

        Assert.DoesNotContain(_iscsi.Calls, c => c.StartsWith("disconnect"));
        Assert.Equal(Target, worker.ConnectedTarget);
    }

    [Fact]
    public async Task Keeps_the_disk_when_the_heartbeat_times_out()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        _server.Throw = new TaskCanceledException("HttpClient.Timeout elapsed");
        await worker.TickAsync(CancellationToken.None);

        Assert.DoesNotContain(_iscsi.Calls, c => c.StartsWith("disconnect"));
        Assert.Equal(Target, worker.ConnectedTarget);
    }

    [Fact]
    public async Task Stays_connected_when_the_disk_gets_another_letter()
    {
        _iscsi.GivenLetter = "E";   // D: is a DVD drive on this PC
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);

        Assert.Equal(Target, worker.ConnectedTarget);
        Assert.Single(_iscsi.Calls, c => c.StartsWith("connect "));
    }

    [Fact]
    public async Task Moves_the_disk_when_the_admin_changes_the_letter_and_reports_it()
    {
        _server.Body = ConfigJson.Replace("\"game_disk\": \"cs2\"", "\"game_disk\": \"cs2\", \"drive_letter\": \"G\"");
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal($"connect {Target} 192.168.0.10:3260 G:", _iscsi.Calls[^1]);

        await worker.TickAsync(CancellationToken.None);   // same letter: nothing to do
        Assert.Single(_iscsi.Calls, c => c.StartsWith("connect "));
        Assert.Contains("\"drive_letter\":\"G\"", _server.Requests[^1].Body);

        _server.Body = _server.Body.Replace("\"drive_letter\": \"G\"", "\"drive_letter\": \"E\"");
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal($"connect {Target} 192.168.0.10:3260 E:", _iscsi.Calls[^1]);
        Assert.DoesNotContain(_iscsi.Calls, c => c.StartsWith("disconnect"));
        Assert.Equal("E", worker.DriveLetter);
    }

    [Fact]
    public async Task Keeps_the_disk_when_the_server_answers_with_an_error()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        _server.Status = HttpStatusCode.InternalServerError;
        _server.Body = "boom";
        await worker.TickAsync(CancellationToken.None);   // logged, not thrown

        Assert.DoesNotContain(_iscsi.Calls, c => c.StartsWith("disconnect"));
        Assert.Equal(Target, worker.ConnectedTarget);
    }

    [Fact]
    public async Task Reconnects_once_the_server_is_back_after_a_dropped_session()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        // Server (and so the target) restarts: the session drops and heartbeats fail.
        _server.Throw = new HttpRequestException("connection refused");
        _iscsi.Connected.Clear();
        await worker.TickAsync(CancellationToken.None);
        Assert.Null(worker.ConnectedTarget);
        Assert.Single(_iscsi.Calls, c => c.StartsWith("connect "));

        _server.Throw = null;
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(2, _iscsi.Calls.Count(c => c.StartsWith("connect ")));
        Assert.Equal(Target, worker.ConnectedTarget);
        Assert.Contains("\"iscsi_connected\":false", _server.Requests[^1].Body);
    }

    [Fact]
    public async Task Does_nothing_for_an_unregistered_machine()
    {
        _server.Status = HttpStatusCode.NotFound;
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);   // logged, not thrown
        Assert.Empty(_iscsi.Calls);
    }

    [Fact]
    public async Task Renames_a_domain_initiator_to_the_expected_iqn()
    {
        _iscsi.InitiatorName = "iqn.1991-05.com.microsoft:pc01.cafe.local";
        await Worker().TickAsync(CancellationToken.None);
        Assert.Equal(
            [$"rename iqn.1991-05.com.microsoft:pc01.cafe.local -> {ExpectedIqn}",
             $"connect {Target} 192.168.0.10:3260 D:"],
            _iscsi.Calls);
    }

    [Fact]
    public async Task A_failed_connect_is_retried_on_the_next_heartbeat()
    {
        _iscsi.FailConnect = new ScriptException("Connected but no disk appeared");
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        Assert.Null(worker.ConnectedTarget);

        _iscsi.FailConnect = null;
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(Target, worker.ConnectedTarget);
    }

    [Fact]
    public async Task Stop_keeps_the_game_disk_connected()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.StopAsync(CancellationToken.None);
        Assert.DoesNotContain(_iscsi.Calls, c => c.StartsWith("disconnect"));
        Assert.Contains(Target, _iscsi.Connected);
    }
}

public class AgentWorkerDiTests
{
    [Fact]
    public void The_host_can_build_the_worker_without_an_uptime_registration()
    {
        var services = new ServiceCollection()
            .AddSingleton(new FakeHandler().Client())
            .AddSingleton<IIscsiInitiator>(new FakeIscsi())
            .AddSingleton(Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.10:8088" }))
            .AddSingleton<ILogger<AgentWorker>>(NullLogger<AgentWorker>.Instance)
            .AddSingleton(TimeProvider.System)
            .AddSingleton<AgentWorker>();

        using var provider = services.BuildServiceProvider();
        Assert.NotNull(provider.GetRequiredService<AgentWorker>());
    }
}
