using System.Text.Json;

namespace GgnetAgent;

/// <summary>
/// What the PC reports about itself: the network it reaches the server on
/// (ggRock's IP and Link Speed columns) and its hardware (the Hardware tab).
/// </summary>
public sealed record Inventory(
    string? IpAddress,
    string? MacAddress,
    long? LinkSpeedMbps,
    string? Nic,
    string? Cpu,
    IReadOnlyList<string>? Gpus,
    string? Motherboard,
    long? MemoryBytes);

public interface IInventory
{
    /// <summary>Reads the inventory; `serverIp` picks the network adapter that reaches the server.</summary>
    Task<Inventory?> ReadAsync(string? serverIp, CancellationToken ct);
}

/// <summary>IInventory from CIM (Get-NetAdapter, Win32_*), read with PowerShell.</summary>
public sealed class WindowsInventory(IScriptRunner runner) : IInventory
{
    public async Task<Inventory?> ReadAsync(string? serverIp, CancellationToken ct) =>
        Parse(await runner.RunAsync(Script(serverIp), ct));

    /// <summary>The script; `serverIp` is embedded only when it is a plain IP address.</summary>
    internal static string Script(string? serverIp)
    {
        var route = IscsiNames.IsPortalIp(serverIp)
            ? $"Find-NetRoute -RemoteIPAddress '{serverIp}' -ErrorAction SilentlyContinue | Where-Object IPAddress | Select-Object -First 1"
            : "$null";
        return $$"""
            $addr = {{route}}
            $nic = if ($addr) { Get-NetAdapter -InterfaceIndex $addr.InterfaceIndex -ErrorAction SilentlyContinue } else { $null }
            $board = Get-CimInstance Win32_BaseBoard | Select-Object -First 1
            [pscustomobject]@{
                ip_address = if ($addr) { [string]$addr.IPAddress } else { $null }
                mac_address = if ($nic) { [string]$nic.MacAddress } else { $null }
                link_speed_mbps = if ($nic) { [int64]($nic.ReceiveLinkSpeed / 1000000) } else { $null }
                nic = if ($nic) { [string]$nic.InterfaceDescription } else { $null }
                cpu = [string](Get-CimInstance Win32_Processor | Select-Object -First 1).Name
                gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { [string]$_.Name })
                motherboard = "$($board.Manufacturer) $($board.Product)"
                memory_bytes = [int64](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
            } | ConvertTo-Json -Compress
            """;
    }

    /// <summary>The script's JSON, trimmed; null when it is not an inventory.</summary>
    internal static Inventory? Parse(string json)
    {
        Inventory? raw;
        try
        {
            raw = JsonSerializer.Deserialize<Inventory>(json, Protocol.Json);
        }
        catch (JsonException)
        {
            return null;
        }
        if (raw is null) return null;

        static string? Clean(string? s)
        {
            var t = s?.Trim();
            return string.IsNullOrEmpty(t) ? null : t.Length > 128 ? t[..128] : t;
        }
        return raw with
        {
            IpAddress = Clean(raw.IpAddress),
            MacAddress = Clean(raw.MacAddress),
            Nic = Clean(raw.Nic),
            Cpu = Clean(raw.Cpu),
            Motherboard = Clean(raw.Motherboard),
            Gpus = (raw.Gpus ?? []).Select(Clean).OfType<string>().Take(8).ToList(),
        };
    }
}
