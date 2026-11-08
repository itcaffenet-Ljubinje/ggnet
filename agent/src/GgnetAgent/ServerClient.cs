using System.Net;
using System.Net.Http.Json;

namespace GgnetAgent;

public sealed class ServerException(string message) : Exception(message);

/// <summary>HTTP client for the ggNet backend.</summary>
public sealed class ServerClient(HttpClient http)
{
    public async Task<AgentConfig> HeartbeatAsync(Heartbeat beat, CancellationToken ct)
    {
        using var response = await http.PostAsJsonAsync(
            "api/v1/agent/heartbeat", beat, Protocol.Json, ct);

        if (response.StatusCode == HttpStatusCode.NotFound)
        {
            throw new ServerException(
                $"Machine '{beat.Name}' is not registered in ggNet; add it in the web UI");
        }
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            throw new ServerException($"Heartbeat failed: HTTP {(int)response.StatusCode} {body}");
        }
        return await response.Content.ReadFromJsonAsync<AgentConfig>(Protocol.Json, ct)
            ?? throw new ServerException("Heartbeat answer was empty");
    }

    /// <summary>
    /// A file of the server's agent package (Settings -> Client app). Read as
    /// a stream: the client's timeout only covers the answer's headers, the
    /// caller's token the download.
    /// </summary>
    public async Task DownloadAsync(string name, Stream output, CancellationToken ct)
    {
        using var response = await http.GetAsync(
            $"api/v1/agent/download/{Uri.EscapeDataString(name)}", HttpCompletionOption.ResponseHeadersRead, ct);
        if (!response.IsSuccessStatusCode)
        {
            throw new ServerException($"Downloading {name} failed: HTTP {(int)response.StatusCode}");
        }
        await using var input = await response.Content.ReadAsStreamAsync(ct);
        await input.CopyToAsync(output, ct);
    }
}
