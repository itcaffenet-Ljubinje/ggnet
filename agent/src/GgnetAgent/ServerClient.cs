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
}
