using System.Runtime.InteropServices;
using System.Text;

namespace GgnetScan;

/// <summary>The PCI devices present in this PC, with their instance and hardware IDs (SetupAPI).</summary>
public static class PciDevices
{
    public const string NetClass = "{4d36e972-e325-11ce-bfc1-08002be10318}";

    public static List<ScanDevice> Present()
    {
        var found = new List<ScanDevice>();
        var set = SetupDiGetClassDevsW(IntPtr.Zero, "PCI", IntPtr.Zero, DigcfPresent | DigcfAllClasses);
        if (set == new IntPtr(-1))
            return found;
        try
        {
            var data = new SpDevinfoData { CbSize = Marshal.SizeOf<SpDevinfoData>() };
            for (var i = 0; SetupDiEnumDeviceInfo(set, i, ref data); i++)
            {
                var id = InstanceId(set, ref data);
                if (id is null)
                    continue;
                found.Add(new ScanDevice(
                    id,
                    MultiSz(set, ref data, SpdrpHardwareId),
                    MultiSz(set, ref data, SpdrpCompatibleIds),
                    Text(set, ref data, SpdrpClassGuid),
                    Text(set, ref data, SpdrpDeviceDesc),
                    Text(set, ref data, SpdrpService)));
            }
        }
        finally
        {
            SetupDiDestroyDeviceInfoList(set);
        }
        return found;
    }

    private static string? InstanceId(IntPtr set, ref SpDevinfoData data)
    {
        var buffer = new char[512];
        return SetupDiGetDeviceInstanceIdW(set, ref data, buffer, buffer.Length, out var size)
            ? new string(buffer, 0, Math.Max(0, size - 1))
            : null;
    }

    private static byte[]? Property(IntPtr set, ref SpDevinfoData data, int property)
    {
        var buffer = new byte[4096];
        return SetupDiGetDeviceRegistryPropertyW(set, ref data, property, out _, buffer, buffer.Length, out var size)
            ? buffer[..size]
            : null;
    }

    private static string? Text(IntPtr set, ref SpDevinfoData data, int property) =>
        Property(set, ref data, property) is { } bytes ? Encoding.Unicode.GetString(bytes).TrimEnd('\0') : null;

    private static List<string> MultiSz(IntPtr set, ref SpDevinfoData data, int property) =>
        Property(set, ref data, property) is { } bytes ? SplitMultiSz(bytes) : [];

    internal static List<string> SplitMultiSz(byte[] bytes) =>
        Encoding.Unicode.GetString(bytes).Split('\0', StringSplitOptions.RemoveEmptyEntries).ToList();

    private const int DigcfPresent = 0x02;
    private const int DigcfAllClasses = 0x04;
    private const int SpdrpDeviceDesc = 0x00;
    private const int SpdrpHardwareId = 0x01;
    private const int SpdrpCompatibleIds = 0x02;
    private const int SpdrpService = 0x04;
    private const int SpdrpClassGuid = 0x08;

    [StructLayout(LayoutKind.Sequential)]
    private struct SpDevinfoData
    {
        public int CbSize;
        public Guid ClassGuid;
        public int DevInst;
        public IntPtr Reserved;
    }

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SetupDiGetClassDevsW(IntPtr classGuid, string enumerator, IntPtr hwndParent, int flags);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiEnumDeviceInfo(IntPtr set, int index, ref SpDevinfoData data);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiGetDeviceInstanceIdW(IntPtr set, ref SpDevinfoData data, char[] buffer,
        int size, out int requiredSize);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiGetDeviceRegistryPropertyW(IntPtr set, ref SpDevinfoData data, int property,
        out int type, byte[] buffer, int size, out int requiredSize);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);
}
