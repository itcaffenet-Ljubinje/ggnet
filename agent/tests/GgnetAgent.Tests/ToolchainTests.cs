using System.Text.Json;

namespace GgnetAgent.Tests;

public sealed class ToolchainTests : IDisposable
{
    private const string I219 = @"PCI\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\3&11583659&0&FE";
    private readonly string _folder = Directory.CreateTempSubdirectory("ggnet-toolchain-").FullName;

    public void Dispose() => Directory.Delete(_folder, true);

    private const string JobJson = """
        {"devices": [{"instance_id": "PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\\3&11583659&0&FE",
                      "hardware_ids": ["PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10",
                                       "PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043"],
                      "compatible_ids": ["PCI\\VEN_8086&DEV_15BC&REV_10", "PCI\\VEN_8086&DEV_15BC"]}]}
        """;

    private sealed class FakeInstaller(Func<ToolchainDevice, ToolchainDeviceResult>? answer = null) : IDeviceInstaller
    {
        public List<ToolchainDevice> Installed { get; } = [];

        public ToolchainDeviceResult Install(ToolchainDevice device)
        {
            Installed.Add(device);
            return answer?.Invoke(device) ?? new(device.InstanceId, true, "Intel Ethernet Connection (2) I219-V");
        }
    }

    [Fact]
    public void Parses_the_job_and_defaults_to_the_network_class()
    {
        var job = ToolchainJob.Parse(JobJson);
        var device = Assert.Single(job.Devices);
        Assert.Equal(I219, device.InstanceId);
        Assert.Equal(2, device.HardwareIds.Count);
        Assert.Equal(2, device.CompatibleIds!.Count);
        Assert.Equal(SetupApiInstaller.NetClass, device.ClassGuid);
    }

    [Theory]
    [InlineData("not json")]
    [InlineData("""{"devices": []}""")]
    [InlineData("""{"devices": [{"instance_id": "no-backslash", "hardware_ids": ["PCI\\VEN_1"]}]}""")]
    [InlineData("""{"devices": [{"instance_id": "PCI\\VEN_1\\0", "hardware_ids": []}]}""")]
    [InlineData("""{"devices": [{"instance_id": "PCI\\VEN_1\\0", "hardware_ids": ["PCI\\VEN_1"], "class_guid": "x"}]}""")]
    public void Rejects_a_bad_job(string json) => Assert.Throws<FormatException>(() => ToolchainJob.Parse(json));

    [Fact]
    public void No_job_means_a_normal_start() => Assert.Null(ToolchainJob.Load(_folder));

    [Fact]
    public void Installs_every_device_and_writes_the_result()
    {
        File.WriteAllText(Path.Combine(_folder, ToolchainJob.JobFile), JobJson);
        var installer = new FakeInstaller();

        var result = Toolchain.Run(_folder, installer);

        Assert.True(result.Ok);
        Assert.Equal(I219, Assert.Single(installer.Installed).InstanceId);
        // The job runs once: a later boot of the image is a normal start.
        Assert.False(File.Exists(Path.Combine(_folder, ToolchainJob.JobFile)));
        Assert.True(File.Exists(Path.Combine(_folder, ToolchainJob.DoneFile)));
        var written = JsonDocument.Parse(File.ReadAllText(Path.Combine(_folder, ToolchainJob.ResultFile))).RootElement;
        Assert.True(written.GetProperty("ok").GetBoolean());
        Assert.Equal(I219, written.GetProperty("devices")[0].GetProperty("instance_id").GetString());
        Assert.Equal("Intel Ethernet Connection (2) I219-V", written.GetProperty("devices")[0].GetProperty("driver").GetString());
    }

    [Fact]
    public void A_device_that_fails_is_reported_and_the_rest_still_installed()
    {
        var json = JobJson.Replace("]}]}", """]}, {"instance_id": "PCI\\VEN_10EC&DEV_8125\\01000000684CE00000", "hardware_ids": ["PCI\\VEN_10EC&DEV_8125"]}]}""");
        File.WriteAllText(Path.Combine(_folder, ToolchainJob.JobFile), json);
        var installer = new FakeInstaller(d => d.InstanceId == I219
            ? throw new InvalidOperationException("boom")
            : new(d.InstanceId, true, "Realtek"));

        var result = Toolchain.Run(_folder, installer);

        Assert.False(result.Ok);
        Assert.Equal(2, installer.Installed.Count);
        Assert.Equal("boom", result.Devices[0].Error);
        Assert.True(result.Devices[1].Ok);
    }

    [Fact]
    public void A_bad_job_is_reported_in_the_result()
    {
        File.WriteAllText(Path.Combine(_folder, ToolchainJob.JobFile), "{}");
        var result = Toolchain.Run(_folder, new FakeInstaller());
        Assert.False(result.Ok);
        Assert.Contains("no devices", result.Error);
        Assert.True(File.Exists(Path.Combine(_folder, ToolchainJob.ResultFile)));
    }
}
