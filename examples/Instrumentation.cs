using System.Text.Json;

// Minimal local-only example. Pass aggregate usage explicitly; never serialize
// prompts, responses, headers, credentials, or tool arguments.
public static class TokenLensEvents
{
    public static void Emit(string path, IDictionary<string, object> body)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var payload = new Dictionary<string, object>(body)
        {
            ["schema_version"] = 2,
            ["event_id"] = Guid.NewGuid().ToString("N"),
            ["timestamp"] = DateTimeOffset.UtcNow
        };
        File.AppendAllText(path, JsonSerializer.Serialize(payload) + Environment.NewLine);
    }
}
