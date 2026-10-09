using System.Text.Json;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;

namespace GgnetAgent.Tests;

public class InventoryTests
{
    [Fact]
    public void Parses_the_script_output_and_trims_it()
    {
        const string json = """
            {"ip_address":"192.168.0.21","mac_address":"AA-BB-CC-DD-EE-01","link_speed_mbps":1000,
             "nic":"Intel(R) Ethernet I219-V","cpu":"  AMD Ryzen 5 5600X 6-Core Processor  ",
             "gpus":["NVIDIA GeForce RTX 3060",""],"motherboard":" ASUSTeK TUF GAMING B550-PLUS ",
             "memory_bytes":17179869184}
            """;
        var inv = WindowsInventory.Parse(json)!;
        Assert.Equal("192.168.0.21", inv.IpAddress);
        Assert.Equal(1000, inv.LinkSpeedMbps);
        Assert.Equal("AMD Ryzen 5 5600X 6-Core Processor", inv.Cpu);
        Assert.Equal(["NVIDIA GeForce RTX 3060"], inv.Gpus);
        Assert.Equal("ASUSTeK TUF GAMING B550-PLUS", inv.Motherboard);
        Assert.Equal(16L << 30, inv.MemoryBytes);
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    public void Unreadable_output_is_no_inventory(string output)
    {
        Assert.Null(WindowsInventory.Parse(output));
    }

    [Fact]
    public void Script_embeds_only_a_plain_server_ip()
    {
        Assert.Contains("Find-NetRoute -RemoteIPAddress '192.168.0.40'", WindowsInventory.Script("192.168.0.40"));
        foreach (var bad in new[] { "ggnet.local", "1.2.3.4'; whoami; '", null })
        {
            var script = WindowsInventory.Script(bad);
            Assert.DoesNotContain("Find-NetRoute", script);
            Assert.Contains("$addr = $null", script);
        }
    }

    [Fact]
    public void Script_outputs_compressed_json_with_the_protocol_names()
    {
        var script = WindowsInventory.Script("192.168.0.40");
        foreach (var name in new[] { "ip_address", "mac_address", "link_speed_mbps", "nic", "cpu", "gpus",
                                     "motherboard", "memory_bytes" })
        {
            Assert.Contains($"{name} = ", script);
        }
        Assert.EndsWith("ConvertTo-Json -Compress", script.TrimEnd());
    }
}

public class InventoryWorkerTests
{
    private readonly FakeHandler _server = new();
    private readonly FakeInventory _inventory = new();

    private sealed class ManualClock(DateTimeOffset now) : TimeProvider
    {
        public DateTimeOffset Now { get; set; } = now;
        public override DateTimeOffset GetUtcNow() => Now;
    }

    private AgentWorker Worker(TimeProvider clock) => new(
        _server.Client(), new FakeIscsi(),
        Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.40:8088" }),
        NullLogger<AgentWorker>.Instance, clock, () => TimeSpan.FromHours(1), _inventory);

    [Fact]
    public async Task Sends_the_inventory_and_reads_it_again_only_every_ten_minutes()
    {
        var clock = new ManualClock(new DateTimeOffset(2026, 10, 9, 12, 0, 0, TimeSpan.Zero));
        var worker = Worker(clock);

        await worker.TickAsync(CancellationToken.None);
        clock.Now += TimeSpan.FromMinutes(5);
        await worker.TickAsync(CancellationToken.None);
        clock.Now += AgentWorker.InventoryEvery;
        await worker.TickAsync(CancellationToken.None);

        // Before the first answer: the server URL's host; then the portal from the answer.
        Assert.Equal(["192.168.0.40", "192.168.0.10"], _inventory.Reads);
        foreach (var (_, body) in _server.Requests)
        {
            var inv = JsonDocument.Parse(body).RootElement.GetProperty("inventory");
            Assert.Equal("192.168.0.21", inv.GetProperty("ip_address").GetString());
            Assert.Equal(1000, inv.GetProperty("link_speed_mbps").GetInt64());
        }
    }

    [Fact]
    public async Task Keeps_the_last_inventory_when_a_read_returns_nothing()
    {
        var clock = new ManualClock(new DateTimeOffset(2026, 10, 9, 12, 0, 0, TimeSpan.Zero));
        var worker = Worker(clock);
        await worker.TickAsync(CancellationToken.None);
        _inventory.Next = null;
        clock.Now += AgentWorker.InventoryEvery;
        await worker.TickAsync(CancellationToken.None);

        var inv = JsonDocument.Parse(_server.Requests[^1].Body).RootElement.GetProperty("inventory");
        Assert.Equal("Intel I219-V", inv.GetProperty("nic").GetString());
    }
}
