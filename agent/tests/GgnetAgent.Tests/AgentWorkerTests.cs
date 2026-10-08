using System.Net;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class AgentWorkerTests
{
    private readonly FakeHandler _server = new();
    private readonly FakeIscsi _iscsi = new();

    private AgentWorker Worker(bool manageInitiatorName = true) => new(
        _server.Client(),
        _iscsi,
        Options.Create(new AgentOptions
        {
            ServerUrl = "http://192.168.0.10:8088",
            ManageInitiatorName = manageInitiatorName,
        }),
        NullLogger<AgentWorker>.Instance,
        TimeProvider.System);

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
    public async Task Stop_disconnects_the_game_disk()
    {
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.StopAsync(CancellationToken.None);
        Assert.Equal($"disconnect {Target}", _iscsi.Calls[^1]);
        Assert.Null(worker.ConnectedTarget);
    }
}
