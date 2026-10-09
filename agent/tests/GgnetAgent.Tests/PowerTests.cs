using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;

namespace GgnetAgent.Tests;

public class PowerTests
{
    private readonly FakeHandler _server = new();
    private readonly FakePower _power = new();

    private AgentWorker Worker() => new(
        _server.Client(), new FakeIscsi(),
        Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.40:8088" }),
        NullLogger<AgentWorker>.Instance, TimeProvider.System, () => TimeSpan.FromHours(1), null, _power);

    private void Answer(string? command) =>
        _server.Body = Samples.ConfigJson.Replace("\"game_disk\": \"cs2\"",
            command is null ? "\"game_disk\": \"cs2\"" : $"\"game_disk\": \"cs2\", \"command\": \"{command}\"");

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
