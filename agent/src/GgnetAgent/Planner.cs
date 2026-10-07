namespace GgnetAgent;

public abstract record AgentAction
{
    public sealed record SetInitiatorName(string CurrentIqn, string NewIqn) : AgentAction;
    public sealed record Disconnect(string TargetIqn) : AgentAction;
    public sealed record Connect(string PortalIp, int PortalPort, string TargetIqn) : AgentAction;
}

/// <summary>What the agent knows about this PC before acting.</summary>
public sealed record LocalState(
    string? InitiatorIqn,
    string? ConnectedTarget);   // the ggNet target this agent connected, if still connected

/// <summary>
/// Decides what to do from the server's answer and the local state. Pure, so
/// every rule is unit-tested without Windows.
/// </summary>
public static class Planner
{
    public static IReadOnlyList<AgentAction> Plan(
        AgentConfig? config, LocalState local, bool manageInitiatorName)
    {
        // Server unreachable: keep whatever is connected; players keep playing.
        if (config is null) return [];

        var actions = new List<AgentAction>();
        var wanted = config.IscsiTargetIqn;

        // A target that is no longer wanted (disk removed/switched, machine in error).
        if (local.ConnectedTarget is not null && local.ConnectedTarget != wanted)
        {
            actions.Add(new AgentAction.Disconnect(local.ConnectedTarget));
        }
        if (wanted is null || local.ConnectedTarget == wanted) return actions;

        if (!IscsiNames.IsIqn(wanted) || !IscsiNames.IsPortalIp(config.PortalIp)
            || !IscsiNames.IsPort(config.PortalPort))
        {
            throw new ServerException(
                $"Server sent an invalid target ({wanted} on {config.PortalIp}:{config.PortalPort})");
        }

        // The server's ACL only lets in the expected IQN; renaming the initiator
        // is only safe while nothing is connected.
        if (manageInitiatorName && local.InitiatorIqn is not null
            && local.InitiatorIqn != config.InitiatorIqn)
        {
            if (!IscsiNames.IsIqn(config.InitiatorIqn))
            {
                throw new ServerException($"Server sent an invalid initiator IQN: {config.InitiatorIqn}");
            }
            actions.Add(new AgentAction.SetInitiatorName(local.InitiatorIqn, config.InitiatorIqn));
        }

        actions.Add(new AgentAction.Connect(config.PortalIp, config.PortalPort, wanted));
        return actions;
    }
}
