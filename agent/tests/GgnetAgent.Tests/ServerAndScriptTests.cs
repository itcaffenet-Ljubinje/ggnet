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
        Assert.Contains("$letter = 'D'", script);
        Assert.Contains("-NewDriveLetter $letter", script);
        Assert.Contains("Set-Disk -Number $disk.Number -IsOffline $false", script);
    }

    [Fact]
    public void Connect_script_falls_back_to_a_free_letter_when_the_preferred_is_taken()
    {
        var script = WindowsIscsiInitiator.Scripts.Connect("192.168.0.10", 3260, Target, "F");
        // Keep the letter Windows gave the partition, else try F..Z, then D, E.
        Assert.Contains("if ($mine -ne $letter -and $used -contains $letter) {", script);
        Assert.Contains("[char[]]'FGHIJKLMNOPQRSTUVWXYZDE'", script);
        Assert.Contains("throw \"No free drive letter for the game disk\"", script);
        // The letter used is the script's output.
        Assert.EndsWith("$letter", script.TrimEnd());
    }

    [Theory]
    [InlineData("D", "DEFGHIJKLMNOPQRSTUVWXYZ")]
    [InlineData("G", "GHIJKLMNOPQRSTUVWXYZDEF")]
    [InlineData("Z", "ZDEFGHIJKLMNOPQRSTUVWXY")]
    public void Letter_order_starts_at_the_preferred_letter_and_never_uses_a_to_c(string preferred, string order)
    {
        Assert.Equal(order, WindowsIscsiInitiator.Scripts.LetterOrder(preferred));
    }

    [Fact]
    public async Task Connect_returns_the_letter_from_the_last_output_line()
    {
        var iscsi = new WindowsIscsiInitiator(new RecordingRunner("some warning\r\nE"));
        Assert.Equal("E", await iscsi.ConnectAsync("192.168.0.10", 3260, Target, "D", CancellationToken.None));
    }

    [Fact]
    public void Connect_script_logs_in_only_when_not_already_logged_in()
    {
        // Found on a real Windows 11 PC: a second Connect-IscsiTarget fails with
        // "The target has already been logged in via an iSCSI session".
        var script = WindowsIscsiInitiator.Scripts.Connect("192.168.0.10", 3260, Target, "D");
        var guard = script.IndexOf($"$_.NodeAddress -eq '{Target}' -and $_.IsConnected", StringComparison.Ordinal);
        var login = script.IndexOf("Connect-IscsiTarget", StringComparison.Ordinal);
        Assert.True(guard >= 0 && guard < login);
        Assert.Contains("if (-not $loggedIn) {", script);
    }

    [Fact]
    public void Connect_script_filters_sessions_with_where_object()
    {
        // Get-IscsiSession has no -TargetNodeAddress parameter (real PC error).
        var script = WindowsIscsiInitiator.Scripts.Connect("192.168.0.10", 3260, Target, "D");
        Assert.DoesNotContain("Get-IscsiSession -", script);
        Assert.Contains($"Get-IscsiSession | Where-Object TargetNodeAddress -eq '{Target}'", script);
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
