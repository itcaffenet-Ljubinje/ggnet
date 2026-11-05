using System.Text;
using System.Text.Json;
using GgnetScan;

namespace GgnetAgent.Tests;

public class ScanTests
{
    [Fact]
    public void Reads_the_config_the_server_writes()
    {
        var config = ScanConfig.Parse("""
            {"server": "http://192.168.0.40:8088", "mac": "4c:ed:fb:67:96:6b", "drivers": ["net1ic64.inf"]}
            """);
        Assert.Equal("4c:ed:fb:67:96:6b", config.Mac);
        Assert.Equal(["net1ic64.inf"], config.Drivers);
        Assert.Equal("http://192.168.0.40:8088/api/v1/boot/scan", config.ReportUrl.ToString());
    }

    [Theory]
    [InlineData("not json")]
    [InlineData("""{"server": "ftp://x", "mac": "aa"}""")]
    [InlineData("""{"server": "http://x:8088"}""")]
    [InlineData("""{"server": "http://x", "mac": "aa", "drivers": ["..\\evil.inf"]}""")]
    [InlineData("""{"server": "http://x", "mac": "aa", "drivers": ["setup.exe"]}""")]
    public void Refuses_a_bad_config(string json) => Assert.Throws<FormatException>(() => ScanConfig.Parse(json));

    [Fact]
    public void Splits_hardware_ids_as_setupapi_returns_them()
    {
        var bytes = Encoding.Unicode.GetBytes("PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\0PCI\\VEN_8086&DEV_15BC\0\0");
        Assert.Equal(["PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10", "PCI\\VEN_8086&DEV_15BC"], PciDevices.SplitMultiSz(bytes));
    }

    [Fact]
    public void The_report_uses_the_servers_names()
    {
        var report = new ScanReport("aa:bb:cc:dd:ee:ff", "0.1.0",
            [new ScanDevice("PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\\3&11583659&0&FE",
                ["PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10"], [], PciDevices.NetClass, "Intel", null)],
            [new ScanAdapter("4c:ed:fb:67:96:6b", "Intel", true)]);
        var json = JsonDocument.Parse(JsonSerializer.Serialize(report, ScanJson.Default.ScanReport)).RootElement;
        Assert.Equal("0.1.0", json.GetProperty("scanner_version").GetString());
        var device = json.GetProperty("devices")[0];
        Assert.StartsWith("PCI\\VEN_8086", device.GetProperty("instance_id").GetString());
        Assert.Equal(PciDevices.NetClass, device.GetProperty("class_guid").GetString());
        Assert.True(json.GetProperty("adapters")[0].GetProperty("has_address").GetBoolean());
    }
}

public class ScanNetworkTests
{
    [Fact]
    public void A_card_without_a_driver_is_found_by_its_class_code()
    {
        var bare = new ScanDevice("PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\\3&11583659&0&FE",
            ["PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10"], ["PCI\\VEN_8086&CC_020000"], null, null, null);
        var gpu = bare with { CompatibleIds = ["PCI\\VEN_10DE&CC_030000"] };
        Assert.True(bare.IsNetwork);
        Assert.False(gpu.IsNetwork);
    }
}

