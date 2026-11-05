using System.Text.Json;

namespace GgnetAgent;

/// <summary>
/// The work the server left in the image for a toolchain run (Add new hardware):
/// the devices of a PC the image does not know yet. The server writes it
/// offline into a clone of the image and boots the clone in a VM; the agent
/// then installs the devices instead of doing its normal work, writes the
/// result next to the job and shuts the VM down. The clone becomes the
/// image's next version, and boots on that PC.
/// </summary>
public sealed record ToolchainJob(IReadOnlyList<ToolchainDevice> Devices)
{
    /// <summary>C:\ProgramData\ggNet\toolchain</summary>
    public static string Folder => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "ggNet", "toolchain");

    public const string JobFile = "job.json";
    public const string ResultFile = "result.json";
    public const string DoneFile = "job.done";

    /// <summary>The job in `folder`, or null when there is none (a normal start).</summary>
    public static ToolchainJob? Load(string folder)
    {
        var path = Path.Combine(folder, JobFile);
        return File.Exists(path) ? Parse(File.ReadAllText(path)) : null;
    }

    /// <summary>Reads a job; throws FormatException with the reason when it is not usable.</summary>
    public static ToolchainJob Parse(string json)
    {
        ToolchainJob? job;
        try
        {
            job = JsonSerializer.Deserialize<ToolchainJob>(json, Protocol.Json);
        }
        catch (JsonException e)
        {
            throw new FormatException($"The toolchain job is not valid JSON: {e.Message}", e);
        }
        if (job?.Devices is not { Count: > 0 })
            throw new FormatException("The toolchain job has no devices");
        foreach (var d in job.Devices)
        {
            if (string.IsNullOrWhiteSpace(d.InstanceId) || !d.InstanceId.Contains('\\'))
                throw new FormatException($"Bad device instance ID '{d.InstanceId}'");
            if (d.HardwareIds is not { Count: > 0 } || d.HardwareIds.Any(string.IsNullOrWhiteSpace))
                throw new FormatException($"Device {d.InstanceId} has no hardware IDs");
            if (!Guid.TryParse(d.ClassGuid ?? SetupApiInstaller.NetClass, out _))
                throw new FormatException($"Device {d.InstanceId} has a bad class GUID '{d.ClassGuid}'");
        }
        return job with
        {
            Devices = job.Devices.Select(d => d with { ClassGuid = d.ClassGuid ?? SetupApiInstaller.NetClass }).ToList(),
        };
    }
}

/// <summary>What the toolchain run did, for the server (result.json).</summary>
public sealed record ToolchainResult(bool Ok, IReadOnlyList<ToolchainDeviceResult> Devices, string? Error = null);

public static class Toolchain
{
    /// <summary>
    /// Installs every device of the job and writes result.json. The job is
    /// renamed to job.done first, so a crash during the install cannot make
    /// the image run it again on every boot.
    /// </summary>
    public static ToolchainResult Run(string folder, IDeviceInstaller installer, Action<string>? log = null)
    {
        ToolchainResult result;
        try
        {
            var job = ToolchainJob.Load(folder) ?? throw new FormatException("There is no toolchain job");
            File.Move(Path.Combine(folder, ToolchainJob.JobFile), Path.Combine(folder, ToolchainJob.DoneFile), true);
            var devices = new List<ToolchainDeviceResult>();
            foreach (var device in job.Devices)
            {
                ToolchainDeviceResult r;
                try
                {
                    r = installer.Install(device);
                }
                catch (Exception e)
                {
                    r = new ToolchainDeviceResult(device.InstanceId, false, Error: e.Message);
                }
                log?.Invoke(r.Ok ? $"Installed {r.InstanceId}: {r.Driver}" : $"Could not install {r.InstanceId}: {r.Error}");
                devices.Add(r);
            }
            result = new ToolchainResult(devices.All(d => d.Ok), devices);
        }
        catch (Exception e) when (e is FormatException or IOException or UnauthorizedAccessException)
        {
            log?.Invoke(e.Message);
            result = new ToolchainResult(false, [], e.Message);
        }
        File.WriteAllText(Path.Combine(folder, ToolchainJob.ResultFile), JsonSerializer.Serialize(result, Protocol.Json));
        return result;
    }
}

/// <summary>Runs the toolchain job in place of the normal agent work, then shuts the VM down.</summary>
public sealed class ToolchainWorker(
    IDeviceInstaller installer, IPowerControl power, ILogger<ToolchainWorker> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("Toolchain job found in {Folder}: installing new hardware", ToolchainJob.Folder);
        var result = await Task.Run(() => Toolchain.Run(ToolchainJob.Folder, installer,
            m => logger.LogInformation("{Message}", m)), stoppingToken);
        logger.LogInformation("Toolchain run {Outcome}; shutting down", result.Ok ? "succeeded" : "failed");
        await power.RunAsync("shutdown", stoppingToken);
    }
}
