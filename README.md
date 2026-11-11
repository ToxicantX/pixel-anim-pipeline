# Pixel anim pipeline

将单张侧视像素角色图处理为分层 Aseprite 动画，并导出游戏可用的 PNG 图集、JSON 和 GIF。通过本地 [MCP 服务](mcp_server/server.py) 调用，支持角色与武器分离的 paperdoll 换装流程，附带 Godot C# 示例。

## 主要功能

- 图片预处理：移除与边界连通的品红背景、清理杂点、最近邻缩放、限色和底部居中，可选描边与 ENDESGA 调色板。
- ASCII 检查：读取角色像素和颜色，辅助选择头部、躯干切分行；读取武器并建议握柄 pivot。
- 程序化动画：分层生成 `idle`、`walk`、`attack`、`hit`，包含阴影和攻击特效。它通过移动、染色基础身体部件形成动作，不会重绘新姿势。
- 换装导出：隐藏手部插槽记录每帧位置和角度，每种武器独立输出叠加图集、合成预览和插槽数据，保留原角色文件。
- Godot 接入：按 JSON 标签和逐帧时长构建 SpriteFrames，支持武器图集叠加、动态插槽换装和左右朝向。

详细提示词、参数和插槽协议见 [pixel-animator 使用流程](.skills/pixel-animator.md)。

## 安装与本地配置

以下命令在仓库根目录的 PowerShell 中执行。建议使用 Python 3.12；需安装支持批处理和 Lua 脚本的 Aseprite。Python 依赖以 [requirements.txt](requirements.txt) 为准。

已有可用本地配置时，跳过两条 `Copy-Item` 命令，避免覆盖。若未安装 `py` 启动器，使用 Python 3.12 的可执行文件替换 `py -3.12`。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .mcp.example.json .mcp.json
Copy-Item mcp_server/config.example.json mcp_server/config.json
```

编辑复制后的本地配置：

1. 在 [MCP 客户端模板](.mcp.example.json) 对应配置中，把 `command` 改为本仓库虚拟环境 Python 的绝对路径。
2. 把 `args` 中的服务脚本改为绝对路径，或确保 MCP 客户端以仓库根目录为工作目录。
3. 把 `env.ASEPRITE_PATH` 改为自己的 Aseprite 可执行文件路径。若改用 [服务配置模板](mcp_server/config.example.json) 中的 `aseprite_path`，从 MCP 配置的 `env` 中删除 `ASEPRITE_PATH` 占位项。环境变量优先，其次本地配置，最后查找 PATH 中的 `aseprite`。
4. 在支持 MCP 的客户端中加载配置并连接服务。不同客户端的配置位置不同，复制模板不会自动启用连接。

服务使用 **stdio**，不是 HTTP 服务。手动启动入口如下，通常由 MCP 客户端负责启动：

```powershell
.\.venv\Scripts\python.exe -B mcp_server/server.py
```

工具参数中的相对文件路径按仓库根目录解析。服务通过 Aseprite 批处理生成文件，不会自动在编辑器窗口中打开结果。

本地配置、虚拟环境以及 [input](input)、[output](output) 中的生成素材由 [.gitignore](.gitignore) 排除，保留在本机。不要提交机器专属路径。

## 角色动画流程

先准备一张完整、朝右的侧视角色图，头、躯干和腿的边界应清晰。推荐纯 `#FF00FF` 背景，角色本身避免使用品红色。预处理只移除与图像边缘连通的品红背景，被角色包围的品红像素会保留。

在 MCP 客户端依次调用以下工具，路径可按需替换：

1. `preprocess_gpt_sprite(input_path="input/hero.png", output_path="output/hero_clean.png", canvas_size=32, max_colors=16)`：得到透明背景的 clean PNG。
2. `inspect_sprite_ascii(sprite_path="output/hero_clean.png")`：逐行检查颜色和身体边界，选择 `head_end_y`、`torso_end_y`。
3. `generate_character_animations(base_png_path="output/hero_clean.png", output_ase_path="output/hero.aseprite", head_end_y=..., torso_end_y=...)`：传入上一步确定的实际行号，省略号不能直接作为参数。
4. 在 Aseprite 中打开结果，检查颈部和腰部接缝、脚部着地与动作连续性。
5. `export_game_assets(ase_path="output/hero.aseprite", output_dir="output/hero", asset_name="hero")`：导出横向图集、Aseprite `json-array` 数据和放大 GIF 预览。

切分行从零开始且包含结束行：头部到 `head_end_y` 为止，躯干到 `torso_end_y` 为止，其余为腿部。必须满足 `0 <= head_end_y < torso_end_y < H - 1`，各部件均需有不透明像素。

[动画生成器](mcp_server/server.py) 固定生成 16 帧：`idle` 4 帧、`walk` 6 帧、`attack` 4 帧、`hit` 2 帧。JSON 中标签范围从零开始，Aseprite 界面帧号从一开始。

## Paperdoll 与武器换装

准备空手角色和独立武器图片。武器应竖直朝上，尖端在上、握柄在下；坐标均从零开始，x 向右、y 向下，角度以朝上为零度、顺时针为正。

1. 预处理角色并读取 ASCII。在 **clean PNG** 中选择切分行和拳头握持点 `hand_x`、`hand_y`，不要使用原始大图坐标。握持点必须是躯干区域内的不透明像素。
2. 调用 `generate_character_with_socket(base_png_path, output_ase_path, head_end_y, torso_end_y, hand_x, hand_y)`，生成含隐藏 `_GripSocket` 层的角色。保持此层隐藏，不要改色或绘画。
3. 用 `export_game_assets` 导出角色身体图集。
4. 对每种武器调用 `preprocess_gpt_sprite`，可使用 `canvas_size=24`。随后调用 `inspect_weapon_ascii(weapon_png_path)`，确认建议 pivot 确实落在握柄上。
5. 调用 `equip_and_export_weapon(character_ase_path, weapon_png_path, output_dir, weapon_id, pivot_x, pivot_y)`，为每种武器使用不同的 `weapon_id`。

[武器工具](mcp_server/weapon_tools.py) 输出装备后的 Aseprite 副本、武器专用 PNG/JSON 图集、合成 GIF、`grip_sockets.json` 和武器 metadata。身体与武器图集保持相同帧序、画布、标签和时长，游戏中应使用同一帧时钟和绘制原点，避免裁边或重新打包破坏对齐。

武器图集使用角色画布，长武器旋转后可能被裁切。检查返回的 `warnings`、`clipping` 和 metadata；可缩短武器，或在引擎中用静态武器加动态插槽绘制。最近邻旋转在非直角时会丢弃或重复像素，并非无损，需检查细刀刃和边缘。

## Godot C# 示例

使用 Godot **4.7.2 .NET 版**和 **.NET 10 SDK**，版本依据 [C# 项目配置](godot/PixelPipeline.csproj)。仓库自带 [示例素材](godot/assets/paperdoll)，首次体验无需重新生成。

```powershell
dotnet build godot/PixelPipeline.csproj
$godot = "C:/Tools/Godot/Godot_v4.7.2-stable_mono_win64_console.exe"
& $godot --editor --path godot
```

将 `$godot` 替换为自己的 .NET 版可执行文件路径。也可在项目管理器中导入 [project.godot](godot/project.godot)，等待资源导入和 C# 编译完成后按 **F5**。主场景提供动作按钮、剑/斧选择、图集/动态插槽切换及左右朝向切换。

复用 [CharacterPaperdoll 场景](godot/Scenes/CharacterPaperdoll.tscn) 时，在根节点 Inspector 设置 `Character Sheet Png`、`Character Sheet Json`、`Grip Sockets Json` 和 `Save Sprite Frames Path`。点击 **Bake Sprite Frames**，将 JSON 标签与逐帧时长烘焙为 `.tres` 并绑定身体动画。资源更新后重新烘焙。

接入代码见 [SpriteFrames 构建器](godot/Scripts/AsepriteSpriteFramesBuilder.cs) 与 [角色控制器](godot/Scripts/CharacterPaperdoll.cs)。控制器通过 `EquipWeaponSheet` 接收 `res://` PNG 路径，通过 `EquipStaticWeapon(Texture2D, Vector2I)` 接收静态武器和握柄 pivot。

**导出游戏时，把 `*.json` 加入非资源文件导出过滤器**，包含角色图集 JSON、插槽及所用武器 metadata。示例在运行时读取这些文件，仅包含 PNG 或烘焙 `.tres` 不足以运行完整换装流程。

更新示例素材时，[prepare_godot_demo.py](scripts/prepare_godot_demo.py) 从本地 `input/paperdoll` 与 `output/paperdoll` 复制文件。如果缺少来源素材，先运行 paperdoll 示例生成，再执行复制：

```powershell
.\.venv\Scripts\python.exe -B scripts/test_paperdoll.py
.\.venv\Scripts\python.exe -B scripts/prepare_godot_demo.py
```

## 使用限制与可选检查

- 预处理支持 8-1024 像素画布、2-256 色；动画输入 PNG 每边限 8-256 像素，并需留透明顶部空间。
- ASCII 角色检查限 256×256，最多使用 71 种不透明颜色。切分与握柄建议需要人工确认。
- `run_custom_lua` 执行具有当前进程文件权限的本地 Lua，**没有沙箱**；只运行可信脚本，不要直接执行远程或不可信内容。

需要检查本地安装和导出链路时，可运行 [基础流程检查](scripts/test_pipeline.py) 或 [paperdoll 检查](scripts/test_paperdoll.py)。这些命令会生成本地输入/输出素材，并依赖前述 Aseprite 配置：

```powershell
.\.venv\Scripts\python.exe -B scripts/test_pipeline.py
.\.venv\Scripts\python.exe -B scripts/test_paperdoll.py
```
