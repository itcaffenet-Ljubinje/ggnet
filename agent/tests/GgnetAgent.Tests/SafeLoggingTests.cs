using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace GgnetAgent.Tests;

public class SafeLoggingTests
{
    /// <summary>Like the Event Log on a Windows that refuses the agent: every write throws.</summary>
    private sealed class RefusingProvider : ILoggerProvider
    {
        public ILogger CreateLogger(string categoryName) => new Refusing();
        public void Dispose() { }

        private sealed class Refusing : ILogger
        {
            public IDisposable? BeginScope<TState>(TState state) where TState : notnull => throw new InvalidOperationException("no");
            public bool IsEnabled(LogLevel logLevel) => true;
            public void Log<TState>(LogLevel l, EventId e, TState s, Exception? x, Func<TState, Exception?, string> f) =>
                throw new InvalidOperationException("Cannot open log for source 'ggnet-agent'. You may not have write access.");
        }
    }

    private sealed class Recording : ILoggerProvider
    {
        public readonly List<string> Lines = [];
        public ILogger CreateLogger(string categoryName) => new Rec(this);
        public void Dispose() { }
        private sealed class Rec(Recording o) : ILogger
        {
            public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
            public bool IsEnabled(LogLevel logLevel) => true;
            public void Log<TState>(LogLevel l, EventId e, TState s, Exception? x, Func<TState, Exception?, string> f) =>
                o.Lines.Add(f(s, x));
        }
    }

    [Fact]
    public void A_provider_that_cannot_write_never_stops_the_agent()
    {
        var recording = new Recording();
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Services.AddSingleton<ILoggerProvider, RefusingProvider>();   // by type, as AddEventLog registers it
        builder.Logging.AddProvider(recording);
        SafeLogging.Apply(builder.Services);
        using var host = builder.Build();

        var logger = host.Services.GetRequiredService<ILogger<SafeLoggingTests>>();
        logger.LogInformation("installing {Card}", "PCI\\VEN_10EC");   // threw before
        using (logger.BeginScope("job")) { }

        Assert.Contains("installing PCI\\VEN_10EC", recording.Lines);
    }

    [Fact]
    public void The_toolchain_log_goes_to_a_file()
    {
        var path = Path.Combine(Path.GetTempPath(), $"ggnet-agent-{Guid.NewGuid():N}.log");
        try
        {
            using var provider = new FileLoggerProvider(path);
            var logger = provider.CreateLogger("GgnetAgent.ToolchainWorker");
            logger.LogInformation("Toolchain job found");
            logger.LogDebug("not written");
            var text = File.ReadAllText(path);
            Assert.Contains("Information GgnetAgent.ToolchainWorker: Toolchain job found", text);
            Assert.DoesNotContain("not written", text);
        }
        finally
        {
            File.Delete(path);
        }
    }
}
