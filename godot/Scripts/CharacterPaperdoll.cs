using Godot;
using System;
using System.Collections.Generic;
using System.IO;

namespace PixelPipeline;

/// <summary>
/// Paperdoll controller that keeps a single body animation clock and mirrors it onto the
/// weapon-sheet, dynamic-weapon and optional FX layers. All layers are mirrored by explicit
/// <c>Scale</c>/<c>Position</c> maths so the node's own scale stays untouched for external
/// transforms (parents, animation players, physics bodies).
/// </summary>
[Tool]
public partial class CharacterPaperdoll : Node2D
{
    private const string IdleAnimation = "idle";

    [Export(PropertyHint.File, "*.png")]
    public string CharacterSheetPng { get; set; } = "res://assets/paperdoll/char_sheet.png";

    [Export(PropertyHint.File, "*.json")]
    public string CharacterSheetJson { get; set; } = "res://assets/paperdoll/char_sheet.json";

    [Export(PropertyHint.File, "*.json")]
    public string GripSocketsJson { get; set; } = "res://assets/paperdoll/grip_sockets.json";

    [Export(PropertyHint.SaveFile, "*.tres")]
    public string SaveSpriteFramesPath { get; set; } = "res://assets/paperdoll/char_frames.tres";

    /// <summary>Single frame canvas of the paperdoll; drives origin and grip mapping.</summary>
    [Export]
    public Vector2I CanvasSize { get; set; } = new(32, 32);

    [Export]
    public AnimatedSprite2D? BodySprite { get; set; }

    [Export]
    public AnimatedSprite2D? WeaponSheetSprite { get; set; }

    [Export]
    public Node2D? WeaponPivot { get; set; }

    [Export]
    public Sprite2D? DynamicWeaponSprite { get; set; }

    [Export]
    public AnimatedSprite2D? FXSprite { get; set; }

    /// <summary>Editor-only bake button; the engine renders it in the inspector.</summary>
    [ExportToolButton("Bake Sprite Frames")]
    public Callable BakeSpriteFramesButton => Callable.From(BakeAssetsInEditor);

    private readonly Dictionary<string, List<GripSocketData>> _socketsByTag = new(StringComparer.Ordinal);
    private readonly HashSet<string> _missingSocketWarnings = new(StringComparer.Ordinal);

    private SpriteFrames? _weaponSheetFrames;
    private SpriteFrames? _fxFrames;
    private bool _sheetWeaponMode;
    private bool _staticWeaponMode;
    private bool _faceLeft;
    private bool _runtimeInitialized;
    private bool _hasSavedState;
    private string _savedAnimation = string.Empty;
    private int _savedFrame;
    private float _savedProgress;
    private bool _savedPlaying;
    private AnimatedSprite2D? _connectedBody;

    // ----------------------------------------------------------------- lifecycle

    public override void _EnterTree()
    {
        ProcessPriority = 1; // children (body, priority 0) advance before this controller syncs
        ResolveNodeReferences();
        ApplyAlignment();
        if (Engine.IsEditorHint())
        {
            return;
        }

        ConnectBodySignals();
        if (_runtimeInitialized)
        {
            RestoreRuntimeState();
        }
    }

    public override void _Ready()
    {
        ProcessPriority = 1;
        ResolveNodeReferences();
        ApplyAlignment();
        if (Engine.IsEditorHint())
        {
            return;
        }

        ConnectBodySignals();
        EnsureRuntimeInitialized();
        if (_hasSavedState)
        {
            RestoreRuntimeState();
        }
        else
        {
            StartDefaultAnimation();
        }
    }

    public override void _ExitTree()
    {
        CaptureRuntimeState();
        DisconnectBodySignals();
    }

    public override void _Notification(int what)
    {
        if (what == NotificationPredelete)
        {
            DisconnectBodySignals();
        }
    }

    public override void _Process(double delta)
    {
        if (Engine.IsEditorHint())
        {
            return;
        }

        SyncLayers();
    }

    // ----------------------------------------------------------------- public API

    /// <summary>Plays a body animation and mirrors it onto every dependent layer.</summary>
    public void PlayAnimation(string animationName)
    {
        if (string.IsNullOrWhiteSpace(animationName))
        {
            throw new ArgumentException("Animation name must be non-empty.", nameof(animationName));
        }

        AnimatedSprite2D body = BodySprite ?? throw new InvalidOperationException(
            "CharacterPaperdoll cannot play an animation without a BodySprite node.");
        SpriteFrames? frames = body.SpriteFrames;
        if (frames is null || !frames.HasAnimation(animationName) || frames.GetFrameCount(animationName) <= 0)
        {
            throw new ArgumentException(
                $"Body sprite has no playable animation '{animationName}'. Available: {DescribeAnimations(frames)}.",
                nameof(animationName));
        }

        if (Engine.IsEditorHint())
        {
            // Never autoplay inside the editor; just show the first frame of the requested tag.
            body.Animation = animationName;
            body.SetFrameAndProgress(0, 0f);
            body.Pause();
            return;
        }

        body.Play(animationName);
        SyncLayers();
    }

    /// <summary>
    /// Replaces the dynamic weapon with a baked weapon-layer sheet (one frame per body frame).
    /// The body frame and its progress are never touched by a weapon swap.
    /// </summary>
    public void EquipWeaponSheet(string weaponSheetPath)
    {
        if (string.IsNullOrWhiteSpace(weaponSheetPath))
        {
            throw new ArgumentException("Weapon sheet path must be non-empty.", nameof(weaponSheetPath));
        }

        string pngPath = weaponSheetPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
            ? Path.ChangeExtension(weaponSheetPath, ".png")
            : weaponSheetPath;

        AnimatedSprite2D layer = WeaponSheetSprite ?? throw new InvalidOperationException(
            "CharacterPaperdoll cannot equip a weapon sheet without a WeaponSheetSprite node.");

        // Build (and validate) before touching any node state: a failure cannot leave a half-swapped mode.
        Texture2D texture = LoadTexture(pngPath);
        SpriteFrames frames = AsepriteSpriteFramesBuilder.BuildOverlayFromMasterJson(texture, CharacterSheetJson);
        if (frames.GetAnimationNames().Length == 0)
        {
            throw new InvalidDataException($"Weapon sheet '{pngPath}' produced no animations.");
        }

        layer.SpriteFrames = frames;
        layer.TextureFilter = CanvasItem.TextureFilterEnum.Nearest;
        _weaponSheetFrames = frames;
        _sheetWeaponMode = true;
        _staticWeaponMode = false;

        if (DynamicWeaponSprite is not null)
        {
            DynamicWeaponSprite.Visible = false;
        }

        layer.Visible = true;
        SyncLayers();
    }

    /// <summary>
    /// Equips a single unbaked weapon texture whose pivot is the grip pixel inside that texture.
    /// The pivot must be an integer pixel inside the texture; anything else is rejected so the
    /// previously equipped mode stays intact.
    /// </summary>
    public void EquipStaticWeapon(Texture2D weaponTexture, Vector2I pivot)
    {
        ArgumentNullException.ThrowIfNull(weaponTexture);

        int width = weaponTexture.GetWidth();
        int height = weaponTexture.GetHeight();
        if (pivot.X < 0 || pivot.Y < 0 || pivot.X >= width || pivot.Y >= height)
        {
            throw new ArgumentOutOfRangeException(
                nameof(pivot),
                $"Weapon pivot ({pivot.X}, {pivot.Y}) must be an integer pixel inside the {width}x{height} weapon texture.");
        }

        Sprite2D sprite = DynamicWeaponSprite ?? throw new InvalidOperationException(
            "CharacterPaperdoll cannot equip a static weapon without a DynamicWeaponSprite node.");

        // Configure the sprite only after validation; the hilt pixel centre then sits on the pivot origin.
        sprite.Texture = weaponTexture;
        sprite.Centered = false;
        sprite.Offset = new Vector2(-pivot.X - 0.5f, -pivot.Y - 0.5f);
        sprite.Scale = Vector2.One;
        sprite.FlipH = false;
        sprite.FlipV = false;
        sprite.TextureFilter = CanvasItem.TextureFilterEnum.Nearest;

        _staticWeaponMode = true;
        _sheetWeaponMode = false;
        if (WeaponSheetSprite is not null)
        {
            WeaponSheetSprite.Visible = false;
        }

        sprite.Visible = true;
        UpdateWeaponTransform();
    }

    /// <summary>Mirrors the doll horizontally without touching this node's own scale.</summary>
    public void SetFacingLeft(bool facingLeft)
    {
        _faceLeft = facingLeft;
        ApplyAlignment();
    }

    /// <summary>Global position of the current grip socket (the weapon hilt centre).</summary>
    public Vector2 GetCurrentGripGlobalPosition()
    {
        Node2D pivot = WeaponPivot ?? throw new InvalidOperationException(
            "CharacterPaperdoll has no WeaponPivot node, so it has no grip position.");
        return pivot.GlobalPosition;
    }

    /// <summary>
    /// Builds and saves the body SpriteFrames (and re-reads the grip sockets) using the real
    /// builder. The live node is only rebound after the resource has been validated and written.
    /// </summary>
    public void BakeAssetsInEditor()
    {
        try
        {
            Texture2D texture = LoadTexture(CharacterSheetPng);

            // Validate in memory first: a failure here must not touch the saved resource or the live node.
            SpriteFrames validated = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, CharacterSheetJson);
            if (validated.GetAnimationNames().Length == 0)
            {
                throw new InvalidDataException($"'{CharacterSheetJson}' contains no animation tags to bake.");
            }

            Dictionary<string, List<GripSocketData>> sockets =
                AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(CharacterSheetJson, GripSocketsJson);

            // Checked resource save; the builder throws when ResourceSaver fails.
            SpriteFrames saved = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, CharacterSheetJson, SaveSpriteFramesPath);

            // Bind only after validation + save succeeded.
            if (BodySprite is not null)
            {
                BodySprite.SpriteFrames = saved;
            }

            _socketsByTag.Clear();
            foreach (KeyValuePair<string, List<GripSocketData>> pair in sockets)
            {
                _socketsByTag[pair.Key] = pair.Value;
            }

            _missingSocketWarnings.Clear();
            _runtimeInitialized = true;

            GD.Print(
                $"[CharacterPaperdoll] Baked {saved.GetAnimationNames().Length} animation(s) " +
                $"({string.Join(", ", saved.GetAnimationNames())}) from '{CharacterSheetJson}' into '{SaveSpriteFramesPath}'.");

            if (BodySprite is not null)
            {
                if (Engine.IsEditorHint())
                {
                    string preview = SelectDefaultAnimation(BodySprite.SpriteFrames);
                    if (!string.IsNullOrEmpty(preview))
                    {
                        BodySprite.Animation = preview;
                        BodySprite.SetFrameAndProgress(0, 0f);
                    }
                }
                else
                {
                    StartDefaultAnimation();
                }
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[CharacterPaperdoll] Bake failed for '{CharacterSheetJson}': {ex.Message}");
        }
    }

    // ----------------------------------------------------------------- node resolution

    private void ResolveNodeReferences()
    {
        BodySprite ??= GetNodeOrNull<AnimatedSprite2D>("BodySprite");
        WeaponSheetSprite ??= GetNodeOrNull<AnimatedSprite2D>("WeaponSheetSprite");
        WeaponPivot ??= GetNodeOrNull<Node2D>("WeaponPivot");
        DynamicWeaponSprite ??= GetNodeOrNull<Sprite2D>("WeaponPivot/DynamicWeaponSprite")
            ?? GetNodeOrNull<Sprite2D>("DynamicWeaponSprite");
        FXSprite ??= GetNodeOrNull<AnimatedSprite2D>("FXSprite");

        CanvasItem?[] layers = { BodySprite, WeaponSheetSprite, WeaponPivot, DynamicWeaponSprite, FXSprite };
        foreach (CanvasItem? layer in layers)
        {
            if (layer is not null)
            {
                layer.TextureFilter = CanvasItem.TextureFilterEnum.Nearest;
            }
        }

        if (DynamicWeaponSprite is not null)
        {
            DynamicWeaponSprite.Centered = false;
            DynamicWeaponSprite.FlipH = false;
            DynamicWeaponSprite.Scale = Vector2.One;
        }
    }

    // ----------------------------------------------------------------- runtime state

    private void EnsureRuntimeInitialized()
    {
        if (_runtimeInitialized)
        {
            return;
        }

        AnimatedSprite2D? body = BodySprite;
        if (body is null)
        {
            GD.PrintErr("[CharacterPaperdoll] BodySprite is missing; the paperdoll cannot animate.");
            _runtimeInitialized = true;
            return;
        }

        // A freshly created AnimatedSprite2D carries a non-null SpriteFrames holding one empty
        // "default" animation. That is not a usable animation set, so rebuild it from the sheet.
        // Valid frames already assigned by a scene/prefab are kept untouched.
        if (!HasPlayableAnimation(body.SpriteFrames))
        {
            try
            {
                Texture2D texture = LoadTexture(CharacterSheetPng);
                body.SpriteFrames = AsepriteSpriteFramesBuilder.BuildFromAsepriteJson(texture, CharacterSheetJson);
            }
            catch (Exception ex)
            {
                GD.PrintErr($"[CharacterPaperdoll] Failed to initialise body frames from '{CharacterSheetJson}': {ex.Message}");
                _runtimeInitialized = true;
                return;
            }
        }

        LoadGripSockets();

        _fxFrames = FXSprite?.SpriteFrames;
        if (!HasPlayableAnimation(_fxFrames))
        {
            _fxFrames = null;
        }

        _runtimeInitialized = true;
        ApplyAlignment();
    }

    private void LoadGripSockets()
    {
        _socketsByTag.Clear();
        _missingSocketWarnings.Clear();
        try
        {
            Dictionary<string, List<GripSocketData>> sockets =
                AsepriteSpriteFramesBuilder.LoadGripSocketsByTag(CharacterSheetJson, GripSocketsJson);
            foreach (KeyValuePair<string, List<GripSocketData>> pair in sockets)
            {
                _socketsByTag[pair.Key] = pair.Value;
            }
        }
        catch (Exception ex)
        {
            // Strict loader: never invent anchors. Anchors stay empty and static weapons report
            // instead of being placed at a made-up position.
            GD.PrintErr($"[CharacterPaperdoll] Grip sockets unavailable from '{GripSocketsJson}': {ex.Message}");
        }
    }

    private void StartDefaultAnimation()
    {
        AnimatedSprite2D? body = BodySprite;
        if (body is null)
        {
            return;
        }

        string animation = SelectDefaultAnimation(body.SpriteFrames);
        if (string.IsNullOrEmpty(animation))
        {
            GD.PrintErr("[CharacterPaperdoll] Body SpriteFrames contain no playable animation.");
            return;
        }

        body.Play(animation);
        SyncLayers();
    }

    private void CaptureRuntimeState()
    {
        AnimatedSprite2D? body = BodySprite;
        if (body is null)
        {
            return;
        }

        _savedAnimation = body.Animation.ToString();
        _savedFrame = body.Frame;
        _savedProgress = body.FrameProgress;
        _savedPlaying = body.IsPlaying();
        _hasSavedState = !string.IsNullOrEmpty(_savedAnimation);
    }

    private void RestoreRuntimeState()
    {
        if (!_hasSavedState)
        {
            return;
        }

        AnimatedSprite2D? body = BodySprite;
        if (body is null)
        {
            return;
        }

        SpriteFrames? frames = body.SpriteFrames;
        if (frames is null || string.IsNullOrEmpty(_savedAnimation)
            || !frames.HasAnimation(_savedAnimation) || frames.GetFrameCount(_savedAnimation) <= 0)
        {
            StartDefaultAnimation();
            return;
        }

        body.Play(_savedAnimation);
        body.SetFrameAndProgress(Math.Clamp(_savedFrame, 0, frames.GetFrameCount(_savedAnimation) - 1), _savedProgress);
        if (!_savedPlaying)
        {
            body.Pause();
        }

        SyncLayers();
    }

    // ----------------------------------------------------------------- body clock sync

    private void SyncLayers()
    {
        AnimatedSprite2D? body = BodySprite;
        if (body is null)
        {
            return;
        }

        string animation = body.Animation.ToString();
        int frame = body.Frame;
        float progress = body.FrameProgress;

        // The body is the only clock. Follower layers are positioned from it and never advance on their own.
        SyncOverlayLayer(WeaponSheetSprite, _sheetWeaponMode ? _weaponSheetFrames : null, animation, frame, progress);
        SyncOverlayLayer(FXSprite, _fxFrames, animation, frame, progress);
        UpdateWeaponTransform();
    }

    /// <summary>
    /// Drives an overlay layer from the body clock only: the layer is always paused and is only
    /// ever positioned with <c>SetFrameAndProgress</c>. A layer that has no matching animation is
    /// hidden and its stale cursor reset (never left pointing past the end of a shorter clip).
    /// </summary>
    private static void SyncOverlayLayer(
        AnimatedSprite2D? layer,
        SpriteFrames? frames,
        string animation,
        int frame,
        float progress)
    {
        if (layer is null)
        {
            return;
        }

        bool usable = frames is not null
            && !string.IsNullOrEmpty(animation)
            && frames.HasAnimation(animation)
            && frames.GetFrameCount(animation) > 0;

        if (!usable)
        {
            if (layer.Visible)
            {
                layer.Visible = false;
            }

            SpriteFrames? own = layer.SpriteFrames;
            if (own is not null && own.HasAnimation(layer.Animation) && own.GetFrameCount(layer.Animation) > 0)
            {
                layer.SetFrameAndProgress(0, 0f);
            }

            if (layer.IsPlaying())
            {
                layer.Pause();
            }

            return;
        }

        if (!ReferenceEquals(layer.SpriteFrames, frames))
        {
            layer.SpriteFrames = frames;
        }

        if (layer.Animation != animation)
        {
            layer.Animation = animation;
        }

        // Single body clock: a follower never runs its own playback, even while the body plays.
        if (layer.IsPlaying())
        {
            layer.Pause();
        }

        int clamped = Math.Clamp(frame, 0, frames!.GetFrameCount(animation) - 1);
        if (layer.Frame != clamped || !Mathf.IsEqualApprox(layer.FrameProgress, progress))
        {
            layer.SetFrameAndProgress(clamped, progress);
        }

        if (!layer.Visible)
        {
            layer.Visible = true;
        }
    }

    private void OnBodyFrameChanged() => SyncLayers();

    private void OnBodyAnimationChanged() => SyncLayers();

    private void OnBodyAnimationFinished()
    {
        if (Engine.IsEditorHint())
        {
            return;
        }

        AnimatedSprite2D? body = BodySprite;
        if (body is null)
        {
            return;
        }

        SpriteFrames? frames = body.SpriteFrames;
        if (frames is null)
        {
            return;
        }

        string animation = body.Animation.ToString();
        if (!frames.HasAnimation(animation)
            || frames.GetAnimationLoopMode(animation) != SpriteFrames.LoopMode.None)
        {
            return; // looping clips (idle/walk) never get kicked back by a finished signal
        }

        string next = SelectDefaultAnimation(frames);
        if (string.IsNullOrEmpty(next) || next == animation)
        {
            return;
        }

        body.Play(next);
        SyncLayers();
    }

    private void ConnectBodySignals()
    {
        AnimatedSprite2D? body = BodySprite;
        if (body is null || ReferenceEquals(_connectedBody, body))
        {
            return;
        }

        DisconnectBodySignals();
        body.FrameChanged += OnBodyFrameChanged;
        body.AnimationChanged += OnBodyAnimationChanged;
        body.AnimationFinished += OnBodyAnimationFinished;
        _connectedBody = body;
    }

    private void DisconnectBodySignals()
    {
        AnimatedSprite2D? body = _connectedBody;
        _connectedBody = null;
        if (body is null || !IsInstanceValid(body))
        {
            return;
        }

        body.FrameChanged -= OnBodyFrameChanged;
        body.AnimationChanged -= OnBodyAnimationChanged;
        body.AnimationFinished -= OnBodyAnimationFinished;
    }

    // ----------------------------------------------------------------- alignment / grip

    private Vector2 CanvasOrigin => new(-CanvasSize.X / 2f, -CanvasSize.Y + 2f);

    private void ApplyAlignment()
    {
        Vector2 origin = CanvasOrigin;
        Vector2 scale = new(_faceLeft ? -1f : 1f, 1f);
        Vector2 position = origin + (_faceLeft ? new Vector2(CanvasSize.X, 0f) : Vector2.Zero);

        AnimatedSprite2D?[] layers = { BodySprite, WeaponSheetSprite, FXSprite };
        foreach (AnimatedSprite2D? layer in layers)
        {
            if (layer is null)
            {
                continue;
            }

            layer.Centered = false;
            layer.FlipH = false;
            layer.FlipV = false;
            layer.Scale = scale;
            layer.Position = position;
        }

        UpdateWeaponTransform();
    }

    private void UpdateWeaponTransform()
    {
        Node2D? pivot = WeaponPivot;
        if (pivot is null)
        {
            return;
        }

        AnimatedSprite2D? body = BodySprite;
        string animation = body?.Animation.ToString() ?? string.Empty;
        int frame = body?.Frame ?? 0;

        if (!TryGetSocket(animation, frame, out GripSocketData? socket))
        {
            if (_staticWeaponMode)
            {
                HideStaticWeaponWithoutAnchor(animation, frame);
            }

            return;
        }

        Vector2 origin = CanvasOrigin;
        float gripX = socket!.GripX + 0.5f;
        float gripY = socket.GripY + 0.5f;

        pivot.Position = origin + (_faceLeft
            ? new Vector2(CanvasSize.X - gripX, gripY)
            : new Vector2(gripX, gripY));
        pivot.Scale = new Vector2(_faceLeft ? -1f : 1f, 1f);
        pivot.RotationDegrees = _faceLeft ? -socket.AngleDeg : socket.AngleDeg;
    }

    private void HideStaticWeaponWithoutAnchor(string animation, int frame)
    {
        if (DynamicWeaponSprite is not null && DynamicWeaponSprite.Visible)
        {
            DynamicWeaponSprite.Visible = false;
        }

        if (_missingSocketWarnings.Add($"{animation}#{frame}"))
        {
            GD.PrintErr(
                $"[CharacterPaperdoll] No grip socket for animation '{animation}' frame {frame}; " +
                "the static weapon is hidden instead of being placed on a fabricated anchor.");
        }
    }

    private bool TryGetSocket(string animation, int frame, out GripSocketData? socket)
    {
        socket = null;
        if (string.IsNullOrEmpty(animation)
            || !_socketsByTag.TryGetValue(animation, out List<GripSocketData>? list)
            || list is null
            || frame < 0
            || frame >= list.Count)
        {
            return false;
        }

        socket = list[frame];
        return socket is not null;
    }

    // ----------------------------------------------------------------- helpers

    private static bool HasPlayableAnimation(SpriteFrames? frames)
    {
        if (frames is null)
        {
            return false;
        }

        foreach (StringName name in frames.GetAnimationNames())
        {
            if (frames.GetFrameCount(name) > 0)
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>
    /// Default clip policy: prefer a real idle clip, then the first non-empty looping clip, then the
    /// first non-empty clip. <c>GetAnimationNames</c> is alphabetically sorted by the engine, so
    /// blindly taking the first name would boot an action clip such as "attack" instead of a loop.
    /// </summary>
    private static string SelectDefaultAnimation(SpriteFrames? frames)
    {
        if (frames is null)
        {
            return string.Empty;
        }

        if (frames.HasAnimation(IdleAnimation) && frames.GetFrameCount(IdleAnimation) > 0)
        {
            return IdleAnimation;
        }

        foreach (StringName name in frames.GetAnimationNames())
        {
            if (frames.GetFrameCount(name) > 0
                && frames.GetAnimationLoopMode(name) != SpriteFrames.LoopMode.None)
            {
                return name.ToString();
            }
        }

        foreach (StringName name in frames.GetAnimationNames())
        {
            if (frames.GetFrameCount(name) > 0)
            {
                return name.ToString();
            }
        }

        return string.Empty;
    }

    private static string DescribeAnimations(SpriteFrames? frames)
    {
        if (frames is null)
        {
            return "<no SpriteFrames>";
        }

        List<string> names = new();
        foreach (StringName name in frames.GetAnimationNames())
        {
            names.Add($"{name}({frames.GetFrameCount(name)})");
        }

        return names.Count == 0 ? "<none>" : string.Join(", ", names);
    }

    private static Texture2D LoadTexture(string path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new ArgumentException("Texture path must be non-empty.", nameof(path));
        }

        if (!ResourceLoader.Exists(path))
        {
            throw new FileNotFoundException($"Texture '{path}' does not exist or is not imported.");
        }

        Texture2D? texture = ResourceLoader.Load<Texture2D>(path);
        return texture ?? throw new InvalidDataException($"Resource '{path}' is not a Texture2D.");
    }
}
