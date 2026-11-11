using Godot;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace PixelPipeline;

public sealed record AsepriteRect(
    [property: JsonPropertyName("x")] int X,
    [property: JsonPropertyName("y")] int Y,
    [property: JsonPropertyName("w")] int W,
    [property: JsonPropertyName("h")] int H);
public sealed record AsepriteFrameData(
    [property: JsonPropertyName("filename")] string Filename,
    [property: JsonPropertyName("frame")] AsepriteRect Frame,
    [property: JsonPropertyName("duration")] double Duration)
{
    [JsonPropertyName("trimmed")] public bool Trimmed { get; init; }
    [JsonPropertyName("rotated")] public bool Rotated { get; init; }
}
public sealed record AsepriteFrameTag(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("from")] int From,
    [property: JsonPropertyName("to")] int To,
    [property: JsonPropertyName("direction")] string Direction);
public sealed record AsepriteMeta([property: JsonPropertyName("frameTags")] List<AsepriteFrameTag> FrameTags);
public sealed record AsepriteSheetRoot(
    [property: JsonPropertyName("frames")] List<AsepriteFrameData> Frames,
    [property: JsonPropertyName("meta")] AsepriteMeta Meta);
public sealed record GripSocketData(
    [property: JsonPropertyName("frame")] int Frame,
    [property: JsonPropertyName("grip_x")] float GripX,
    [property: JsonPropertyName("grip_y")] float GripY,
    [property: JsonPropertyName("angle_deg")] float AngleDeg);

public static class AsepriteSpriteFramesBuilder
{
    public const double BaseFps = 10;
    private static readonly HashSet<string> LoopingAnimations = new(StringComparer.OrdinalIgnoreCase)
    { "idle", "walk", "run", "fly", "default" };

    public static SpriteFrames BuildFromAsepriteJson(Texture2D sheetTexture, string jsonPath, string saveTresPath = "")
    {
        ArgumentNullException.ThrowIfNull(sheetTexture);
        if (!string.IsNullOrEmpty(saveTresPath) && !saveTresPath.EndsWith(".tres", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("SpriteFrames save path must end in .tres.", nameof(saveTresPath));
        var root = ReadSheet(jsonPath);
        ValidateTexture(root, sheetTexture, jsonPath);
        var result = new SpriteFrames();
        result.RemoveAnimation("default");
        foreach (var tag in Tags(root))
        {
            result.AddAnimation(tag.Name);
            result.SetAnimationSpeed(tag.Name, BaseFps);
            result.SetAnimationLoopMode(tag.Name, LoopingAnimations.Contains(tag.Name)
                ? SpriteFrames.LoopMode.Linear : SpriteFrames.LoopMode.None);
            foreach (int index in Expand(tag))
            {
                var frame = root.Frames[index];
                var rect = frame.Frame;
                var atlas = new AtlasTexture
                {
                    Atlas = sheetTexture,
                    Region = new Rect2(rect.X, rect.Y, rect.W, rect.H),
                    FilterClip = true
                };
                result.AddFrame(tag.Name, atlas, (float)(frame.Duration / 100d));
            }
        }
        if (!string.IsNullOrEmpty(saveTresPath))
        {
            Error error = ResourceSaver.Save(result, saveTresPath);
            if (error != Error.Ok)
                throw new IOException($"Cannot save SpriteFrames to '{saveTresPath}': {error}.");
        }
        return result;
    }

    public static SpriteFrames BuildOverlayFromMasterJson(Texture2D overlaySheetTexture, string masterCharacterJsonPath)
        => BuildFromAsepriteJson(overlaySheetTexture, masterCharacterJsonPath);

    public static Dictionary<string, List<GripSocketData>> LoadGripSocketsByTag(string characterJsonPath, string socketsJsonPath)
    {
        var root = ReadSheet(characterJsonPath);
        using var document = ReadDocument(socketsJsonPath);
        if (document.RootElement.ValueKind != JsonValueKind.Array)
            throw Invalid(socketsJsonPath, "socket root must be a JSON array");
        foreach (var element in document.RootElement.EnumerateArray())
            Require(element, socketsJsonPath, "frame", "grip_x", "grip_y", "angle_deg");
        var sockets = Deserialize<List<GripSocketData>>(document.RootElement, socketsJsonPath);
        var byFrame = new Dictionary<int, GripSocketData>();
        foreach (var socket in sockets)
        {
            if (socket is null || socket.Frame < 1 || socket.Frame > root.Frames.Count)
                throw Invalid(socketsJsonPath, $"socket frame must be 1..{root.Frames.Count}");
            if (!float.IsFinite(socket.GripX) || !float.IsFinite(socket.GripY) || !float.IsFinite(socket.AngleDeg))
                throw Invalid(socketsJsonPath, $"socket frame {socket.Frame} has nonfinite coordinates or angle");
            var rect = root.Frames[socket.Frame - 1].Frame;
            if (socket.GripX != MathF.Truncate(socket.GripX) || socket.GripY != MathF.Truncate(socket.GripY)
                || socket.GripX < 0 || socket.GripX >= rect.W || socket.GripY < 0 || socket.GripY >= rect.H)
                throw Invalid(socketsJsonPath, $"socket frame {socket.Frame} grip ({socket.GripX}, {socket.GripY}) must be integer pixels within 0 <= x < {rect.W}, 0 <= y < {rect.H}");
            if (!byFrame.TryAdd(socket.Frame, socket))
                throw Invalid(socketsJsonPath, $"duplicate socket for frame {socket.Frame}");
        }
        for (int i = 1; i <= root.Frames.Count; i++)
            if (!byFrame.ContainsKey(i)) throw Invalid(socketsJsonPath, $"missing socket for frame {i}");
        return Tags(root).ToDictionary(t => t.Name, t => Expand(t).Select(i => byFrame[i + 1]).ToList(), StringComparer.Ordinal);
    }

    private static JsonDocument ReadDocument(string path)
    {
        using var file = Godot.FileAccess.Open(path, Godot.FileAccess.ModeFlags.Read);
        if (file is null) throw new IOException($"Cannot open '{path}': {Godot.FileAccess.GetOpenError()}.");
        try { return JsonDocument.Parse(file.GetAsText()); }
        catch (JsonException ex) { throw Invalid(path, $"invalid JSON: {ex.Message}"); }
    }

    private static T Deserialize<T>(JsonElement element, string path)
    {
        try { return element.Deserialize<T>() ?? throw Invalid(path, "JSON value cannot be null"); }
        catch (JsonException ex) { throw Invalid(path, $"invalid JSON structure: {ex.Message}"); }
    }

    private static void Require(JsonElement element, string path, params string[] names)
    {
        if (element.ValueKind != JsonValueKind.Object) throw Invalid(path, "expected JSON object");
        foreach (string name in names)
            if (!element.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null)
                throw Invalid(path, $"required property '{name}' is missing or null");
    }

    private static AsepriteSheetRoot ReadSheet(string path)
    {
        using var document = ReadDocument(path);
        Require(document.RootElement, path, "frames");
        var frames = document.RootElement.GetProperty("frames");
        if (frames.ValueKind != JsonValueKind.Array || frames.GetArrayLength() == 0)
            throw Invalid(path, "frames must be a nonempty JSON array (export Aseprite JSON Array)");
        foreach (var frame in frames.EnumerateArray())
        {
            Require(frame, path, "filename", "frame", "duration");
            Require(frame.GetProperty("frame"), path, "x", "y", "w", "h");
        }
        if (document.RootElement.TryGetProperty("meta", out var meta) && meta.ValueKind != JsonValueKind.Null)
        {
            if (meta.ValueKind != JsonValueKind.Object) throw Invalid(path, "meta must be an object");
            if (meta.TryGetProperty("frameTags", out var tags))
            {
                if (tags.ValueKind != JsonValueKind.Array) throw Invalid(path, "frameTags must be an array");
                foreach (var tag in tags.EnumerateArray()) Require(tag, path, "name", "from", "to", "direction");
            }
        }
        var root = Deserialize<AsepriteSheetRoot>(document.RootElement, path);
        for (int i = 0; i < root.Frames.Count; i++)
        {
            var frame = root.Frames[i];
            var r = frame.Frame;
            if (string.IsNullOrWhiteSpace(frame.Filename) || r.X < 0 || r.Y < 0 || r.W <= 0 || r.H <= 0)
                throw Invalid(path, $"frame {i} has invalid filename or rectangle");
            if (!double.IsFinite(frame.Duration) || frame.Duration <= 0)
                throw Invalid(path, $"frame {i} duration must be finite and positive milliseconds");
            float relativeDuration = (float)(frame.Duration / 100d);
            if (!float.IsFinite(relativeDuration) || relativeDuration <= 0)
                throw Invalid(path, $"frame {i} duration cannot be represented as a positive finite Godot frame duration");
            if (frame.Trimmed || frame.Rotated)
                throw Invalid(path, $"frame {i} is trimmed or rotated; export with trimming and rotation disabled");
        }
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var tag in Tags(root))
        {
            if (tag is null || string.IsNullOrWhiteSpace(tag.Name) || !names.Add(tag.Name))
                throw Invalid(path, "animation tag names must be nonempty and unique");
            if (tag.From < 0 || tag.To < tag.From || tag.To >= root.Frames.Count)
                throw Invalid(path, $"tag '{tag.Name}' bounds {tag.From}..{tag.To} exceed frame array");
            if (tag.Direction is not ("forward" or "reverse" or "pingpong" or "pingpong_reverse"))
                throw Invalid(path, $"tag '{tag.Name}' has unsupported direction '{tag.Direction}'");
        }
        return root;
    }

    private static List<AsepriteFrameTag> Tags(AsepriteSheetRoot root)
        => root.Meta?.FrameTags is { Count: > 0 } tags ? tags : new() { new("default", 0, root.Frames.Count - 1, "forward") };

    // Endpoints occur once per cycle, including in reverse ping-pong playback.
    private static IEnumerable<int> Expand(AsepriteFrameTag tag)
    {
        bool reverse = tag.Direction is "reverse" or "pingpong_reverse";
        int start = reverse ? tag.To : tag.From;
        int end = reverse ? tag.From : tag.To;
        int step = reverse ? -1 : 1;
        for (int i = start; ; i += step)
        {
            yield return i;
            if (i == end) break;
        }
        if (tag.Direction is "pingpong" or "pingpong_reverse")
            for (int i = end - step; reverse ? i < start : i > start; i -= step) yield return i;
    }

    private static void ValidateTexture(AsepriteSheetRoot root, Texture2D texture, string path)
    {
        foreach (var frame in root.Frames)
        {
            var r = frame.Frame;
            if ((long)r.X + r.W > texture.GetWidth() || (long)r.Y + r.H > texture.GetHeight())
                throw Invalid(path, $"frame '{frame.Filename}' rectangle exceeds texture {texture.GetWidth()}x{texture.GetHeight()}");
        }
    }

    private static InvalidDataException Invalid(string path, string reason) => new($"'{path}': {reason}.");
}
