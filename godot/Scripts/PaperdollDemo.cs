using Godot;
using System;
using System.Text.Json;

namespace PixelPipeline;

public partial class PaperdollDemo : Node2D
{
    private CharacterPaperdoll? _character;

    private Button? _idleButton;
    private Button? _walkButton;
    private Button? _attackButton;
    private Button? _hitButton;
    private OptionButton? _weaponSelect;
    private CheckButton? _modeToggle;
    private CheckBox? _facingToggle;

    private const string SwordSheetPath = "res://assets/paperdoll/weapon_sword_sheet.png";
    private const string AxeSheetPath = "res://assets/paperdoll/weapon_axe_sheet.png";
    private const string SwordCleanPath = "res://assets/paperdoll/sword_clean.png";
    private const string AxeCleanPath = "res://assets/paperdoll/axe_clean.png";
    private const string SwordMetaPath = "res://assets/paperdoll/weapon_sword_metadata.json";
    private const string AxeMetaPath = "res://assets/paperdoll/weapon_axe_metadata.json";

    public override void _Ready()
    {
        _character = GetNodeOrNull<CharacterPaperdoll>("CharacterRoot");

        _idleButton = GetNodeOrNull<Button>("CanvasLayer/Margin/Toolbar/AnimGrid/IdleButton");
        _walkButton = GetNodeOrNull<Button>("CanvasLayer/Margin/Toolbar/AnimGrid/WalkButton");
        _attackButton = GetNodeOrNull<Button>("CanvasLayer/Margin/Toolbar/AnimGrid/AttackButton");
        _hitButton = GetNodeOrNull<Button>("CanvasLayer/Margin/Toolbar/AnimGrid/HitButton");
        _weaponSelect = GetNodeOrNull<OptionButton>("CanvasLayer/Margin/Toolbar/WeaponSelect");
        _modeToggle = GetNodeOrNull<CheckButton>("CanvasLayer/Margin/Toolbar/ModeToggle");
        _facingToggle = GetNodeOrNull<CheckBox>("CanvasLayer/Margin/Toolbar/FacingToggle");

        if (_idleButton != null) _idleButton.Pressed += OnIdlePressed;
        if (_walkButton != null) _walkButton.Pressed += OnWalkPressed;
        if (_attackButton != null) _attackButton.Pressed += OnAttackPressed;
        if (_hitButton != null) _hitButton.Pressed += OnHitPressed;
        if (_weaponSelect != null) _weaponSelect.ItemSelected += OnWeaponSelected;
        if (_modeToggle != null) _modeToggle.Toggled += OnModeToggled;
        if (_facingToggle != null) _facingToggle.Toggled += OnFacingToggled;

        ApplyWeaponSelection();
        _character?.PlayAnimation("idle");
    }

    public override void _ExitTree()
    {
        if (_idleButton != null) _idleButton.Pressed -= OnIdlePressed;
        if (_walkButton != null) _walkButton.Pressed -= OnWalkPressed;
        if (_attackButton != null) _attackButton.Pressed -= OnAttackPressed;
        if (_hitButton != null) _hitButton.Pressed -= OnHitPressed;
        if (_weaponSelect != null) _weaponSelect.ItemSelected -= OnWeaponSelected;
        if (_modeToggle != null) _modeToggle.Toggled -= OnModeToggled;
        if (_facingToggle != null) _facingToggle.Toggled -= OnFacingToggled;
    }

    private void OnIdlePressed() => _character?.PlayAnimation("idle");
    private void OnWalkPressed() => _character?.PlayAnimation("walk");
    private void OnAttackPressed() => _character?.PlayAnimation("attack");
    private void OnHitPressed() => _character?.PlayAnimation("hit");
    private void OnWeaponSelected(long index) => ApplyWeaponSelection();
    private void OnModeToggled(bool toggled) => ApplyWeaponSelection();
    private void OnFacingToggled(bool facingLeft) => _character?.SetFacingLeft(facingLeft);

    private void ApplyWeaponSelection()
    {
        if (_character == null) return;

        bool isAxe = _weaponSelect != null && _weaponSelect.Selected == 1;
        bool isSocketMode = _modeToggle != null && _modeToggle.ButtonPressed;

        if (isSocketMode)
        {
            string cleanPath = isAxe ? AxeCleanPath : SwordCleanPath;
            string metaPath = isAxe ? AxeMetaPath : SwordMetaPath;

            try {
                Vector2I pivot = ReadPivotFromMetadata(metaPath);
                var weaponTex = GD.Load<Texture2D>(cleanPath);
                if (weaponTex == null) {
                    GD.PrintErr($"[PaperdollDemo] Failed to load texture: {cleanPath}");
                    return;
                }
                _character.EquipStaticWeapon(weaponTex, pivot);
            } catch (Exception ex) {
                GD.PrintErr($"[PaperdollDemo] Aborting mode change: {ex.Message}");
            }
        }
        else
        {
            string sheetPath = isAxe ? AxeSheetPath : SwordSheetPath;
            _character.EquipWeaponSheet(sheetPath);
        }
    }

    private static Vector2I ReadPivotFromMetadata(string resourcePath)
    {
        using var file = Godot.FileAccess.Open(resourcePath, Godot.FileAccess.ModeFlags.Read);
        if (file == null)
            throw new Exception($"Cannot open metadata file: {resourcePath}");

        string json = file.GetAsText();
        using var doc = JsonDocument.Parse(json);
        if (doc.RootElement.TryGetProperty("pivot", out var pivotElem))
        {
            int x = pivotElem.GetProperty("x").GetInt32();
            int y = pivotElem.GetProperty("y").GetInt32();
            return new Vector2I(x, y);
        }
        throw new Exception($"Metadata file {resourcePath} missing 'pivot' property.");
    }
}
