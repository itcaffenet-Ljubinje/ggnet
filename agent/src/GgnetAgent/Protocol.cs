using System.Text.Json;
using System.Text.Json.Serialization;

namespace GgnetAgent;

/// <summary>Body of POST /api/v1/agent/heartbeat (see agent/README.md).</summary>
/// <param name="BootedAt">
/// When Windows booted (UTC). A newer value tells the server the PC restarted,
/// so it discards the old writeback first; restarting only the agent service
/// keeps the same value.
/// </param>
public sealed record Heartbeat(
    string Name,
    string AgentVersion,
    string? InitiatorIqn,
    bool IscsiConnected,
    DateTimeOffset? BootedAt = null,
    string? DriveLetter = null);   // the letter the game disk got, while connected

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
    string? GameDisk,
    string? DriveLetter = null);   // the letter the game disk should get (set per machine in the UI)

internal static class Protocol
{
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };
}
