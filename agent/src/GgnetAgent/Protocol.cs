using System.Text.Json;
using System.Text.Json.Serialization;

namespace GgnetAgent;

/// <summary>Body of POST /api/v1/agent/heartbeat (see agent/README.md).</summary>
/// <param name="BootedAt">
/// When this agent process started (UTC), i.e. this Windows boot. A newer value
/// tells the server the PC restarted, so it discards the old writeback first.
/// </param>
public sealed record Heartbeat(
    string Name,
    string AgentVersion,
    string? InitiatorIqn,
    bool IscsiConnected,
    DateTimeOffset? BootedAt = null);

/// <summary>The server's answer: what this PC should connect.</summary>
public sealed record AgentConfig(
    int MachineId,
    string Name,
    string Mode,
    string Status,
    string InitiatorIqn,
    string? IscsiTargetIqn,
    string PortalIp,
    int PortalPort,
    string? GameDisk);

internal static class Protocol
{
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };
}
