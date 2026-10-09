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
    TimeProvider time,
    Func<TimeSpan>? uptime = null,
    IInventory? inventory = null) : BackgroundService
{
    internal static readonly string Version =
        typeof(AgentWorker).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()
            ?.InformationalVersion.Split('+')[0] ?? "0.0.0";

    private readonly AgentOptions _options = options.Value;
    private readonly string _machineName = Environment.MachineName.ToLowerInvariant();

    // Windows boot time (now minus uptime), so restarting or updating the
    // agent service is not taken for a reboot (which discards the writeback).
    // Fast Startup would keep it across shutdowns; install.ps1 turns that off.
    private readonly Func<TimeSpan> _uptime = uptime ?? (() => TimeSpan.FromMilliseconds(Environment.TickCount64));
    private DateTimeOffset? _bootedAt;

    // Inventory (IP, link speed, hardware) costs a PowerShell run; read it at
    // start and then every InventoryEvery, and send the last one each beat.
    internal static readonly TimeSpan InventoryEvery = TimeSpan.FromMinutes(10);
    private Inventory? _inventory;
    private DateTimeOffset? _inventoryAt;
    private string? _portalIp;

        /// <summary>The ggNet target this agent connected (sessions are not persistent across reboots).</summary>
    internal string? ConnectedTarget { get; private set; }

    /// <summary>The letter the connection was made for, and the one the disk actually got.</summary>
    internal string? RequestedLetter { get; private set; }
    internal string? DriveLetter { get; private set; }

    private void Forget()
    {
        ConnectedTarget = RequestedLetter = DriveLetter = null;
    }

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
            if (_bootedAt is null)
            {
                var boot = time.GetUtcNow() - _uptime();
                _bootedAt = boot.AddTicks(-(boot.Ticks % TimeSpan.TicksPerSecond));   // whole seconds
            }
            string? initiator = await iscsi.GetInitiatorNameAsync(ct);

            // The session can drop (server reset, network); forget it so it is reconnected.
            if (ConnectedTarget is not null && !await iscsi.IsConnectedAsync(ConnectedTarget, ct))
            {
                logger.LogWarning("Target {Target} is no longer connected", ConnectedTarget);
                Forget();
            }

            await RefreshInventoryAsync(ct);

            AgentConfig? config = null;
            try
            {
                config = await server.HeartbeatAsync(
                    new Heartbeat(_machineName, Version, initiator, ConnectedTarget is not null, _bootedAt,
                        DriveLetter, _inventory), ct);
                _portalIp = config.PortalIp;
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException && !ct.IsCancellationRequested)
            {
                logger.LogWarning("Server {Server} unreachable: {Error}", _options.ServerUrl, e.Message);
            }

            var actions = Planner.Plan(config, new LocalState(initiator, ConnectedTarget, RequestedLetter),
                _options.ManageInitiatorName, _options.DriveLetter);
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

    private async Task RefreshInventoryAsync(CancellationToken ct)
    {
        var now = time.GetUtcNow();
        if (inventory is null || (_inventoryAt is { } at && now - at < InventoryEvery)) return;
        _inventoryAt = now;   // also after a failure: retry at the next interval, not every beat
        try
        {
            // Before the first answer the portal is unknown; the server URL's host is the next best.
            var server = _portalIp ?? new Uri(_options.ServerUrl).Host;
            _inventory = await inventory.ReadAsync(server, ct) ?? _inventory;
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            logger.LogWarning("Reading the PC's inventory failed: {Error}", e.Message);
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
                Forget();
                break;

            case AgentAction.Connect a:
                logger.LogInformation("Connecting {Target} on {Ip}:{Port} as {Drive}:",
                    a.TargetIqn, a.PortalIp, a.PortalPort, a.DriveLetter);
                var letter = await iscsi.ConnectAsync(a.PortalIp, a.PortalPort, a.TargetIqn, a.DriveLetter, ct);
                ConnectedTarget = a.TargetIqn;
                RequestedLetter = a.DriveLetter;
                DriveLetter = IscsiNames.IsDriveLetter(letter) ? letter : null;
                if (DriveLetter is null)
                {
                    logger.LogWarning("The connect script did not report the game disk's drive letter");
                }
                else if (DriveLetter != a.DriveLetter)
                {
                    logger.LogWarning("{Drive}: is taken; the game disk is {Letter}: instead",
                        a.DriveLetter, letter);
                }
                break;
        }
    }

    /// <summary>
    /// The game disk stays connected when the service stops: a service restart
    /// (update, crash recovery) must not pull it from running games, and a
    /// Windows shutdown ends the session anyway. uninstall.ps1 disconnects it.
    /// </summary>
    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        await base.StopAsync(cancellationToken);
        if (ConnectedTarget is not null)
        {
            logger.LogInformation("Stopping; {Target} stays connected", ConnectedTarget);
        }
    }
}
