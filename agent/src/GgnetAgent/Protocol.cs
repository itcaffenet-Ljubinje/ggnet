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
    string? DriveLetter = null,    // the letter the game image got, while connected
    Inventory? Inventory = null,   // network and hardware, refreshed every few minutes
    bool Stopping = false,         // last beat as the service stops (Windows shutting down)
    string? Mok = null);           // the ggNet Secure Boot key: MokState, once asked for

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
    string? GameImage,
    string? DriveLetter = null,    // the letter the game image should get (set per machine in the UI)
    string? Command = null,        // "shutdown" | "reboot", sent once
    SecureBootKey? SecureBoot = null);   // set while the server asks PCs to enroll its key

/// <summary>The ggNet Secure Boot key to enroll (DER, base64) and the MokManager password.</summary>
public sealed record SecureBootKey(string Certificate, string Password);

internal static class Protocol
{
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };
}
