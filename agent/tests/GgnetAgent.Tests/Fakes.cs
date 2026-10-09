using System.Net;
using System.Text;

namespace GgnetAgent.Tests;

internal static class Samples
{
    public const string Target = "iqn.2025-05.net.ggnet:client-pc01";
    public const string ExpectedIqn = "iqn.1991-05.com.microsoft:pc01";

    public static AgentConfig Config(string? target = Target, string status = "provisioned") => new(
        MachineId: 1, Name: "pc01", Mode: "disk", Status: status,
        InitiatorIqn: ExpectedIqn, IscsiTargetIqn: target,
        PortalIp: "192.168.0.10", PortalPort: 3260, GameDisk: target is null ? null : "cs2");

    public const string ConfigJson = """
        {"machine_id": 1, "name": "pc01", "mode": "disk", "status": "provisioned",
         "initiator_iqn": "iqn.1991-05.com.microsoft:pc01",
         "iscsi_target_iqn": "iqn.2025-05.net.ggnet:client-pc01",
         "portal_ip": "192.168.0.10", "portal_port": 3260, "game_disk": "cs2"}
        """;
}

/// <summary>HttpMessageHandler that answers every request with a fixed response (or throws).</summary>
internal sealed class FakeHandler : HttpMessageHandler
{
    public HttpStatusCode Status { get; set; } = HttpStatusCode.OK;
    public string Body { get; set; } = Samples.ConfigJson;
    public Exception? Throw { get; set; }
    public List<(string Path, string Body)> Requests { get; } = [];

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        Requests.Add((request.RequestUri!.AbsolutePath,
            request.Content is null ? "" : await request.Content.ReadAsStringAsync(ct)));
        if (Throw is not null) throw Throw;
        return new HttpResponseMessage(Status) { Content = new StringContent(Body, Encoding.UTF8, "application/json") };
    }

    public ServerClient Client() =>
        new(new HttpClient(this) { BaseAddress = new Uri("http://192.168.0.10:8088/") });
}

/// <summary>In-memory iSCSI initiator that records what the agent asked for.</summary>
internal sealed class FakeIscsi : IIscsiInitiator
{
    public string? InitiatorName { get; set; } = Samples.ExpectedIqn;
    public HashSet<string> Connected { get; } = [];
    public List<string> Calls { get; } = [];
    public Exception? FailConnect { get; set; }
    /// <summary>The letter the disk "gets"; null means the requested one.</summary>
    public string? GivenLetter { get; set; }

    public Task<string?> GetInitiatorNameAsync(CancellationToken ct) => Task.FromResult(InitiatorName);

    public Task SetInitiatorNameAsync(string currentIqn, string newIqn, CancellationToken ct)
    {
        Calls.Add($"rename {currentIqn} -> {newIqn}");
        InitiatorName = newIqn;
        return Task.CompletedTask;
    }

    public Task<bool> IsConnectedAsync(string targetIqn, CancellationToken ct) =>
        Task.FromResult(Connected.Contains(targetIqn));

    public Task<string> ConnectAsync(string portalIp, int portalPort, string targetIqn, string driveLetter, CancellationToken ct)
    {
        Calls.Add($"connect {targetIqn} {portalIp}:{portalPort} {driveLetter}:");
        if (FailConnect is not null) throw FailConnect;
        Connected.Add(targetIqn);
        return Task.FromResult(GivenLetter ?? driveLetter);
    }

    public Task DisconnectAsync(string targetIqn, CancellationToken ct)
    {
        Calls.Add($"disconnect {targetIqn}");
        Connected.Remove(targetIqn);
        return Task.CompletedTask;
    }
}

/// <summary>Runs nothing; returns a canned answer and keeps the scripts.</summary>
internal sealed class RecordingRunner(string output = "") : IScriptRunner
{
    public List<string> Scripts { get; } = [];

    public Task<string> RunAsync(string script, CancellationToken ct)
    {
        Scripts.Add(script);
        return Task.FromResult(output);
    }
}

/// <summary>Returns a fixed inventory and counts the reads.</summary>
internal sealed class FakeInventory : IInventory
{
    public Inventory? Next { get; set; } = new("192.168.0.21", "AA-BB-CC-DD-EE-01", 1000, "Intel I219-V",
        "AMD Ryzen 5 5600X", ["NVIDIA GeForce RTX 3060"], "ASUS TUF B550", 16L << 30);
    public List<string?> Reads { get; } = [];

    public Task<Inventory?> ReadAsync(string? serverIp, CancellationToken ct)
    {
        Reads.Add(serverIp);
        return Task.FromResult(Next);
    }
}
