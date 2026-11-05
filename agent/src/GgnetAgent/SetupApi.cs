using System.ComponentModel;
using System.Runtime.InteropServices;

namespace GgnetAgent;

/// <summary>One device to install in the image, from the toolchain job.</summary>
public sealed record ToolchainDevice(
    string InstanceId,                 // PCI\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\3&11583659&0&FE
    IReadOnlyList<string> HardwareIds,
    IReadOnlyList<string>? CompatibleIds = null,
    string ClassGuid = SetupApiInstaller.NetClass);

/// <summary>How installing one device went.</summary>
public sealed record ToolchainDeviceResult(
    string InstanceId, bool Ok, string? Driver = null, string? Error = null, string? Name = null,
    [property: System.Text.Json.Serialization.JsonIgnore] bool Deferred = false);

public interface IDeviceInstaller
{
    /// <summary>Creates the device (not present on this machine) and installs its best driver.</summary>
    ToolchainDeviceResult Install(ToolchainDevice device);
}

/// <summary>
/// Installs a device that is not in this machine the way Windows installs a
/// card it finds: the device node is created under the PC's own instance ID,
/// given its hardware IDs and registered, then the best driver Windows has for
/// it (inbox or in the driver store) is selected and installed with the class
/// installer, which for a network card also sets up its bindings. The image
/// then boots on the PC that has the card.
/// </summary>
public sealed class SetupApiInstaller : IDeviceInstaller
{
    public const string NetClass = "{4d36e972-e325-11ce-bfc1-08002be10318}";

    public ToolchainDeviceResult Install(ToolchainDevice device)
    {
        // Early in boot Windows defers an install ("Installation deferred (too
        // early)") to its own service, which cannot see the device this process
        // created ("removed unexpectedly", 0xE000020B): the card is left without a
        // driver. Later in boot it installs at once, so a failed try is repeated.
        ToolchainDeviceResult result = null!;
        for (var attempt = 1; attempt <= InstallAttempts; attempt++)
        {
            result = InstallOnce(device);
            if (result.Ok || !result.Deferred)
                return result;
            Thread.Sleep(RetryDelay);
        }
        return result;
    }

    private static readonly TimeSpan RetryDelay = TimeSpan.FromSeconds(20);
    private const int InstallAttempts = 4;

    private ToolchainDeviceResult InstallOnce(ToolchainDevice device)
    {
        // Let Windows finish what it installs at boot first (the VM's own devices).
        CMP_WaitNoPendingInstallEvents(InstallWaitMs);
        var classGuid = Guid.Parse(device.ClassGuid);
        var set = SetupDiCreateDeviceInfoList(ref classGuid, IntPtr.Zero);
        if (set == InvalidHandle)
            return Fail(device, "SetupDiCreateDeviceInfoList");
        try
        {
            var data = new SpDevinfoData { CbSize = Marshal.SizeOf<SpDevinfoData>() };
            if (!SetupDiCreateDeviceInfoW(set, device.InstanceId, ref classGuid, null, IntPtr.Zero, 0, ref data))
            {
                // Left in the image by an earlier run whose install failed: Windows only marks
                // such a non-present device for reinstall, so it is removed and made anew.
                if (Marshal.GetLastWin32Error() != ErrorDevinstAlreadyExists
                    || !SetupDiOpenDeviceInfoW(set, device.InstanceId, IntPtr.Zero, 0, ref data))
                    return Fail(device, "SetupDiCreateDeviceInfo");
                if (!SetupDiCallClassInstaller(DifRemove, set, ref data))
                    return Fail(device, "remove the device an earlier run left");
                data = new SpDevinfoData { CbSize = Marshal.SizeOf<SpDevinfoData>() };
                if (!SetupDiCreateDeviceInfoW(set, device.InstanceId, ref classGuid, null, IntPtr.Zero, 0, ref data))
                    return Fail(device, "SetupDiCreateDeviceInfo");
            }
            // Windows 10 and later take hardware IDs through SetupAPI only for
            // root-enumerated devices; a PCI device gets them in its Enum key.
            var idsSet = SetProperty(set, ref data, SpdrpHardwareId, device.HardwareIds)
                && (device.CompatibleIds is not { Count: > 0 } compatible
                    || SetProperty(set, ref data, SpdrpCompatibleIds, compatible));
            if (!idsSet && Marshal.GetLastWin32Error() != ErrorInvalidRegProperty)
                return Fail(device, "set the hardware IDs");
            if (!SetupDiCallClassInstaller(DifRegisterDevice, set, ref data)
                && Marshal.GetLastWin32Error() != ErrorDevinstAlreadyExists)
                return Fail(device, "register the device");
            if (!idsSet)
            {
                try
                {
                    if (!OperatingSystem.IsWindows())
                        throw new PlatformNotSupportedException("the registry is only on Windows");
                    WriteIds(device);
                }
                catch (Exception e) when (e is UnauthorizedAccessException or IOException
                    or System.Security.SecurityException or PlatformNotSupportedException)
                {
                    return new ToolchainDeviceResult(device.InstanceId, false, Error: $"write the hardware IDs: {e.Message}");
                }
            }
            if (!SetupDiBuildDriverInfoList(set, ref data, SpditCompatDriver))
                return Fail(device, "list its drivers");
            if (!SetupDiCallClassInstaller(DifSelectBestCompatDrv, set, ref data))
                return Fail(device, "find a driver for it (none in the image)");
            var driver = new SpDrvinfoData { CbSize = Marshal.SizeOf<SpDrvinfoData>() };
            if (!SetupDiGetSelectedDriverW(set, ref data, ref driver))
                return Fail(device, "read the selected driver");
            if (!DiInstallDevice(IntPtr.Zero, set, ref data, ref driver, 0, out _))
                return Fail(device, $"install {driver.Description}");
            // A deferred install runs in the background: wait for it, then check
            // that the card really got a driver (a retry removes and remakes it).
            CMP_WaitNoPendingInstallEvents(InstallWaitMs);
            if (DriverKey(set, ref data) is null)
                return new ToolchainDeviceResult(device.InstanceId, false,
                    Error: $"Windows did not finish installing {driver.Description} (see setupapi.dev.log)",
                    Deferred: true);
            return new ToolchainDeviceResult(device.InstanceId, true, $"{driver.ProviderName} {driver.Description}",
                Name: driver.Description);
        }
        finally
        {
            SetupDiDestroyDeviceInfoList(set);
        }
    }

    /// <summary>The device's driver key (SPDRP_DRIVER), set once its driver is installed; null before.</summary>
    private static string? DriverKey(IntPtr set, ref SpDevinfoData data)
    {
        var buffer = new byte[512];
        if (!SetupDiGetDeviceRegistryPropertyW(set, ref data, SpdrpDriver, out _, buffer, buffer.Length, out var size))
            return null;
        var text = System.Text.Encoding.Unicode.GetString(buffer, 0, size).TrimEnd('\0');
        return text.Length > 0 ? text : null;
    }

    private static ToolchainDeviceResult Fail(ToolchainDevice device, string step) =>
        new(device.InstanceId, false, Error: $"{step}: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");

    /// <summary>HardwareID and CompatibleIDs of a registered device, in its Enum key.</summary>
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    private static void WriteIds(ToolchainDevice device)
    {
        using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
            $@"SYSTEM\CurrentControlSet\Enum\{device.InstanceId}", writable: true)
            ?? throw new IOException($"the device's Enum key is missing after its registration");
        key.SetValue("HardwareID", device.HardwareIds.ToArray(), Microsoft.Win32.RegistryValueKind.MultiString);
        if (device.CompatibleIds is { Count: > 0 } compatible)
            key.SetValue("CompatibleIDs", compatible.ToArray(), Microsoft.Win32.RegistryValueKind.MultiString);
    }

    private static bool SetProperty(IntPtr set, ref SpDevinfoData data, int property, IReadOnlyList<string> values)
    {
        var bytes = System.Text.Encoding.Unicode.GetBytes(string.Join('\0', values) + "\0\0");
        return SetupDiSetDeviceRegistryPropertyW(set, ref data, property, bytes, bytes.Length);
    }

    // ── setupapi.dll / newdev.dll ─────────────────────────────────────

    private static readonly IntPtr InvalidHandle = new(-1);
    private const int ErrorDevinstAlreadyExists = unchecked((int)0xE0000207);
    private const int ErrorInvalidRegProperty = unchecked((int)0xE0000209);
    private const int SpdrpHardwareId = 0x01;
    private const int SpdrpCompatibleIds = 0x02;
    private const int SpdrpDriver = 0x09;
    private const int InstallWaitMs = 5 * 60 * 1000;
    private const int DifRemove = 0x05;
    private const int DifSelectBestCompatDrv = 0x17;
    private const int DifRegisterDevice = 0x19;
    private const int SpditCompatDriver = 0x02;

    [StructLayout(LayoutKind.Sequential)]
    private struct SpDevinfoData
    {
        public int CbSize;
        public Guid ClassGuid;
        public int DevInst;
        public IntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct SpDrvinfoData
    {
        public int CbSize;
        public int DriverType;
        public IntPtr Reserved;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Description;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string MfgName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ProviderName;
        public long DriverDate;
        public ulong DriverVersion;
    }

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid classGuid, IntPtr hwndParent);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiCreateDeviceInfoW(IntPtr set, string deviceName, ref Guid classGuid,
        string? description, IntPtr hwndParent, int flags, ref SpDevinfoData data);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiOpenDeviceInfoW(IntPtr set, string instanceId, IntPtr hwndParent, int flags,
        ref SpDevinfoData data);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr set, ref SpDevinfoData data, int property,
        byte[] buffer, int size);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiCallClassInstaller(int function, IntPtr set, ref SpDevinfoData data);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiBuildDriverInfoList(IntPtr set, ref SpDevinfoData data, int driverType);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiGetSelectedDriverW(IntPtr set, ref SpDevinfoData data, ref SpDrvinfoData driver);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiGetDeviceRegistryPropertyW(IntPtr set, ref SpDevinfoData data, int property,
        out int type, byte[] buffer, int size, out int requiredSize);

    /// <summary>Waits until Windows has no device install in progress or queued (WAIT_OBJECT_0 = 0).</summary>
    [DllImport("setupapi.dll")]
    private static extern int CMP_WaitNoPendingInstallEvents(int timeoutMs);

    [DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool DiInstallDevice(IntPtr hwndParent, IntPtr set, ref SpDevinfoData data,
        ref SpDrvinfoData driver, int flags, out bool needReboot);
}
