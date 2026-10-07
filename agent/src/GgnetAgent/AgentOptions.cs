namespace GgnetAgent;

/// <summary>Settings from the "Agent" section of appsettings.json next to ggnet-agent.exe.</summary>
public sealed class AgentOptions
{
    public const string Section = "Agent";

    /// <summary>ggNet server, e.g. http://192.168.0.10:8088.</summary>
    public string ServerUrl { get; set; } = "";

    /// <summary>Drive letter for the game disk.</summary>
    public string DriveLetter { get; set; } = "D";

    public int HeartbeatSeconds { get; set; } = 30;

    /// <summary>
    /// Set the Windows initiator name to the IQN the server's ACL expects
    /// (Windows uses iqn.1991-05.com.microsoft:&lt;fqdn&gt; on domain PCs).
    /// </summary>
    public bool ManageInitiatorName { get; set; } = true;

    /// <summary>Returns the problems with these settings; empty when they are valid.</summary>
    public IReadOnlyList<string> Validate()
    {
        var errors = new List<string>();
        if (!Uri.TryCreate(ServerUrl, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            errors.Add($"Agent:ServerUrl must be an http(s) URL, got '{ServerUrl}'");
        }
        if (!IscsiNames.IsDriveLetter(DriveLetter))
        {
            errors.Add($"Agent:DriveLetter must be one letter D-Z, got '{DriveLetter}'");
        }
        if (HeartbeatSeconds is < 5 or > 3600)
        {
            errors.Add($"Agent:HeartbeatSeconds must be 5-3600, got {HeartbeatSeconds}");
        }
        return errors;
    }
}
