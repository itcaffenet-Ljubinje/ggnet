using System.Reflection;
using Microsoft.Extensions.Options;

namespace GgnetAgent;

/// <summary>
/// Heartbeat loop: report state, get the wanted target from the server, and
/// connect/disconnect the game disk to match. Errors are logged and retried
/// on the next heartbeat; the service never stops on its own.
/// </summary>
public sealed class AgentWorker(
    ServerClient server,
    IIscsiInitiator iscsi,
    IOptions<AgentOptions> options,
    ILogger<AgentWorker> logger,
    TimeProvider time) : BackgroundService
{
    internal static readonly string Version =
        typeof(AgentWorker).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()
            ?.InformationalVersion.Split('+')[0] ?? "0.0.0";

    private readonly AgentOptions _options = options.Value;
    private readonly string _machineName = Environment.MachineName.ToLowerInvariant();

    // The service starts with Windows, so its start time identifies this boot.
    // (Not the kernel uptime: Fast Startup keeps it running across shutdowns.)
    private DateTimeOffset? _startedAt;

    /// <summary>The ggNet target this agent connected (sessions are not persistent across reboots).</summary>
    internal string? ConnectedTarget { get; private set; }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("ggnet-agent {Version} for {Machine}, server {Server}",
            Version, _machineName, _options.ServerUrl);

        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(_options.HeartbeatSeconds), time);
        do
        {
            await TickAsync(stoppingToken);
        } while (await WaitAsync(timer, stoppingToken));
    }

    private static async Task<bool> WaitAsync(PeriodicTimer timer, CancellationToken ct)
    {
        try
        {
            return await timer.WaitForNextTickAsync(ct);
        }
        catch (OperationCanceledException)
        {
            return false;
        }
    }

    /// <summary>One heartbeat round. Public to tests through InternalsVisibleTo.</summary>
    internal async Task TickAsync(CancellationToken ct)
    {
        try
        {
            _startedAt ??= time.GetUtcNow();
            string? initiator = await iscsi.GetInitiatorNameAsync(ct);

            // The session can drop (server reset, network); forget it so it is reconnected.
            if (ConnectedTarget is not null && !await iscsi.IsConnectedAsync(ConnectedTarget, ct))
            {
                logger.LogWarning("Target {Target} is no longer connected", ConnectedTarget);
                ConnectedTarget = null;
            }

            AgentConfig? config = null;
            try
            {
                config = await server.HeartbeatAsync(
                    new Heartbeat(_machineName, Version, initiator, ConnectedTarget is not null, _startedAt), ct);
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException && !ct.IsCancellationRequested)
            {
                logger.LogWarning("Server {Server} unreachable: {Error}", _options.ServerUrl, e.Message);
            }

            var actions = Planner.Plan(config, new LocalState(initiator, ConnectedTarget),
                _options.ManageInitiatorName);
            foreach (var action in actions)
            {
                await ApplyAsync(action, ct);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e)
        {
            logger.LogError("{Error}", e.Message);
        }
    }

    private async Task ApplyAsync(AgentAction action, CancellationToken ct)
    {
        switch (action)
        {
            case AgentAction.SetInitiatorName a:
                logger.LogInformation("Setting initiator name {Old} -> {New}", a.CurrentIqn, a.NewIqn);
                await iscsi.SetInitiatorNameAsync(a.CurrentIqn, a.NewIqn, ct);
                break;

            case AgentAction.Disconnect a:
                logger.LogInformation("Disconnecting {Target}", a.TargetIqn);
                await iscsi.DisconnectAsync(a.TargetIqn, ct);
                ConnectedTarget = null;
                break;

            case AgentAction.Connect a:
                logger.LogInformation("Connecting {Target} on {Ip}:{Port} as {Drive}:",
                    a.TargetIqn, a.PortalIp, a.PortalPort, _options.DriveLetter);
                var letter = await iscsi.ConnectAsync(a.PortalIp, a.PortalPort, a.TargetIqn, _options.DriveLetter, ct);
                ConnectedTarget = a.TargetIqn;
                if (!string.Equals(letter, _options.DriveLetter, StringComparison.OrdinalIgnoreCase))
                {
                    logger.LogWarning("{Drive}: is taken; the game disk is {Letter}: instead",
                        _options.DriveLetter, letter);
                }
                break;
        }
    }

    /// <summary>On service stop / Windows shutdown, log out of the target cleanly.</summary>
    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        await base.StopAsync(cancellationToken);
        if (ConnectedTarget is null) return;
        try
        {
            logger.LogInformation("Stopping: disconnecting {Target}", ConnectedTarget);
            await iscsi.DisconnectAsync(ConnectedTarget, cancellationToken);
            ConnectedTarget = null;
        }
        catch (Exception e)
        {
            logger.LogWarning("Disconnect on stop failed: {Error}", e.Message);
        }
    }
}
