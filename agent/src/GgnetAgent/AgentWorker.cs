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
    IInventory? inventory = null,
    IPowerControl? power = null,
    ILocalDisks? localDisks = null,
    IUefiVariables? uefi = null,
    IIdleTasks? idleTasks = null) : BackgroundService
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
    internal static readonly TimeSpan GoodbyeTimeout = TimeSpan.FromSeconds(3);
    // After a shutdown/reboot command; if the shutdown is cancelled, beats resume.
    internal static readonly TimeSpan QuietAfterCommand = TimeSpan.FromMinutes(2);
    private DateTimeOffset? _quietUntil;
    private string? _initiator;
    private Inventory? _inventory;
    private DateTimeOffset? _inventoryAt;
    private string? _portalIp;
    // Boot Mode: local disks are taken offline once per service start.
    private bool _disksHidden;
    // Boot Mode: Windows' startup scans and clean-up tasks are turned off once per service start.
    private bool _tasksOff;
    // Boot Mode: the Secure Boot key is checked (and asked for) once per service start.
    private string? _mok;
    private bool _mokChecked;

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
        if (_quietUntil is { } quiet && time.GetUtcNow() < quiet) return;
        try
        {
            if (_bootedAt is null)
            {
                var boot = time.GetUtcNow() - _uptime();
                _bootedAt = boot.AddTicks(-(boot.Ticks % TimeSpan.TicksPerSecond));   // whole seconds
            }
            string? initiator = await iscsi.GetInitiatorNameAsync(ct);
            _initiator = initiator;

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
                        DriveLetter, _inventory, Mok: _mok), ct);
                _portalIp = config.PortalIp;
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException && !ct.IsCancellationRequested)
            {
                logger.LogWarning("Server {Server} unreachable: {Error}", _options.ServerUrl, e.Message);
            }

            await HideLocalDisksAsync(config, ct);
            await DisableIdleTasksAsync(config, ct);
            EnrollSecureBootKey(config);

            var actions = Planner.Plan(config, new LocalState(initiator, ConnectedTarget, RequestedLetter),
                _options.ManageInitiatorName, _options.DriveLetter);
            foreach (var action in actions)
            {
                await ApplyAsync(action, ct);
            }
            await RunCommandAsync(config?.Command, ct);
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

    private async Task DisableIdleTasksAsync(AgentConfig? config, CancellationToken ct)
    {
        if (_tasksOff || idleTasks is null || !_options.DisableIdleTasks || config?.Mode != "boot") return;
        try
        {
            var off = await idleTasks.DisableAsync(ct);
            _tasksOff = true;
            if (off.Count > 0)
                logger.LogInformation("Boot Mode: scheduled tasks turned off: {Tasks}", string.Join(", ", off));
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            // Retried on the next heartbeat.
            logger.LogWarning("Turning off scheduled tasks failed: {Error}", e.Message);
        }
    }

    private async Task HideLocalDisksAsync(AgentConfig? config, CancellationToken ct)
    {
        if (_disksHidden || localDisks is null || !_options.HideLocalDisks || config?.Mode != "boot") return;
        try
        {
            var hidden = await localDisks.HideAsync(ct);
            _disksHidden = true;
            foreach (var disk in hidden)
            {
                logger.LogInformation("Boot Mode: local disk {Disk} taken offline", disk);
            }
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            // Retried on the next heartbeat.
            logger.LogWarning("Taking the local disks offline failed: {Error}", e.Message);
        }
    }

    /// <summary>
    /// Boot Mode, when the server asks for it: make sure this PC trusts the
    /// ggNet Secure Boot key, else ask for it (MokManager at the next boot).
    /// Reported from the next heartbeat on.
    /// </summary>
    private void EnrollSecureBootKey(AgentConfig? config)
    {
        if (_mokChecked || uefi is null || config?.Mode != "boot" || config.SecureBoot is not { } key) return;
        _mokChecked = true;
        try
        {
            _mok = new MokEnrollment(uefi).Ensure(Convert.FromBase64String(key.Certificate), key.Password);
            if (_mok == MokState.Requested)
            {
                logger.LogInformation("Secure Boot: ggNet key requested; confirm Enroll MOK in MokManager at the next boot");
            }
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            _mok = MokState.Failed;
            logger.LogWarning("Secure Boot: asking for the ggNet key failed: {Error}", e.Message);
        }
    }

    private async Task RunCommandAsync(string? command, CancellationToken ct)
    {
        if (command is null || power is null) return;
        if (!WindowsPowerControl.Commands.Contains(command))
        {
            logger.LogWarning("Ignoring unknown command from the server: {Command}", command);
            return;
        }
        logger.LogInformation("The server asked for a {Command}", command);
        // Say goodbye now, while the network is surely up: during a Windows
        // shutdown the service is not given time to stop cleanly. Then stay
        // quiet so a heartbeat in the last seconds does not undo it.
        await SayGoodbyeAsync(ct);
        _quietUntil = time.GetUtcNow() + QuietAfterCommand;
        await power.RunAsync(command, ct);
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
        await SayGoodbyeAsync(cancellationToken);
    }

    /// <summary>
    /// Tell the server right away that this PC is going offline, so the UI does
    /// not wait for missed heartbeats. Best effort and short: it must never hold
    /// up a Windows shutdown.
    /// </summary>
    private async Task SayGoodbyeAsync(CancellationToken cancellationToken)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(GoodbyeTimeout);
        try
        {
            await server.HeartbeatAsync(new Heartbeat(_machineName, Version, _initiator,
                ConnectedTarget is not null, _bootedAt, DriveLetter, null, Stopping: true), cts.Token);
        }
        catch (Exception e)
        {
            logger.LogInformation("Could not tell the server the PC goes offline: {Error}", e.Message);
        }
    }
}
