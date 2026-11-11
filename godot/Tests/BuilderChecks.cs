using Godot;
using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PixelPipeline.Tests;

public static class BuilderChecks
{
    public static int Run()
    {
        int checks = 0;
        string prefix = "user://builder_checks_" + Guid.NewGuid().ToString("N");
        string json = prefix + ".json", sockets = prefix + "_sockets.json", saved = prefix + ".tres";
        void Check(bool condition, string message) { Expect(condition, message); checks++; }
        void Reject(Action action, string message)
        {
            try { action(); }
            catch (Exception ex) when (ex is InvalidDataException or ArgumentException or IOException)
            { checks++; return; }
            throw new Exception("Expected rejection: " + message);
        }
        void Write(string path, string text)
        {
            using var file = Godot.FileAccess.Open(path, Godot.FileAccess.ModeFlags.Write);
            Expect(file is not null, "create fixture " + path);
            file!.StoreString(text);
        }
        using var image = Image.CreateEmpty(96, 32, false, Image.Format.Rgba8);
        using var texture = ImageTexture.CreateFromImage(image);
        var frames = new[]
        {
            new AsepriteFrameData("a", new(0, 0, 32, 32), 180),
            new AsepriteFrameData("b", new(32, 0, 32, 32), 80),
            new AsepriteFrameData("c", new(64, 0, 32, 32), 120)
        };
        string Sheet(params AsepriteFrameTag[] tags) => JsonSerializer.Serialize(new AsepriteSheetRoot(frames.ToList(), new(tags.ToList())));
        void Reset() => Write(json, Sheet(new AsepriteFrameTag("idle", 0, 2, "forward")));
        void Mutate(Action<JsonObject> mutation)
        {
            var node = JsonNode.Parse(Sheet(new AsepriteFrameTag("idle", 0, 2, "forward")))!.AsObject();
            mutation(node); Write(json, node.ToJsonString());
        }
        void Build() => AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json);
        var socketData = Enumerable.Range(1, 3).Select(i => new GripSocketData(i, i, 16, i * 10)).ToArray();
        void SocketFixture(params GripSocketData[] values) => Write(sockets, JsonSerializer.Serialize(values));
        try
        {
            Reset(); SocketFixture(socketData);
            using var built = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json, saved);
            Check(built.GetAnimationSpeed("idle") == 10, "base FPS");
            Check(Math.Abs(built.GetFrameDuration("idle", 0) - 1.8) < 0.00001, "180 ms duration");
            Check(Math.Abs(built.GetFrameDuration("idle", 1) - 0.8) < 0.00001, "80 ms duration");
            Check(((AtlasTexture)built.GetFrameTexture("idle", 0)).FilterClip, "atlas filter clipping");
            Check(Godot.FileAccess.FileExists(saved), "saved tres exists");
            using var loaded = ResourceLoader.Load<SpriteFrames>(saved);
            Check(loaded is not null && loaded.GetFrameCount("idle") == 3, "saved tres reload");
            Reject(() => AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json, prefix + ".json"), "save extension");
            Reject(() => AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json, prefix + "/missing.tres"), "ResourceSaver error");
            Write(json, Sheet());
            using var defaults = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json);
            Check(defaults.GetFrameCount("default") == 3, "no-tag default");
            foreach (var item in new[] { ("forward", new[] { 1, 2, 3 }), ("reverse", new[] { 3, 2, 1 }), ("pingpong", new[] { 1, 2, 3, 2 }), ("pingpong_reverse", new[] { 3, 2, 1, 2 }) })
            {
                Write(json, Sheet(new AsepriteFrameTag("walk", 0, 2, item.Item1)));
                using var animation = AsepriteSpriteFramesBuilder.BuildOverlayFromMasterJson(texture, json);
                var order = AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets)["walk"];
                Check(order.Select(s => s.Frame).SequenceEqual(item.Item2), item.Item1 + " socket order");
                Check(animation.GetFrameCount("walk") == item.Item2.Length, item.Item1 + " length");
                for (int i = 0; i < order.Count; i++)
                    Check(((AtlasTexture)animation.GetFrameTexture("walk", i)).Region.Position.X == (order[i].Frame - 1) * 32, item.Item1 + " atlas order");
                Write(json, Sheet(new AsepriteFrameTag("walk", 1, 1, item.Item1)));
                using var single = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json);
                Check(single.GetFrameCount("walk") == 1, item.Item1 + " single frame");
            }
            foreach (string name in new[] { "idle", "walk", "run", "fly", "attack", "hit" })
            {
                Write(json, Sheet(new AsepriteFrameTag(name, 0, 0, "forward")));
                using var animation = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json);
                Check(animation.GetAnimationLoopMode(name) == (name is "idle" or "walk" or "run" or "fly"
                    ? SpriteFrames.LoopMode.Linear : SpriteFrames.LoopMode.None), name + " looping");
            }
            Write(json, Sheet(new AsepriteFrameTag("Idle", 0, 0, "forward")));
            using var mixedCase = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, json);
            Check(mixedCase.GetAnimationLoopMode("Idle") == SpriteFrames.LoopMode.Linear, "Idle loops case-insensitively");
            Check(mixedCase.HasAnimation("Idle") && !mixedCase.HasAnimation("idle"), "animation keys preserve case");
            foreach (double duration in new[] { 0d, -1d, 1e100, 1e-100 })
            { Mutate(n => n["frames"]![0]!["duration"] = duration); Reject(Build, "duration"); }
            foreach (string key in new[] { "w", "h", "x", "y" })
            { Mutate(n => n["frames"]![0]!["frame"]![key] = -1); Reject(Build, "rectangle " + key); }
            Mutate(n => n["frames"]![0]!["frame"]!["x"] = 96); Reject(Build, "texture bounds");
            foreach (string flag in new[] { "trimmed", "rotated" })
            { Mutate(n => n["frames"]![0]![flag] = true); Reject(Build, flag); }
            Write(json, Sheet(new AsepriteFrameTag("idle", 0, 3, "forward"))); Reject(Build, "tag bounds");
            Write(json, Sheet(new AsepriteFrameTag("idle", 0, 0, "forward"), new AsepriteFrameTag("idle", 1, 2, "forward"))); Reject(Build, "duplicate tag");
            Write(json, Sheet(new AsepriteFrameTag("idle", 0, 2, "unknown"))); Reject(Build, "direction");
            Write(json, """{"frames":{}}"""); Reject(Build, "array export required");
            Reset();
            using var tinyImage = Image.CreateEmpty(32, 32, false, Image.Format.Rgba8);
            using var tiny = ImageTexture.CreateFromImage(tinyImage);
            Reject(() => AsepriteSpriteFramesBuilder.BuildOverlayFromMasterJson(tiny, json), "overlay bounds");
            SocketFixture(socketData.Take(2).ToArray()); Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "missing socket");
            SocketFixture(socketData.Concat(new[] { socketData[0] }).ToArray()); Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "duplicate socket");
            SocketFixture(new GripSocketData(0, 0, 0, 0)); Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "one-based socket");
            SocketFixture(new GripSocketData(4, 0, 0, 0)); Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "socket range");
            foreach (var point in new[] { (-1f, 16f), (16f, -1f), (32f, 16f), (16f, 32f), (1.5f, 16f), (16f, 1.5f) })
            {
                SocketFixture(new GripSocketData(1, point.Item1, point.Item2, 0), socketData[1], socketData[2]);
                Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "socket integer pixel bounds");
            }
            SocketFixture(new GripSocketData(1, 0, 31, 0), socketData[1], socketData[2]);
            Check(AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets)["idle"][0].GripY == 31, "socket inclusive zero and exclusive canvas bounds");
            Write(sockets, "{}"); Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "socket array");
            Write(sockets, """[{"frame":1,"grip_x":0,"grip_y":0,"angle_deg":1e100}]""");
            Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "nonfinite socket angle");
            Write(sockets, """[{"frame":1}]"""); Reject(() => AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(json, sockets), "missing socket fields");
            const string demo = "res://assets/paperdoll/";
            using var demoTexture = ResourceLoader.Load<Texture2D>(demo + "char_sheet.png");
            Expect(demoTexture is not null, "demo character texture import");
            using var demoFrames = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(demoTexture!, demo + "char_sheet.json");
            var demoSockets = AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(demo + "char_sheet.json", demo + "grip_sockets.json");
            Check(demoFrames.GetAnimationNames().Length == 4, "demo four tags");
            Check(Math.Abs(demoFrames.GetFrameDuration("idle", 0) - 1.8) < 0.00001, "demo duration");
            foreach (string name in demoFrames.GetAnimationNames())
                Check(demoSockets[name].Count == demoFrames.GetFrameCount(name), "demo socket alignment " + name);
            return checks;
        }
        finally
        {
            foreach (string path in new[] { json, sockets, saved })
                if (Godot.FileAccess.FileExists(path))
                    Expect(DirAccess.RemoveAbsolute(ProjectSettings.GlobalizePath(path)) == Error.Ok, "remove fixture " + path);
        }
    }

    private static void Expect(bool condition, string message)
    {
        if (!condition) throw new Exception("Builder check failed: " + message);
    }
}
