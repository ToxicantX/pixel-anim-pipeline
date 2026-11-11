using Godot;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

namespace PixelPipeline.Tests;

/// <summary>
/// Behavioural checks for <see cref="CharacterPaperdoll"/> against the real prefab and the real
/// paperdoll assets. Returns the number of passing assertions; any failure throws.
/// </summary>
public static class ControllerChecks
{
    private const string PrefabPath = "res://Scenes/CharacterPaperdoll.tscn";
    private const string CharPng = "res://assets/paperdoll/char_sheet.png";
    private const string CharJson = "res://assets/paperdoll/char_sheet.json";
    private const string SocketsJson = "res://assets/paperdoll/grip_sockets.json";
    private const string SwordSheetPng = "res://assets/paperdoll/weapon_sword_sheet.png";
    private const string AxeSheetPng = "res://assets/paperdoll/weapon_axe_sheet.png";
    private const string AxeCleanPng = "res://assets/paperdoll/axe_clean.png";
    private const string SwordCleanPng = "res://assets/paperdoll/sword_clean.png";
    private const string Idle = "idle";
    private const string Walk = "walk";
    private const string Attack = "attack";
    private const string Hit = "hit";

    public static async Task<int> Run(Node host)
    {
        await Task.CompletedTask;

        int checks = 0;
        var tempFiles = new List<string>();
        var dolls = new List<CharacterPaperdoll>();
        var extraParents = new List<Node>();

        void Check(bool condition, string message)
        {
            if (!condition)
            {
                throw new Exception("Controller check failed: " + message);
            }

            checks++;
        }

        void CheckClose(float actual, float expected, string message)
            => Check(Mathf.Abs(actual - expected) <= 0.01f, $"{message} (got {actual}, expected {expected})");

        void CheckVector(Vector2 actual, Vector2 expected, string message)
            => Check(actual.DistanceTo(expected) <= 0.01f, $"{message} (got {actual}, expected {expected})");

        void ExpectThrow(Action action, string message)
        {
            try
            {
                action();
            }
            catch (Exception)
            {
                checks++;
                return;
            }

            throw new Exception("Controller check failed (expected a rejection): " + message);
        }

        int ConnectionCount(GodotObject emitter, StringName signal)
            => emitter.GetSignalConnectionList(signal).Count;

        PackedScene? prefab = ResourceLoader.Load<PackedScene>(PrefabPath);
        Check(prefab is not null, "CharacterPaperdoll prefab loads");

        CharacterPaperdoll Spawn(Node parent)
        {
            var doll = prefab!.Instantiate<CharacterPaperdoll>();
            dolls.Add(doll);
            parent.AddChild(doll);
            doll.SetProcess(false); // the body clock is driven explicitly in these checks
            return doll;
        }

        Node2D SpawnParent()
        {
            var parent = new Node2D
            {
                Position = new Vector2(120f, 80f),
                RotationDegrees = 37f,
                Scale = new Vector2(2.5f, 1.5f)
            };
            host.AddChild(parent);
            extraParents.Add(parent);
            return parent;
        }

        string NewTempPath(string suffix)
        {
            string path = $"user://controller_checks_{Guid.NewGuid():N}{suffix}";
            tempFiles.Add(path);
            return path;
        }

        string WriteTemp(string suffix, string text)
        {
            string path = NewTempPath(suffix);
            using var file = FileAccess.Open(path, FileAccess.ModeFlags.Write);
            Check(file is not null, "create temporary fixture " + path);
            file!.StoreString(text);
            return path;
        }

        string ReadText(string path)
        {
            using var file = FileAccess.Open(path, FileAccess.ModeFlags.Read);
            Check(file is not null, "read temporary fixture " + path);
            return file!.GetAsText();
        }

        static string SheetJson(params AsepriteFrameTag[] tags)
        {
            var frames = new List<AsepriteFrameData>
            {
                new("frame0", new AsepriteRect(0, 0, 32, 32), 100),
                new("frame1", new AsepriteRect(32, 0, 32, 32), 100),
                new("frame2", new AsepriteRect(64, 0, 32, 32), 100),
                new("frame3", new AsepriteRect(96, 0, 32, 32), 100)
            };
            return JsonSerializer.Serialize(new AsepriteSheetRoot(frames, new AsepriteMeta(tags.ToList())));
        }

        static string SocketsJsonFor(int frameCount)
        {
            var sockets = new List<GripSocketData>();
            for (int frame = 1; frame <= frameCount; frame++)
            {
                sockets.Add(new GripSocketData(frame, 5, 6, 0));
            }

            return JsonSerializer.Serialize(sockets);
        }

        try
        {
            // ---------------------------------------------------------------- runtime bootstrap
            CharacterPaperdoll doll = Spawn(host);
            AnimatedSprite2D? body = doll.GetNodeOrNull<AnimatedSprite2D>("BodySprite");
            AnimatedSprite2D? sheetLayer = doll.GetNodeOrNull<AnimatedSprite2D>("WeaponSheetSprite");
            Node2D? pivot = doll.GetNodeOrNull<Node2D>("WeaponPivot");
            Sprite2D? dynamicWeapon = doll.GetNodeOrNull<Sprite2D>("WeaponPivot/DynamicWeaponSprite");
            AnimatedSprite2D? fxLayer = doll.GetNodeOrNull<AnimatedSprite2D>("FXSprite");

            Check(body is not null, "prefab exposes BodySprite");
            Check(sheetLayer is not null, "prefab exposes WeaponSheetSprite");
            Check(pivot is not null, "prefab exposes WeaponPivot");
            Check(dynamicWeapon is not null, "prefab exposes WeaponPivot/DynamicWeaponSprite");
            Check(fxLayer is not null, "prefab exposes FXSprite");

            SpriteFrames? bodyFrames = body!.SpriteFrames;
            Check(bodyFrames is not null, "runtime initialises body SpriteFrames");
            foreach (string name in new[] { Idle, Walk, Attack, Hit })
            {
                Check(bodyFrames!.HasAnimation(name) && bodyFrames.GetFrameCount(name) > 0,
                    $"body animation '{name}' is playable");
            }

            Check(body.Animation == Idle, "runtime auto-plays idle when it exists");
            Check(body.IsPlaying(), "body idle animation is playing");
            Check(doll.ProcessPriority == 1, "controller process priority is 1 so the body advances first");
            Check(body.ProcessPriority == 0, "body keeps its default process priority");
            Check(!body.Centered, "body sprite is not centred");
            Check(!body.FlipH, "body does not use FlipH (mirroring is explicit)");
            CheckVector(body.Position, new Vector2(-16f, -30f), "body position uses the canvas origin");
            Check(body.Scale == Vector2.One, "body scale is identity when facing right");
            Check(doll.Scale == Vector2.One, "root scale is untouched");
            Check(!fxLayer!.Visible, "optional FX layer stays hidden when it has no frames");

            // ---------------------------------------------------------------- default empty SpriteFrames
            CharacterPaperdoll emptyDoll = prefab!.Instantiate<CharacterPaperdoll>();
            dolls.Add(emptyDoll);
            AnimatedSprite2D? emptyBody = emptyDoll.GetNodeOrNull<AnimatedSprite2D>("BodySprite");
            Check(emptyBody is not null, "probe BodySprite before entering the tree");
            emptyBody!.SpriteFrames = new SpriteFrames(); // the non-null default empty set of a new AnimatedSprite2D
            host.AddChild(emptyDoll);
            emptyDoll.SetProcess(false);
            Check(emptyBody.SpriteFrames is not null && emptyBody.SpriteFrames.HasAnimation(Idle),
                "runtime replaces the default empty SpriteFrames with real body frames");
            Check(emptyBody.Animation == Idle && emptyBody.IsPlaying(),
                "doll with default empty frames boots into idle");
            Check(emptyDoll.ProcessPriority == 1, "replacement doll also keeps priority 1");

            // ---------------------------------------------------------------- preassigned valid frames kept
            Texture2D? presetTexture = ResourceLoader.Load<Texture2D>(CharPng);
            Check(presetTexture is not null, "character sheet texture is imported");
            SpriteFrames presetFrames = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(presetTexture!, CharJson);
            CharacterPaperdoll presetDoll = prefab!.Instantiate<CharacterPaperdoll>();
            dolls.Add(presetDoll);
            AnimatedSprite2D presetBody = presetDoll.GetNode<AnimatedSprite2D>("BodySprite");
            presetBody.SpriteFrames = presetFrames;
            host.AddChild(presetDoll);
            presetDoll.SetProcess(false);
            Check(ReferenceEquals(presetBody.SpriteFrames, presetFrames),
                "valid preassigned body frames are kept instead of rebuilt");

            // ---------------------------------------------------------------- overlay follows one clock
            doll.PlayAnimation(Walk);
            body.SetFrameAndProgress(2, 0.5f);
            doll.EquipWeaponSheet(SwordSheetPng);
            Check(sheetLayer!.Visible, "weapon sheet layer is visible after equipping a sheet");
            Check(dynamicWeapon!.Visible == false, "sheet mode hides the dynamic weapon sprite");
            Check(sheetLayer.Animation == Walk, "weapon sheet follows the body animation");
            Check(sheetLayer.Frame == 2, "weapon sheet follows the body frame");
            CheckClose(sheetLayer.FrameProgress, 0.5f, "weapon sheet follows the body frame progress");
            Check(body.Frame == 2, "equipping a sheet does not reset the body frame");
            CheckClose(body.FrameProgress, 0.5f, "equipping a sheet does not reset body progress");

            body.SetFrameAndProgress(5, 0.75f);
            doll.EquipWeaponSheet(AxeSheetPng);
            Check(body.Frame == 5, "mid-animation weapon swap keeps the body frame");
            CheckClose(body.FrameProgress, 0.75f, "mid-animation weapon swap keeps the body progress");
            doll._Process(0.016);
            Check(sheetLayer.Frame == 5 && Mathf.IsEqualApprox(sheetLayer.FrameProgress, 0.75f),
                "overlay resyncs to the body after a mid-animation swap");

            body.Pause();
            int pausedFrame = body.Frame;
            doll._Process(1.0);
            Check(body.Frame == pausedFrame, "paused body does not advance");
            Check(!sheetLayer.IsPlaying(), "overlay pauses together with the body");
            Check(sheetLayer.Frame == pausedFrame, "overlay holds the paused frame");

            body.Play(Walk);
            body.SetFrameAndProgress(0, 0.3f);
            doll._Process(0.016);
            Check(!sheetLayer.IsPlaying(), "follower layer never runs its own clock while the body plays");
            Check(sheetLayer.Frame == 0, "follower copies the body frame instead of playing");
            CheckClose(sheetLayer.FrameProgress, 0.3f, "follower copies the body frame progress instead of playing");

            // ---------------------------------------------------------------- direct body changes
            body.SetFrameAndProgress(4, 0.25f);
            Check(sheetLayer.Frame == 4, "frame changed signal syncs the overlay immediately");
            CheckClose(sheetLayer.FrameProgress, 0.25f, "frame progress is mirrored on the frame signal");
            doll._Process(0.016);
            Check(sheetLayer.Frame == 4, "overlay keeps tracking direct body frame changes");

            // ---------------------------------------------------------------- missing animations
            ExpectThrow(() => doll.PlayAnimation("not_a_real_animation"), "unknown animation is rejected");
            ExpectThrow(() => doll.PlayAnimation("  "), "blank animation name is rejected");
            Check(body.Animation == Walk, "a rejected animation leaves the body animation untouched");

            string partialJson = WriteTemp("_partial.json", SheetJson(
                new AsepriteFrameTag(Idle, 0, 1, "forward"),
                new AsepriteFrameTag(Walk, 2, 3, "forward")));
            string partialSockets = WriteTemp("_partial_sockets.json", SocketsJsonFor(4));
            CharacterPaperdoll partialDoll = Spawn(host);
            partialDoll.CharacterSheetJson = partialJson;
            partialDoll.EquipWeaponSheet(SwordSheetPng);
            AnimatedSprite2D partialBody = partialDoll.GetNode<AnimatedSprite2D>("BodySprite");
            AnimatedSprite2D partialLayer = partialDoll.GetNode<AnimatedSprite2D>("WeaponSheetSprite");
            partialDoll.PlayAnimation(Walk);
            partialBody.SetFrameAndProgress(1, 0.1f);
            partialDoll._Process(0.016);
            Check(partialLayer.Visible && partialLayer.Animation == Walk && partialLayer.Frame == 1,
                "overlay shows an animation it contains");
            partialDoll.PlayAnimation(Attack); // body has attack, overlay does not
            partialDoll._Process(0.016);
            Check(!partialLayer.Visible, "overlay hides when its SpriteFrames lack the body animation");
            Check(partialBody.Animation == Attack && partialBody.IsPlaying(),
                "body keeps playing the animation missing from the overlay");
            Check(partialLayer.Frame == 0, "hidden overlay resets its stale frame cursor instead of going out of bounds");
            partialDoll.PlayAnimation(Walk);
            partialBody.SetFrameAndProgress(1, 0.2f);
            partialDoll._Process(0.016);
            Check(partialLayer.Visible && partialLayer.Frame == 1, "overlay reappears and resyncs on a covered animation");
            Check(!partialSockets.StartsWith(CharJson, StringComparison.Ordinal), "fixture sockets live under user://");

            // ---------------------------------------------------------------- facing / mirroring
            Vector2 origin = new(-16f, -30f);
            for (int i = 0; i < 100; i++)
            {
                doll.SetFacingLeft(i % 2 == 0);
            }

            doll.SetFacingLeft(true);
            CheckVector(body.Position, origin + new Vector2(32f, 0f), "facing-left body position");
            Check(body.Scale == new Vector2(-1f, 1f), "facing-left body mirror scale");
            CheckVector(sheetLayer.Position, origin + new Vector2(32f, 0f), "facing-left weapon sheet position");
            Check(sheetLayer.Scale == new Vector2(-1f, 1f), "facing-left weapon sheet mirror scale");
            Check(doll.Scale == Vector2.One, "root scale stays untouched across facing toggles");

            doll.SetFacingLeft(false);
            CheckVector(body.Position, origin, "facing-right body position after 100 toggles");
            Check(body.Scale == Vector2.One, "facing-right body scale is exactly restored");
            CheckVector(sheetLayer.Position, origin, "facing-right weapon sheet position after 100 toggles");
            Check(sheetLayer.Scale == Vector2.One, "facing-right weapon sheet scale is exactly restored");

            // ---------------------------------------------------------------- static weapon / hilt geometry
            Vector2I swordPivot = new(11, 20);
            Texture2D? swordCleanTexture = ResourceLoader.Load<Texture2D>(SwordCleanPng);
            Check(swordCleanTexture is not null, "clean sword texture is imported");
            Texture2D swordClean = swordCleanTexture!;

            Node2D transformedParent = SpawnParent();
            CharacterPaperdoll geometryDoll = Spawn(transformedParent);
            geometryDoll.EquipStaticWeapon(swordClean, swordPivot);
            AnimatedSprite2D geometryBody = geometryDoll.GetNode<AnimatedSprite2D>("BodySprite");
            Sprite2D geometryWeapon = geometryDoll.GetNode<Sprite2D>("WeaponPivot/DynamicWeaponSprite");
            Dictionary<string, List<GripSocketData>> sockets =
                AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(CharJson, SocketsJson);

            foreach (bool facingLeft in new[] { false, true })
            {
                geometryDoll.SetFacingLeft(facingLeft);
                geometryBody.SetFrameAndProgress(1, 0f);
                geometryDoll._Process(0.016);

                GripSocketData socket = sockets[Idle][geometryBody.Frame];
                Vector2 gripGlobal = geometryBody.GetGlobalTransform()
                    * new Vector2(socket.GripX + 0.5f, socket.GripY + 0.5f);
                Vector2 hiltLocal = geometryWeapon.Offset
                    + new Vector2(swordPivot.X + 0.5f, swordPivot.Y + 0.5f);
                Vector2 hiltGlobal = geometryWeapon.GetGlobalTransform() * hiltLocal;

                CheckVector(hiltGlobal, gripGlobal,
                    $"weapon hilt pixel centre meets the grip socket under a rotated and scaled parent (facingLeft={facingLeft})");
                CheckVector(geometryDoll.GetCurrentGripGlobalPosition(), gripGlobal,
                    $"GetCurrentGripGlobalPosition matches the socket (facingLeft={facingLeft})");
                Check(geometryDoll.Scale == Vector2.One, "doll root scale is untouched under a transformed parent");
                Check(geometryWeapon.Centered == false && geometryWeapon.FlipH == false
                    && geometryWeapon.Scale == Vector2.One, "dynamic weapon sprite is unbaked and unmirrored");
                CheckVector(geometryWeapon.Offset, new Vector2(-swordPivot.X - 0.5f, -swordPivot.Y - 0.5f),
                    "dynamic weapon offset targets the pivot pixel centre");
            }

            // ---------------------------------------------------------------- weapon modes
            CharacterPaperdoll modeDoll = Spawn(host);
            AnimatedSprite2D modeSheet = modeDoll.GetNode<AnimatedSprite2D>("WeaponSheetSprite");
            Sprite2D modeWeapon = modeDoll.GetNode<Sprite2D>("WeaponPivot/DynamicWeaponSprite");
            Node2D modePivot = modeDoll.GetNode<Node2D>("WeaponPivot");
            AnimatedSprite2D modeBody = modeDoll.GetNode<AnimatedSprite2D>("BodySprite");

            modeDoll.EquipWeaponSheet(SwordSheetPng);
            Check(modeSheet.Visible && !modeWeapon.Visible, "sheet mode shows the sheet layer only");
            CheckVector(modeSheet.Position, origin, "weapon sheet layer shares the body canvas origin");

            Texture2D? axeCleanTexture = ResourceLoader.Load<Texture2D>(AxeCleanPng);
            Check(axeCleanTexture is not null, "clean axe texture is imported");
            Texture2D axeClean = axeCleanTexture!;
            modeDoll.EquipStaticWeapon(axeClean, swordPivot);
            Check(modeWeapon.Visible && !modeSheet.Visible, "static mode shows the dynamic weapon sprite only");

            modeDoll.PlayAnimation(Idle);
            modeBody.SetFrameAndProgress(0, 0f);
            modeDoll._Process(0.016);
            GripSocketData idleSocket = sockets[Idle][0];
            CheckVector(modePivot.Position,
                origin + new Vector2(idleSocket.GripX + 0.5f, idleSocket.GripY + 0.5f),
                "static weapon pivot sits on the baked grip socket in canvas space");
            CheckClose(modePivot.RotationDegrees, idleSocket.AngleDeg, "pivot rotation follows the socket angle");
            Check(modePivot.Scale == Vector2.One, "pivot is not mirrored while facing right");

            int axeWidth = axeClean.GetWidth();
            int axeHeight = axeClean.GetHeight();
            ExpectThrow(() => modeDoll.EquipStaticWeapon(axeClean, new Vector2I(axeWidth, 0)),
                "weapon pivot outside the texture width is rejected");
            ExpectThrow(() => modeDoll.EquipStaticWeapon(axeClean, new Vector2I(0, axeHeight)),
                "weapon pivot outside the texture height is rejected");
            Check(modeWeapon.Visible && !modeSheet.Visible, "a rejected static weapon leaves the previous mode intact");
            ExpectThrow(() => modeDoll.EquipStaticWeapon(null!, Vector2I.Zero), "null weapon texture is rejected");
            Check(modeWeapon.Visible && !modeSheet.Visible, "a null weapon texture leaves the previous mode intact");
            ExpectThrow(() => modeDoll.EquipWeaponSheet("res://assets/paperdoll/does_not_exist.png"),
                "missing weapon sheet is rejected");
            Check(modeWeapon.Visible && !modeSheet.Visible, "a failed sheet equip keeps the previous static weapon");
            Check(modeBody.Frame == 0, "weapon mode changes never move the body frame");

            // ---------------------------------------------------------------- exit / re-entry
            CharacterPaperdoll lifeDoll = Spawn(host);
            AnimatedSprite2D lifeBody = lifeDoll.GetNode<AnimatedSprite2D>("BodySprite");
            AnimatedSprite2D lifeLayer = lifeDoll.GetNode<AnimatedSprite2D>("WeaponSheetSprite");
            lifeDoll.EquipWeaponSheet(SwordSheetPng);
            lifeDoll.PlayAnimation(Walk);
            lifeBody.SetFrameAndProgress(3, 0.4f);
            lifeDoll._Process(0.016);
            Check(ConnectionCount(lifeBody, AnimatedSprite2D.SignalName.FrameChanged) == 1,
                "frame signal is connected exactly once");
            Check(ConnectionCount(lifeBody, AnimatedSprite2D.SignalName.AnimationChanged) == 1,
                "animation signal is connected exactly once");
            Check(ConnectionCount(lifeBody, AnimatedSprite2D.SignalName.AnimationFinished) == 1,
                "finished signal is connected exactly once");

            for (int i = 0; i < 3; i++)
            {
                host.RemoveChild(lifeDoll);
                Check(ConnectionCount(lifeBody, AnimatedSprite2D.SignalName.FrameChanged) == 0,
                    "body signals are disconnected when leaving the tree");
                host.AddChild(lifeDoll);
                Check(ConnectionCount(lifeBody, AnimatedSprite2D.SignalName.FrameChanged) == 1,
                    "body signals reconnect exactly once on re-entry");
            }

            Check(lifeBody.Animation == Walk, "body animation survives exit and re-entry");
            lifeBody.SetFrameAndProgress(2, 0.1f);
            Check(lifeLayer.Frame == 2, "overlay still follows the body after exit and re-entry");
            CheckClose(lifeBody.FrameProgress, 0.1f, "frame progress stays under explicit control");

            // ---------------------------------------------------------------- non-looping completion
            CharacterPaperdoll loopDoll = Spawn(host);
            AnimatedSprite2D loopBody = loopDoll.GetNode<AnimatedSprite2D>("BodySprite");
            loopDoll.PlayAnimation(Attack);
            Check(loopBody.Animation == Attack, "attack animation plays");
            loopBody.EmitSignal(AnimatedSprite2D.SignalName.AnimationFinished);
            Check(loopBody.Animation == Idle, "finished non-looping attack returns to idle");
            loopDoll.PlayAnimation(Hit);
            loopBody.EmitSignal(AnimatedSprite2D.SignalName.AnimationFinished);
            Check(loopBody.Animation == Idle, "finished non-looping hit returns to idle");
            loopDoll.PlayAnimation(Walk);
            loopBody.EmitSignal(AnimatedSprite2D.SignalName.AnimationFinished);
            Check(loopBody.Animation == Walk, "looping animations are never interrupted by a finished signal");

            string fallbackJson = WriteTemp("_fallback.json", SheetJson(
                new AsepriteFrameTag(Walk, 0, 1, "forward"),
                new AsepriteFrameTag(Attack, 2, 3, "forward")));
            string fallbackSockets = WriteTemp("_fallback_sockets.json", SocketsJsonFor(4));
            CharacterPaperdoll fallbackDoll = prefab!.Instantiate<CharacterPaperdoll>();
            dolls.Add(fallbackDoll);
            fallbackDoll.CharacterSheetJson = fallbackJson;
            fallbackDoll.GripSocketsJson = fallbackSockets;
            host.AddChild(fallbackDoll);
            fallbackDoll.SetProcess(false);
            AnimatedSprite2D fallbackBody = fallbackDoll.GetNode<AnimatedSprite2D>("BodySprite");
            Check(fallbackBody.SpriteFrames!.HasAnimation(Walk) && !fallbackBody.SpriteFrames.HasAnimation(Idle),
                "fallback fixture has no idle animation, only walk (loop) and attack (one-shot)");
            Check(fallbackBody.Animation == Walk,
                "runtime falls back to the first looping animation when idle is missing");
            fallbackDoll.PlayAnimation(Attack);
            fallbackBody.EmitSignal(AnimatedSprite2D.SignalName.AnimationFinished);
            Check(fallbackBody.Animation == Walk,
                "non-looping completion falls back to the first looping animation when idle is missing");
            ExpectThrow(() => fallbackDoll.PlayAnimation(Idle),
                "missing idle animation is a coherent API error on this doll");

            // ---------------------------------------------------------------- editor bake
            string bakePath = NewTempPath("_frames.tres");
            CharacterPaperdoll bakeDoll = Spawn(host);
            bakeDoll.SaveSpriteFramesPath = bakePath;
            AnimatedSprite2D bakeBody = bakeDoll.GetNode<AnimatedSprite2D>("BodySprite");
            SpriteFrames bakeFramesBefore = bakeBody.SpriteFrames!;
            bakeDoll.BakeAssetsInEditor();
            Check(FileAccess.FileExists(bakePath), "editor bake writes the SpriteFrames resource");
            SpriteFrames? baked = ResourceLoader.Load<SpriteFrames>(bakePath);
            Check(baked is not null && baked.HasAnimation(Idle) && baked.GetFrameCount(Idle) == 4,
                "baked resource reloads with the idle animation");
            Check(!ReferenceEquals(bakeBody.SpriteFrames, bakeFramesBefore),
                "editor bake binds the validated resource to the body");
            Check(bakeBody.SpriteFrames!.HasAnimation(Idle) && bakeBody.SpriteFrames.HasAnimation(Attack),
                "body still has every animation after the bake");

            string bakedBeforeFailure = ReadText(bakePath);
            SpriteFrames bakeFramesAfterSuccess = bakeBody.SpriteFrames!;
            bakeDoll.CharacterSheetJson = "res://assets/paperdoll/does_not_exist.json";
            bakeDoll.BakeAssetsInEditor();
            Check(ReadText(bakePath) == bakedBeforeFailure, "a failed bake leaves the saved resource untouched");
            Check(ReferenceEquals(bakeBody.SpriteFrames, bakeFramesAfterSuccess),
                "a failed bake leaves the live body frames untouched");

            bakeDoll.CharacterSheetJson = CharJson;
            bakeDoll.SaveSpriteFramesPath = NewTempPath("_missing_dir/missing.tres");
            bakeDoll.BakeAssetsInEditor();
            Check(ReferenceEquals(bakeBody.SpriteFrames, bakeFramesAfterSuccess),
                "an unwritable bake destination leaves the live body frames untouched");

            // ---------------------------------------------------------------- real native clock (SceneTree driven)
            CharacterPaperdoll liveDoll = Spawn(host);
            AnimatedSprite2D liveBody = liveDoll.GetNode<AnimatedSprite2D>("BodySprite");
            AnimatedSprite2D liveLayer = liveDoll.GetNode<AnimatedSprite2D>("WeaponSheetSprite");
            liveDoll.EquipWeaponSheet(SwordSheetPng);
            liveDoll.SetProcess(true); // let the controller and the body really run

            SceneTree? tree = host.GetTree();
            Check(tree is not null, "test host is inside a SceneTree");
            SceneTree sceneTree = tree!;

            async Task AwaitSeconds(double seconds)
                => await host.ToSignal(sceneTree.CreateTimer(seconds), SceneTreeTimer.SignalName.Timeout);

            // forward playback at a non-default speed scale
            liveBody.SpeedScale = 2f;
            liveDoll.PlayAnimation(Walk);
            int liveFrameBefore = liveBody.Frame;
            float liveProgressBefore = liveBody.FrameProgress;
            await AwaitSeconds(0.08);

            Check(liveBody.Frame != liveFrameBefore
                    || !Mathf.IsEqualApprox(liveBody.FrameProgress, liveProgressBefore),
                "the real body clock advances over a native SceneTree timer");
            Check(!liveLayer.IsPlaying(), "follower stays paused while the real body advances");
            Check(liveLayer.Frame == liveBody.Frame, "follower frame equals the advancing body frame");
            CheckClose(liveLayer.FrameProgress, liveBody.FrameProgress,
                "follower progress equals the advancing body progress");

            // reverse playback keeps the follower glued to the body clock
            liveBody.SpeedScale = 2f;
            liveBody.PlayBackwards(Walk);
            Check(liveBody.GetPlayingSpeed() < 0f, "body really plays the clip in reverse");
            int liveReverseFrameBefore = liveBody.Frame;
            float liveReverseProgressBefore = liveBody.FrameProgress;
            await AwaitSeconds(0.08);

            Check(liveBody.Frame != liveReverseFrameBefore
                    || !Mathf.IsEqualApprox(liveBody.FrameProgress, liveReverseProgressBefore),
                "the real body clock advances during reverse playback");
            Check(liveBody.Animation == Walk, "reverse playback keeps the walk animation");
            Check(!liveLayer.IsPlaying(), "follower stays paused during reverse playback");
            Check(liveLayer.Frame == liveBody.Frame, "follower frame equals the reversing body frame");
            CheckClose(liveLayer.FrameProgress, liveBody.FrameProgress,
                "follower progress equals the reversing body progress");

            // a paused body holds both clocks
            liveBody.SpeedScale = 1f;
            liveBody.Play(Walk);
            liveBody.Pause();
            int livePausedFrame = liveBody.Frame;
            float livePausedProgress = liveBody.FrameProgress;
            await AwaitSeconds(0.08);

            Check(liveBody.Frame == livePausedFrame
                    && Mathf.IsEqualApprox(liveBody.FrameProgress, livePausedProgress),
                "paused body does not advance over a native SceneTree timer");
            Check(liveLayer.Frame == liveBody.Frame
                    && Mathf.IsEqualApprox(liveLayer.FrameProgress, liveBody.FrameProgress),
                "follower matches the paused body");
            liveDoll.SetProcess(false);

            return checks;
        }
        finally
        {
            foreach (CharacterPaperdoll doll in dolls)
            {
                if (!GodotObject.IsInstanceValid(doll))
                {
                    continue;
                }

                Node? parent = doll.GetParent();
                parent?.RemoveChild(doll);
                doll.Free();
            }

            foreach (Node parent in extraParents)
            {
                if (!GodotObject.IsInstanceValid(parent))
                {
                    continue;
                }

                parent.GetParent()?.RemoveChild(parent);
                parent.Free();
            }

            foreach (string path in tempFiles)
            {
                if (FileAccess.FileExists(path))
                {
                    DirAccess.RemoveAbsolute(ProjectSettings.GlobalizePath(path));
                }
            }
        }
    }
}
