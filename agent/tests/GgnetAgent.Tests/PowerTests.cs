using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;

namespace GgnetAgent.Tests;

public class PowerTests
{
    private readonly FakeHandler _server = new();
    private readonly FakePower _power = new();

    private AgentWorker Worker(TimeProvider? clock = null) => new(
        _server.Client(), new FakeIscsi(),
        Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.40:8088" }),
        NullLogger<AgentWorker>.Instance, clock ?? TimeProvider.System, () => TimeSpan.FromHours(1), null, _power);

    private void Answer(string? command) =>
        _server.Body = Samples.ConfigJson.Replace("\"game_image\": \"cs2\"",
            command is null ? "\"game_image\": \"cs2\"" : $"\"game_image\": \"cs2\", \"command\": \"{command}\"");

    [Theory]
    [InlineData("shutdown")]
    [InlineData("reboot")]
    public async Task Runs_the_command_from_the_server(string command)
    {
        Answer(command);
        await Worker().TickAsync(CancellationToken.None);
        Assert.Equal([command], _power.Commands);
    }

    [Fact]
    public async Task Says_goodbye_before_shutting_down_and_then_stays_quiet()
    {
        var clock = new ManualClock(new DateTimeOffset(2026, 10, 12, 13, 0, 0, TimeSpan.Zero));
        var worker = Worker(clock);
        Answer("shutdown");
        await worker.TickAsync(CancellationToken.None);

        // Real PC: Windows killed the service during shutdown before it could stop,
        // so the goodbye has to go out before shutdown.exe runs.
        var bodies = _server.Requests.Select(r => System.Text.Json.JsonDocument.Parse(r.Body).RootElement).ToList();
        Assert.Equal([false, true], bodies.Select(b => b.GetProperty("stopping").GetBoolean()));
        Assert.Equal(["shutdown"], _power.Commands);

        Answer(null);
        clock.Now += TimeSpan.FromSeconds(30);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(2, _server.Requests.Count);                 // quiet: no beat to undo the goodbye

        clock.Now += AgentWorker.QuietAfterCommand;               // shutdown was cancelled
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(3, _server.Requests.Count);
    }

    private sealed class ManualClock(DateTimeOffset now) : TimeProvider
    {
        public DateTimeOffset Now { get; set; } = now;
        public override DateTimeOffset GetUtcNow() => Now;
    }

    [Fact]
    public async Task Ignores_unknown_commands_and_beats_without_one()
    {
        Answer("format c:");
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        Answer(null);
        await worker.TickAsync(CancellationToken.None);
        Assert.Empty(_power.Commands);
    }

    [Theory]
    [InlineData("shutdown", "/s /t 10")]
    [InlineData("reboot", "/r /t 10")]
    public void Shutdown_exe_arguments(string command, string expected)
    {
        Assert.StartsWith(expected, WindowsPowerControl.Arguments(command));
    }

    [Fact]
    public void Unknown_command_has_no_arguments()
    {
        Assert.Throws<ArgumentException>(() => WindowsPowerControl.Arguments("format"));
    }
}
