using System.Diagnostics;
using System.Net.Http.Json;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using GgnetScan;

// ggNet hardware scan: runs in WinPE (wimboot puts it, ggnet-scan.json and the
// network card driver into X:\Windows\System32, winpeshl.ini starts it). It
// loads the driver, starts the network, sends the PC's PCI devices to the
// server and restarts the PC; the server then adds the network card to the
// PC's OS image (Add new hardware) and boots it.

var version = typeof(ScanConfig).Assembly.GetName().Version?.ToString(3) ?? "0";
Console.Title = "ggNet hardware scan";
Say($"ggNet hardware scan {version}");

var folder = AppContext.BaseDirectory;
ScanConfig config;
try
{
    config = ScanConfig.Parse(File.ReadAllText(Path.Combine(folder, "ggnet-scan.json")));
}
catch (Exception e) when (e is IOException or FormatException or UnauthorizedAccessException)
{
    Say($"Cannot start: {e.Message}");
    Finish(60);
    return 1;
}

foreach (var driver in config.Drivers ?? [])
{
    Say($"Loading network driver {driver}");
    Run("drvload.exe", $"\"{Path.Combine(folder, driver)}\"", TimeSpan.FromMinutes(2));
}
Say("Starting the network");
Run("wpeinit.exe", "", TimeSpan.FromMinutes(3));

var devices = PciDevices.Present();
var nics = devices.Where(d => d.IsNetwork).ToList();
foreach (var nic in nics)
    Say($"Network card: {nic.InstanceId}{(nic.Service is null ? " (no driver)" : "")}");

var report = new ScanReport(config.Mac, version, devices, Adapters());
string? answer = null;
var deadline = DateTime.UtcNow.AddSeconds(90);
using (var http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) })
{
    while (answer is null && DateTime.UtcNow < deadline)
    {
        try
        {
            var response = await http.PostAsJsonAsync(config.ReportUrl, report, ScanJson.Default.ScanReport);
            response.EnsureSuccessStatusCode();
            answer = (await response.Content.ReadFromJsonAsync(ScanJson.Default.ScanAnswer))?.Message ?? "";
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or System.Text.Json.JsonException)
        {
            Say($"Waiting for the network ({e.Message})");
            await Task.Delay(TimeSpan.FromSeconds(5));
        }
    }
}

if (answer is null)
{
    Say("The ggNet server could not be reached. In the web UI, Images -> the OS image -> Add new hardware,");
    Say("enter this PC's network card:");
    foreach (var nic in nics)
        Say($"    {nic.InstanceId}");
    Finish(300);
    return 2;
}
Say(answer);
Finish(5);
return 0;

static void Say(string text) => Console.WriteLine($"{DateTime.Now:HH:mm:ss}  {text}");

static List<ScanAdapter> Adapters()
{
    try
    {
        return NetworkInterface.GetAllNetworkInterfaces()
            .Where(n => n.NetworkInterfaceType is not (NetworkInterfaceType.Loopback or NetworkInterfaceType.Tunnel))
            .Select(n => new ScanAdapter(
                string.Join(":", n.GetPhysicalAddress().GetAddressBytes().Select(b => b.ToString("x2"))),
                n.Description,
                n.GetIPProperties().UnicastAddresses.Any(a => a.Address.AddressFamily == AddressFamily.InterNetwork)))
            .ToList();
    }
    catch (NetworkInformationException)
    {
        return [];
    }
}

static void Run(string exe, string arguments, TimeSpan timeout)
{
    try
    {
        using var process = Process.Start(new ProcessStartInfo(exe, arguments) { UseShellExecute = false });
        if (process is not null && !process.WaitForExit(timeout))
            process.Kill();
    }
    catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException)
    {
        Say($"{exe} failed: {e.Message}");
    }
}

// Restart the PC (WinPE): the server boots it on with what it learned.
static void Finish(int seconds)
{
    Say($"Restarting in {seconds} s");
    Thread.Sleep(TimeSpan.FromSeconds(seconds));
    Run("wpeutil.exe", "reboot", TimeSpan.FromMinutes(1));
}
