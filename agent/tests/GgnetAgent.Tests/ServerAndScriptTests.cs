using System.Net;
using System.Text.Json;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class ServerClientTests
{
    [Fact]
    public async Task Sends_snake_case_heartbeat_and_parses_the_config()
    {
        var handler = new FakeHandler();
        var config = await handler.Client().HeartbeatAsync(
            new Heartbeat("pc01", "0.1.0", ExpectedIqn, true), CancellationToken.None);

        Assert.Equal(Config(), config);
        var (path, body) = Assert.Single(handler.Requests);
        Assert.Equal("/api/v1/agent/heartbeat", path);
        using var json = JsonDocument.Parse(body);
        Assert.Equal("pc01", json.RootElement.GetProperty("name").GetString());
        Assert.Equal("0.1.0", json.RootElement.GetProperty("agent_version").GetString());
        Assert.Equal(ExpectedIqn, json.RootElement.GetProperty("initiator_iqn").GetString());
        Assert.True(json.RootElement.GetProperty("iscsi_connected").GetBoolean());
    }

    [Fact]
    public async Task Explains_an_unregistered_machine()
    {
        var handler = new FakeHandler { Status = HttpStatusCode.NotFound, Body = """{"detail":{"error":"x"}}""" };
        var e = await Assert.ThrowsAsync<ServerException>(() =>
            handler.Client().HeartbeatAsync(new Heartbeat("pc01", "0.1.0", null, false), CancellationToken.None));
        Assert.Contains("not registered", e.Message);
    }

    [Fact]
    public async Task Reports_other_http_errors_with_the_body()
    {
        var handler = new FakeHandler { Status = HttpStatusCode.UnprocessableEntity, Body = "bad name" };
        var e = await Assert.ThrowsAsync<ServerException>(() =>
            handler.Client().HeartbeatAsync(new Heartbeat("pc_01", "0.1.0", null, false), CancellationToken.None));
        Assert.Contains("422", e.Message);
        Assert.Contains("bad name", e.Message);
    }
}

public class ScriptTests
{
    [Fact]
    public void Connect_script_uses_the_validated_values()
    {
        var script = WindowsIscsiInitiator.Scripts.Connect("192.168.0.10", 3260, Target, "D");
        Assert.Contains($"Connect-IscsiTarget -NodeAddress '{Target}' -TargetPortalAddress '192.168.0.10' -TargetPortalPortNumber 3260 -IsPersistent $false", script);
        Assert.Contains("-NewDriveLetter 'D'", script);
        Assert.Contains("Set-Disk -Number $disk.Number -IsOffline $false", script);
    }

    [Theory]
    [InlineData("192.168.0.10", 3260, "iqn.2025-05.net.ggnet:client-pc01'; whoami; '", "D")]
    [InlineData("192.168.0.10'; whoami; '", 3260, Target, "D")]
    [InlineData("192.168.0.10", 70000, Target, "D")]
    [InlineData("192.168.0.10", 3260, Target, "C")]
    [InlineData("192.168.0.10", 3260, Target, "D'; whoami; '")]
    public void Connect_script_rejects_invalid_values(string ip, int port, string target, string drive)
    {
        Assert.Throws<ArgumentException>(() => WindowsIscsiInitiator.Scripts.Connect(ip, port, target, drive));
    }

    [Fact]
    public void Other_scripts_reject_invalid_iqns()
    {
        const string bad = "iqn.2025-05.x:y'; whoami; '";
        Assert.Throws<ArgumentException>(() => WindowsIscsiInitiator.Scripts.Disconnect(bad));
        Assert.Throws<ArgumentException>(() => WindowsIscsiInitiator.Scripts.IsConnected(bad));
        Assert.Throws<ArgumentException>(() => WindowsIscsiInitiator.Scripts.SetInitiatorName(ExpectedIqn, bad));
    }

    [Fact]
    public async Task Initiator_reads_and_lowercases_the_node_name()
    {
        var runner = new RecordingRunner("IQN.1991-05.com.microsoft:PC01");
        var iscsi = new WindowsIscsiInitiator(runner);
        Assert.Equal("iqn.1991-05.com.microsoft:pc01", await iscsi.GetInitiatorNameAsync(CancellationToken.None));
        Assert.Contains("Get-InitiatorPort", Assert.Single(runner.Scripts));
    }

    [Theory]
    [InlineData("True", true)]
    [InlineData("False", false)]
    public async Task IsConnected_parses_the_powershell_bool(string output, bool expected)
    {
        var iscsi = new WindowsIscsiInitiator(new RecordingRunner(output));
        Assert.Equal(expected, await iscsi.IsConnectedAsync(Target, CancellationToken.None));
    }
}

public class OptionsTests
{
    [Fact]
    public void Defaults_with_a_server_url_are_valid()
    {
        Assert.Empty(new AgentOptions { ServerUrl = "http://192.168.0.10:8088" }.Validate());
    }

    [Theory]
    [InlineData("", "D", 30)]
    [InlineData("192.168.0.10:8088", "D", 30)]
    [InlineData("ftp://192.168.0.10", "D", 30)]
    [InlineData("http://192.168.0.10:8088", "C", 30)]
    [InlineData("http://192.168.0.10:8088", "DE", 30)]
    [InlineData("http://192.168.0.10:8088", "D", 1)]
    public void Invalid_settings_are_reported(string url, string drive, int seconds)
    {
        var options = new AgentOptions { ServerUrl = url, DriveLetter = drive, HeartbeatSeconds = seconds };
        Assert.Single(options.Validate());
    }
}
