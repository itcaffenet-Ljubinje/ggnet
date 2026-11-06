using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

/// <summary>In-memory UEFI variables.</summary>
internal sealed class FakeUefi : IUefiVariables
{
    public bool IsUefi { get; set; } = true;
    public Dictionary<string, byte[]> Variables { get; } = [];
    public Exception? FailWrite { get; set; }
    public int Writes { get; private set; }

    public byte[]? Read(string name, Guid vendor) =>
        Variables.TryGetValue($"{name}-{vendor}", out var data) ? data : null;

    public void Write(string name, Guid vendor, byte[] data)
    {
        if (FailWrite is not null) throw FailWrite;
        Writes++;
        Variables[$"{name}-{vendor}"] = data;
    }
}

public class MokEnrollmentTests
{
    // Any bytes do for the format; a real DER certificate starts with 30 82.
    private static readonly byte[] Cert = [0x30, 0x82, 0x03, 0x53, 0xAA, 0xBB, 0xCC];
    private const string ShimLock = "605dab50-e046-4300-abb6-3dd810dd8b23";

    [Fact]
    public void The_request_is_one_x509_signature_list_owned_by_shim_as_mokutil_writes_it()
    {
        var list = Mok.SignatureList(Cert);
        byte[] expected =
        [
            // SignatureType EFI_CERT_X509_GUID a5c059a1-94e4-4aa7-87b5-ab155c2bf072, EFI byte order
            0xa1, 0x59, 0xc0, 0xa5, 0xe4, 0x94, 0xa7, 0x4a, 0x87, 0xb5, 0xab, 0x15, 0x5c, 0x2b, 0xf0, 0x72,
            51, 0, 0, 0,     // SignatureListSize: 28 + 16 + 7
            0, 0, 0, 0,      // SignatureHeaderSize
            23, 0, 0, 0,     // SignatureSize: owner + certificate
            // SignatureOwner: shim lock GUID 605dab50-e046-4300-abb6-3dd810dd8b23
            0x50, 0xab, 0x5d, 0x60, 0x46, 0xe0, 0x00, 0x43, 0xab, 0xb6, 0x3d, 0xd8, 0x10, 0xdd, 0x8b, 0x23,
            .. Cert,
        ];
        Assert.Equal(expected, list);
    }

    [Fact]
    public void The_auth_is_sha256_of_the_request_and_the_password_in_ucs2()
    {
        var list = Mok.SignatureList(Cert);
        var expected = SHA256.HashData([.. list, .. Encoding.Unicode.GetBytes("ggnet")]);
        Assert.Equal(expected, Mok.Auth(list, "ggnet"));
        Assert.Equal(32, expected.Length);
    }

    [Fact]
    public void Finds_the_certificate_among_other_signature_lists()
    {
        byte[] sha256List = [.. new Guid("c1c41626-504c-4092-aca9-41f936934328").ToByteArray(),
            .. BitConverter.GetBytes(28u + 48u), .. BitConverter.GetBytes(0u), .. BitConverter.GetBytes(48u),
            .. new byte[48]];
        byte[] other = Mok.SignatureList([0x30, 0x82, 0x01]);
        Assert.True(Mok.Contains([.. sha256List, .. other, .. Mok.SignatureList(Cert)], Cert));
        Assert.False(Mok.Contains([.. sha256List, .. other], Cert));
        Assert.False(Mok.Contains(null, Cert));
        Assert.False(Mok.Contains([1, 2, 3], Cert));                       // garbage, not a crash
        Assert.False(Mok.Contains([.. Mok.SignatureList(Cert)[..30]], Cert));  // cut short
    }

    [Fact]
    public void Asks_for_the_key_once_and_reports_it_pending()
    {
        var uefi = new FakeUefi();
        var mok = new MokEnrollment(uefi);
        Assert.Equal(MokState.Requested, mok.Ensure(Cert, "ggnet"));
        Assert.Equal(Mok.SignatureList(Cert), uefi.Variables[$"MokNew-{ShimLock}"]);
        Assert.Equal(Mok.Auth(Mok.SignatureList(Cert), "ggnet"), uefi.Variables[$"MokAuth-{ShimLock}"]);
        Assert.Equal(MokState.Requested, mok.Ensure(Cert, "ggnet"));
        Assert.Equal(2, uefi.Writes);                                       // not written again
        Assert.Equal(MokState.Requested, mok.Ensure(Cert, "other"));       // a new password: asked again
        Assert.Equal(4, uefi.Writes);
    }

    [Fact]
    public void An_enrolled_key_is_left_alone()
    {
        var uefi = new FakeUefi();
        uefi.Variables[$"MokListRT-{ShimLock}"] = [.. Mok.SignatureList([0x30, 0x01]), .. Mok.SignatureList(Cert)];
        Assert.Equal(MokState.Enrolled, new MokEnrollment(uefi).Ensure(Cert, "ggnet"));
        Assert.Equal(0, uefi.Writes);
    }

    [Fact]
    public void Nothing_on_a_bios_boot()
    {
        var uefi = new FakeUefi { IsUefi = false };
        Assert.Null(new MokEnrollment(uefi).Ensure(Cert, "ggnet"));
        Assert.Equal(0, uefi.Writes);
    }

    // ── In the agent ──────────────────────────────────────────────────

    private readonly FakeHandler _server = new();
    private readonly FakeUefi _uefi = new();

    private AgentWorker Worker() => new(
        _server.Client(),
        new FakeIscsi(),
        Options.Create(new AgentOptions { ServerUrl = "http://192.168.0.10:8088" }),
        NullLogger<AgentWorker>.Instance,
        TimeProvider.System,
        uefi: _uefi);

    private void Config(string mode, bool key) => _server.Body = ConfigJson
        .Replace("\"mode\": \"disk\"", $"\"mode\": \"{mode}\"")
        .Replace("\"game_image\": \"cs2\"", "\"game_image\": \"cs2\"" + (key
            ? $", \"secure_boot\": {{\"certificate\": \"{Convert.ToBase64String(Cert)}\", \"password\": \"ggnet\"}}"
            : ""));

    private string? SentMok(int beat) =>
        JsonDocument.Parse(_server.Requests[beat].Body).RootElement.GetProperty("mok").GetString();

    [Fact]
    public async Task Boot_mode_asks_for_the_key_once_and_reports_it()
    {
        Config("boot", key: true);
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(2, _uefi.Writes);
        Assert.Null(SentMok(0));                                    // asked after the first answer
        Assert.Equal(MokState.Requested, SentMok(1));
        Assert.Equal(MokState.Requested, SentMok(2));
    }

    [Theory]
    [InlineData("disk", true)]     // Disk Mode: the PC boots its own Windows, not shim
    [InlineData("boot", false)]    // the server does not ask for it
    public async Task Otherwise_the_variables_are_not_touched(string mode, bool key)
    {
        Config(mode, key);
        await Worker().TickAsync(CancellationToken.None);
        await Worker().TickAsync(CancellationToken.None);
        Assert.Equal(0, _uefi.Writes);
    }

    [Fact]
    public async Task A_failure_is_reported_and_heartbeats_go_on()
    {
        Config("boot", key: true);
        _uefi.FailWrite = new UnauthorizedAccessException("privilege not held");
        var worker = Worker();
        await worker.TickAsync(CancellationToken.None);
        await worker.TickAsync(CancellationToken.None);
        Assert.Equal(MokState.Failed, SentMok(1));
        Assert.Equal(2, _server.Requests.Count);
    }
}
