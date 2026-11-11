using Microsoft.Extensions.DependencyInjection.Extensions;

namespace GgnetAgent;

/// <summary>
/// A log that cannot be written is never a reason to stop. On some Windows
/// builds the agent may not write to the Event Log ("Cannot open log for
/// source 'ggnet-agent'. Access is denied."); the logging framework then
/// throws from every log call, which stopped the agent before it did
/// anything (an Add new hardware run never finished). Every logger provider
/// is wrapped so a failing one is skipped and the others still log.
/// </summary>
public static class SafeLogging
{
    public static void Apply(IServiceCollection services)
    {
        for (var i = 0; i < services.Count; i++)
        {
            var d = services[i];
            if (d.ServiceType != typeof(ILoggerProvider) || d.ImplementationType == typeof(SafeLoggerProvider))
                continue;
            services[i] = ServiceDescriptor.Singleton<ILoggerProvider>(sp => new SafeLoggerProvider(Create(sp, d)));
        }
    }

    private static ILoggerProvider Create(IServiceProvider sp, ServiceDescriptor d) =>
        (ILoggerProvider)(d.ImplementationInstance
            ?? d.ImplementationFactory?.Invoke(sp)
            ?? ActivatorUtilities.CreateInstance(sp, d.ImplementationType!));
}

public sealed class SafeLoggerProvider(ILoggerProvider inner) : ILoggerProvider
{
    public ILogger CreateLogger(string categoryName)
    {
        try
        {
            return new SafeLogger(inner.CreateLogger(categoryName));
        }
        catch (Exception)
        {
            return new SafeLogger(null);
        }
    }

    public void Dispose()
    {
        try
        {
            inner.Dispose();
        }
        catch (Exception)
        {
            // nothing to do about it
        }
    }
}

public sealed class SafeLogger(ILogger? inner) : ILogger
{
    public IDisposable? BeginScope<TState>(TState state) where TState : notnull
    {
        try
        {
            return inner?.BeginScope(state);
        }
        catch (Exception)
        {
            return null;
        }
    }

    public bool IsEnabled(LogLevel logLevel)
    {
        try
        {
            return inner?.IsEnabled(logLevel) ?? false;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception,
        Func<TState, Exception?, string> formatter)
    {
        try
        {
            inner?.Log(logLevel, eventId, state, exception, formatter);
        }
        catch (Exception)
        {
            // This provider cannot write; the others still do.
        }
    }
}

/// <summary>
/// Add new hardware: the run's log next to the job (C:\ProgramData\ggNet\toolchain\agent.log),
/// so the server can tell why a run did not finish even when the Event Log is closed.
/// </summary>
public sealed class FileLoggerProvider(string path) : ILoggerProvider
{
    private readonly object _lock = new();

    public ILogger CreateLogger(string categoryName) => new FileLogger(this, categoryName);

    internal void Write(string line)
    {
        lock (_lock)
        {
            File.AppendAllText(path, line + Environment.NewLine);
        }
    }

    public void Dispose() { }

    private sealed class FileLogger(FileLoggerProvider owner, string category) : ILogger
    {
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(LogLevel logLevel) => logLevel >= LogLevel.Information;

        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception,
            Func<TState, Exception?, string> formatter)
        {
            if (!IsEnabled(logLevel)) return;
            var text = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {logLevel} {category}: {formatter(state, exception)}";
            if (exception is not null) text += Environment.NewLine + exception;
            owner.Write(text);
        }
    }
}
