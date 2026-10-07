using System.Net;
using System.Text.RegularExpressions;

namespace GgnetAgent;

/// <summary>
/// Validation for values that end up inside PowerShell scripts. Only values
/// that pass these checks are ever embedded, so they cannot contain quotes or
/// anything else that would change the script.
/// </summary>
internal static partial class IscsiNames
{
    // iqn.YYYY-MM.reversed.domain[:suffix], lowercase (RFC 3720).
    [GeneratedRegex(@"^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*(:[a-z0-9][a-z0-9.:-]*)?$")]
    private static partial Regex IqnRegex();

    public static bool IsIqn(string? value) =>
        value is { Length: <= 223 } && IqnRegex().IsMatch(value);

    public static bool IsPortalIp(string? value) =>
        value is not null && IPAddress.TryParse(value, out var ip)
        && !ip.Equals(IPAddress.Any) && !ip.Equals(IPAddress.IPv6Any)
        && ip.ToString() == value;

    public static bool IsPort(int value) => value is > 0 and < 65536;

    // A, B and C are floppy and system drives on Windows.
    public static bool IsDriveLetter(string? value) =>
        value is { Length: 1 } && value[0] is >= 'D' and <= 'Z';
}
