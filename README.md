# Agender

**AI-powered natural language assistant for Blender.**

Talk to Blender in plain language. Agender translates your words into safe, white-listed scene operations — no scripting required.

## Features

- **Chat interface** in Blender's sidebar — type commands, see results
- **22 built-in commands**: primitives, transforms, lights, cameras, materials, animation keyframes, rigid body physics, modifiers, rendering
- **Safe execution** — all operations are white-listed, no arbitrary code
- **External bridge** — control Blender from scripts, agents, or automation pipelines via HTTP (port 9876)
- **Works with any OpenAI-compatible LLM API** (OpenRouter, OpenAI, local models, etc.)

## Quick Start

### Install

1. [Download the latest release](https://github.com/nicekwell/Agender/releases) or clone this repo
2. In Blender: **Edit → Preferences → Get Extensions → ▾ → Install from Disk**
3. Select the `agender/` directory (or a zip of it)
4. Enable the extension

### Configure

1. Press **N** in the 3D Viewport to open the sidebar
2. Click the **Agender** tab
3. Expand **Settings** and enter your LLM API key

### Use

Type natural language commands in the chat input:

```
Add a red metallic sphere at (2, 0, 1)
```
```
Create a 120-frame animation of the monkey falling from height 10 to the ground
```
```
Set up three-point lighting for the scene
```
```
Render at 1920x1080 with Cycles, 256 samples
```

## Supported Commands

| Category | Commands |
|----------|----------|
| **Scene** | `add_primitive`, `import_asset`, `set_transform`, `delete_object`, `duplicate_object`, `set_parent`, `collection_new`, `move_to_collection` |
| **Appearance** | `add_light`, `add_camera`, `look_at`, `set_material`, `shade_smooth`, `add_modifier` |
| **Animation** | `set_frame_range`, `set_keyframe`, `keyframe_sequence`, `clear_keyframes`, `add_rigid_body` |
| **Output** | `set_render`, `render` |
| **Query** | `scene_info` |

## External Bridge

Agender runs an HTTP server on `localhost:9876` for external control:

```bash
# Query scene state
curl http://localhost:9876/scene-info

# Execute commands
curl -X POST http://localhost:9876/execute \
  -H "Content-Type: application/json" \
  -d '{"commands": [{"type": "add_primitive", "params": {"type": "cube", "name": "Floor", "size": 10}}]}'
```

A CLI bridge tool is included in `tools/blender_bridge.py`:

```bash
python tools/blender_bridge.py scene-info
python tools/blender_bridge.py execute --commands '[{"type":"shade_smooth","params":{"object":"Suzanne"}}]'
python tools/blender_bridge.py asset-list
```

## Requirements

- Blender 4.2+ (tested on 5.0.1)
- System Python 3.x (`py -3` on Windows) for LLM API calls
- An OpenAI-compatible LLM API key

## License

MIT
