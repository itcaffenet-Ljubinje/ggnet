using System.Net;
using System.Security.Cryptography;
using Microsoft.Extensions.Logging.Abstractions;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

/// <summary>Serves the agent package's files by name; counts the requests.</summary>
internal sealed class PackageHandler : HttpMessageHandler
{
    public Dictionary<string, byte[]> Files { get; } = new()
    {
        ["ggnet-agent.exe"] = [0x4d, 0x5a, 1, 2, 3],
        ["install.ps1"] = "param([switch]$Update)\n"u8.ToArray(),
        ["uninstall.ps1"] = "# remove\n"u8.ToArray(),
    };
    public List<string> Requests { get; } = [];
    /// <summary>The heartbeat answer, when set.</summary>
    public AgentConfig? Heartbeat { get; set; }

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        var path = request.RequestUri!.AbsolutePath;
        Requests.Add(path);
        if (path.EndsWith("/heartbeat", StringComparison.Ordinal) && Heartbeat is not null)
        {
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(System.Text.Json.JsonSerializer.Serialize(Heartbeat, Protocol.Json),
                    System.Text.Encoding.UTF8, "application/json"),
            });
        }
        var name = Uri.UnescapeDataString(path[(path.LastIndexOf('/') + 1)..]);
        return Task.FromResult(Files.TryGetValue(name, out var data)
            ? new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(data) }
            : new HttpResponseMessage(HttpStatusCode.NotFound));
    }

    public ServerClient Client() =>
        new(new HttpClient(this) { BaseAddress = new Uri("http://192.168.0.10:8088/") });

    public AgentUpdate Offer(string version = "0.2.0") => new(version,
        [.. Files.Select(f => new UpdateFile(f.Key, f.Value.Length, Convert.ToHexStringLower(SHA256.HashData(f.Value))))]);
}

internal sealed class StepClock(DateTimeOffset now) : TimeProvider
{
    public override DateTimeOffset GetUtcNow() => now;
    public void Advance(TimeSpan by) => now += by;
}

internal sealed class FakeLauncher : IUpdateLauncher
{
    public List<(string Script, string Log)> Launches { get; } = [];

    public void Launch(string installScript, string logFile) => Launches.Add((installScript, logFile));
}

public sealed class AgentUpdaterTests : IDisposable
{
    private readonly PackageHandler _server = new();
    private readonly FakeLauncher _launcher = new();
    private readonly StepClock _time = new(DateTimeOffset.Parse("2026-11-08T20:00:00Z"));
    private readonly string _root = Path.Combine(Path.GetTempPath(), "ggnet-update-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        if (Directory.Exists(_root)) Directory.Delete(_root, recursive: true);
    }

    private AgentUpdater Updater() =>
        new(_server.Client(), _launcher, NullLogger<AgentUpdater>.Instance, _time, _root);

    private static AgentConfig With(AgentUpdate? update, string mode = "disk") =>
        Config() with { Mode = mode, Update = update };

    [Fact]
    public async Task Downloads_checks_and_starts_install_ps1_from_the_version_folder()
    {
        var updater = Updater();
        Assert.True(await updater.TryUpdateAsync(With(_server.Offer()), "0.1.21", CancellationToken.None));

        var folder = Path.Combine(_root, "0.2.0");
        foreach (var (name, data) in _server.Files)
        {
            Assert.Equal(data, File.ReadAllBytes(Path.Combine(folder, name)));
        }
        Assert.Empty(Directory.GetFiles(folder, "*.part"));
        Assert.Equal([(Path.Combine(folder, "install.ps1"), Path.Combine(folder, "update.log"))], _launcher.Launches);
        Assert.True(updater.Started);

        // Once started, nothing more: the service is about to be replaced.
        Assert.False(await updater.TryUpdateAsync(With(_server.Offer()), "0.1.21", CancellationToken.None));
        Assert.Single(_launcher.Launches);
    }

    [Theory]
    [InlineData("0.2.0")]          // the same
    [InlineData("0.3.0")]          // newer than offered: never a downgrade
    [InlineData("dev")]            // unreadable: leave it alone
    public async Task Only_an_older_agent_updates(string current)
    {
        Assert.False(await Updater().TryUpdateAsync(With(_server.Offer()), current, CancellationToken.None));
        Assert.Empty(_server.Requests);
        Assert.Empty(_launcher.Launches);
    }

    [Fact]
    public async Task Never_in_boot_mode_or_without_an_offer()
    {
        var updater = Updater();
        Assert.False(await updater.TryUpdateAsync(With(_server.Offer(), mode: "boot"), "0.1.0", CancellationToken.None));
        Assert.False(await updater.TryUpdateAsync(With(null), "0.1.0", CancellationToken.None));
        Assert.False(await updater.TryUpdateAsync(null, "0.1.0", CancellationToken.None));
        Assert.Empty(_server.Requests);
    }

    [Fact]
    public async Task A_damaged_download_is_not_installed_and_retried_an_hour_later()
    {
        var offer = _server.Offer();
        _server.Files["ggnet-agent.exe"] = [0x4d, 0x5a, 9, 9, 9];   // same size, other bytes
        var updater = Updater();

        Assert.False(await updater.TryUpdateAsync(With(offer), "0.1.21", CancellationToken.None));
        Assert.Empty(_launcher.Launches);
        Assert.False(File.Exists(Path.Combine(_root, "0.2.0", "ggnet-agent.exe")));

        var asked = _server.Requests.Count;
        _time.Advance(TimeSpan.FromMinutes(59));
        Assert.False(await updater.TryUpdateAsync(With(offer), "0.1.21", CancellationToken.None));
        Assert.Equal(asked, _server.Requests.Count);    // not yet

        _server.Files["ggnet-agent.exe"] = [0x4d, 0x5a, 1, 2, 3];
        _time.Advance(TimeSpan.FromMinutes(1));
        Assert.True(await updater.TryUpdateAsync(With(offer), "0.1.21", CancellationToken.None));
    }

    [Fact]
    public async Task A_newer_offer_is_tried_at_once_after_a_failure()
    {
        var updater = Updater();
        var bad = _server.Offer() with { Files = [.. _server.Offer().Files.Select(f => f with { SizeBytes = 1 })] };
        Assert.False(await updater.TryUpdateAsync(With(bad), "0.1.21", CancellationToken.None));
        Assert.True(await updater.TryUpdateAsync(With(_server.Offer("0.2.1")), "0.1.21", CancellationToken.None));
    }

    [Theory]
    [InlineData("..\\evil.exe")]
    [InlineData("appsettings.json")]
    public async Task Only_the_package_files_are_fetched(string name)
    {
        var offer = _server.Offer();
        offer = offer with { Files = [.. offer.Files, new UpdateFile(name, 1, new string('a', 64))] };
        Assert.False(await Updater().TryUpdateAsync(With(offer), "0.1.21", CancellationToken.None));
        Assert.Empty(_server.Requests);
    }

    [Fact]
    public async Task An_offer_without_install_ps1_is_refused()
    {
        var offer = _server.Offer();
        offer = offer with { Files = [.. offer.Files.Where(f => f.Name != "install.ps1")] };
        Assert.False(await Updater().TryUpdateAsync(With(offer), "0.1.21", CancellationToken.None));
        Assert.Empty(_server.Requests);
    }

    [Theory]
    [InlineData("0.1.21", "0.1.20", true)]
    [InlineData("0.1.10", "0.1.9", true)]
    [InlineData("v0.2.0", "0.1.99", true)]
    [InlineData("0.2.0", "0.2.0-rc1", true)]
    [InlineData("0.2.0-rc2", "0.2.0-rc1", true)]
    [InlineData("0.2.0-rc1", "0.2.0", false)]
    [InlineData("0.1.21", "0.1.21", false)]
    [InlineData("0.1.21", null, false)]
    [InlineData("x", "0.1.0", false)]
    public void Versions_compare_like_the_server(string candidate, string? current, bool newer)
    {
        Assert.Equal(newer, Versions.IsNewer(candidate, current));
    }

    [Fact]
    public void The_launcher_quotes_its_paths()
    {
        Assert.Equal(@"& 'C:\Program Files\ggnet-agent\update\0.2.0\install.ps1' -Update *> 'C:\it''s\update.log'",
            PowerShellUpdateLauncher.Command(@"C:\Program Files\ggnet-agent\update\0.2.0\install.ps1", @"C:\it's\update.log"));
    }

    [Fact]
    public void The_heartbeat_answer_carries_the_offer()
    {
        var json = ConfigJson.TrimEnd().TrimEnd('}') + """
            , "update": {"version": "0.2.0", "files": [{"name": "install.ps1", "size_bytes": 3, "sha256": "ab"}]}}
            """;
        var config = System.Text.Json.JsonSerializer.Deserialize<AgentConfig>(json, Protocol.Json)!;
        Assert.Equal(new UpdateFile("install.ps1", 3, "ab"), Assert.Single(config.Update!.Files));
        Assert.Equal("0.2.0", config.Update.Version);
    }
}

public sealed class AgentWorkerUpdateTests : IDisposable
{
    private readonly PackageHandler _server = new();
    private readonly FakeLauncher _launcher = new();
    private readonly FakePower _power = new();
    private readonly string _root = Path.Combine(Path.GetTempPath(), "ggnet-update-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        if (Directory.Exists(_root)) Directory.Delete(_root, recursive: true);
    }

    private AgentWorker Worker()
    {
        var client = _server.Client();
        var updater = new AgentUpdater(client, _launcher, NullLogger<AgentUpdater>.Instance, TimeProvider.System, _root);
        return new AgentWorker(client, new FakeIscsi(),
            Microsoft.Extensions.Options.Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.10:8088" }),
            NullLogger<AgentWorker>.Instance, TimeProvider.System, () => TimeSpan.FromHours(1),
            power: _power, updater: updater);
    }

    [Fact]
    public async Task Updates_after_connecting_the_game_disk()
    {
        _server.Heartbeat = Config() with { Update = _server.Offer("999.0.0") };
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);

        Assert.Equal(Target, worker.ConnectedTarget);
        Assert.Single(_launcher.Launches);
    }

    [Fact]
    public async Task A_shutdown_or_reboot_goes_first()
    {
        _server.Heartbeat = Config() with { Update = _server.Offer("999.0.0"), Command = "reboot" };
        await Worker().TickAsync(CancellationToken.None);

        Assert.Equal(["reboot"], _power.Commands);
        Assert.Empty(_launcher.Launches);
    }
}
