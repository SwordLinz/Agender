"""
Agender — Chat-style AI assistant panel for Blender.

Provides a conversational interface in the 3D Viewport sidebar.
User messages and Agender responses are displayed as a scrollable
chat history with Blender-native styling.
"""

import bpy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from . import executor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wrap(text, width=42):
    """Word-wrap text to fit in the sidebar."""
    lines = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        words = raw.split(" ")
        buf = ""
        for w in words:
            if buf and len(buf) + 1 + len(w) > width:
                lines.append(buf)
                buf = w
            else:
                buf = f"{buf} {w}" if buf else w
        if buf:
            lines.append(buf)
    return lines or [""]


def _format_results(results):
    """Format command results into a compact chat-friendly summary."""
    lines = []
    ok = 0
    for r in results:
        ct = r.get("type", "?")
        if r.get("ok"):
            ok += 1
            detail = ""
            if "object" in r:
                detail = f" → {r['object']}"
            elif "imported" in r:
                detail = f" → {', '.join(r['imported'])}"
            elif "deleted" in r:
                detail = f" → {r['deleted']}"
            elif "collection" in r:
                detail = f" → {r['collection']}"
            elif "frames" in r:
                detail = f" ({len(r['frames'])} keyframes)"
            elif "frame_range" in r:
                fr = r["frame_range"]
                detail = f" → {fr[0]}-{fr[1]}"
            lines.append(f"✓ {ct}{detail}")
        else:
            err = r.get("error", "unknown")[:60]
            lines.append(f"✗ {ct}: {err}")
    lines.append(f"{ok}/{len(results)} OK")
    return "\n".join(lines)


def _extract_json(text):
    """Extract JSON array or object from LLM output."""
    fenced = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    for opener, closer in [("[", "]"), ("{", "}")]:
        start = text.find(opener)
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
    return None


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class AgenderMessage(bpy.types.PropertyGroup):
    role: bpy.props.StringProperty()  # "user" | "agent"
    content: bpy.props.StringProperty()


class AgenderProperties(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(
        name="",
        description="Tell Agender what to do...",
    )
    messages: bpy.props.CollectionProperty(type=AgenderMessage)
    api_base: bpy.props.StringProperty(
        name="API Base",
        default="https://openrouter.ai/api/v1",
    )
    api_key: bpy.props.StringProperty(
        name="API Key",
        subtype="PASSWORD",
    )
    model_id: bpy.props.StringProperty(
        name="Model",
        default="xiaomi/mimo-v2-pro",
    )
    is_thinking: bpy.props.BoolProperty(default=False)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are Agender, an AI assistant for Blender. Output ONLY a valid JSON array of commands. No explanation, no markdown.

Commands — Scene:
- {"type":"scene_info","params":{}}
- {"type":"import_asset","params":{"filepath":"...","name":"?","location":[x,y,z],"rotation":[rx,ry,rz],"scale":1.0}}
- {"type":"add_primitive","params":{"type":"cube|sphere|cylinder|cone|plane|torus|monkey","name":"?","location":[x,y,z],"size":1.0,"rotation":[rx,ry,rz],"scale":[sx,sy,sz]}}
- {"type":"set_transform","params":{"object":"name","location":[x,y,z],"rotation":[rx,ry,rz],"scale":[sx,sy,sz]}}
- {"type":"delete_object","params":{"object":"name"}}
- {"type":"duplicate_object","params":{"object":"name","name":"?","offset":[dx,dy,dz]}}
- {"type":"set_parent","params":{"child":"name","parent":"name"}}
- {"type":"collection_new","params":{"name":"..."}}
- {"type":"move_to_collection","params":{"object":"name","collection":"name"}}

Commands — Appearance:
- {"type":"add_light","params":{"type":"POINT|SUN|SPOT|AREA","name":"?","location":[x,y,z],"rotation":[rx,ry,rz],"energy":1000,"color":[r,g,b],"size":1.0}}
- {"type":"add_camera","params":{"name":"?","location":[x,y,z],"rotation":[rx,ry,rz],"lens":50,"set_active":true}}
- {"type":"look_at","params":{"object":"name","target":[x,y,z]}}
- {"type":"set_material","params":{"object":"name","name":"mat","color":[r,g,b],"metallic":0.0,"roughness":0.5}}
- {"type":"shade_smooth","params":{"object":"name","smooth":true}}
- {"type":"add_modifier","params":{"object":"name","modifier_type":"SUBSURF|MIRROR|ARRAY|SOLIDIFY|BEVEL","name":"?","settings":{"levels":2}}}

Commands — Animation:
- {"type":"set_frame_range","params":{"start":1,"end":120,"fps":24}}
- {"type":"set_keyframe","params":{"object":"name","frame":1,"location":[x,y,z],"rotation":[rx,ry,rz],"scale":[sx,sy,sz],"interpolation":"LINEAR|BEZIER|CONSTANT"}}
- {"type":"keyframe_sequence","params":{"object":"name","interpolation":"LINEAR","keyframes":[{"frame":1,"location":[0,0,5]},{"frame":60,"location":[0,0,0]}]}}
- {"type":"clear_keyframes","params":{"object":"name"}}
- {"type":"add_rigid_body","params":{"object":"name","type":"ACTIVE|PASSIVE","mass":1.0,"friction":0.5,"restitution":0.3,"collision_shape":"CONVEX_HULL|MESH|BOX|SPHERE"}}

Commands — Render:
- {"type":"set_render","params":{"engine":"eevee|cycles","resolution_x":1920,"resolution_y":1080,"samples":128,"format":"png"}}
- {"type":"render","params":{"output_path":"...","animation":false}}

Rules:
- Output ONLY a valid JSON array. No prose, no markdown fences.
- rotation: degrees. color: 0.0-1.0 RGB. location: meters.
- Reference objects by exact name from scene context.
- Give new objects meaningful names.
- For falling/bouncing: prefer keyframe_sequence or add_rigid_body.

Current scene:
{scene_context}"""


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class AGENDER_OT_send(bpy.types.Operator):
    """Send your message to Agender"""
    bl_idname = "agender.send"
    bl_label = "Send"
    bl_options = {"REGISTER"}

    _thread = None
    _commands = None
    _error = None
    _timer = None

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._thread is not None and self._thread.is_alive():
            return {"PASS_THROUGH"}

        context.window_manager.event_timer_remove(self._timer)
        self._timer = None
        props = context.scene.agender
        props.is_thinking = False

        if self._error:
            msg = props.messages.add()
            msg.role = "agent"
            msg.content = f"✗ {self._error[:200]}"
        elif not self._commands:
            msg = props.messages.add()
            msg.role = "agent"
            msg.content = "I couldn't produce commands for that. Try rephrasing?"
        else:
            try:
                results = executor.execute_commands(self._commands)
                msg = props.messages.add()
                msg.role = "agent"
                msg.content = _format_results(results)
            except Exception as e:
                msg = props.messages.add()
                msg.role = "agent"
                msg.content = f"✗ Execution error: {str(e)[:150]}"

        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}

    def execute(self, context):
        props = context.scene.agender
        prompt = props.prompt.strip()
        if not prompt:
            return {"CANCELLED"}
        if not props.api_key:
            self.report({"WARNING"}, "Set API Key in Agender > Settings")
            return {"CANCELLED"}

        # Add user message to chat
        msg = props.messages.add()
        msg.role = "user"
        msg.content = prompt
        props.prompt = ""
        props.is_thinking = True

        # Gather scene context on main thread
        try:
            info = executor._execute_one({"type": "scene_info", "params": {}})
            scene_ctx = json.dumps(info, ensure_ascii=False)
        except Exception:
            scene_ctx = "{}"

        system = _SYSTEM_PROMPT.replace("{scene_context}", scene_ctx)

        self._commands = None
        self._error = None
        self._thread = threading.Thread(
            target=self._llm_call,
            args=(props.api_base, props.api_key, props.model_id, system, prompt),
            daemon=True,
        )
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)

        for area in context.screen.areas:
            area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _llm_call(self, api_base, api_key, model_id, system, prompt):
        try:
            url = f"{api_base}/chat/completions"
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            }

            tmp_dir = tempfile.mkdtemp(prefix="agender_")
            payload_path = os.path.join(tmp_dir, "payload.json")
            config_path = os.path.join(tmp_dir, "config.json")
            script_path = os.path.join(tmp_dir, "llm_call.py")

            with open(payload_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"url": url, "key": api_key}, f)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(
                    "import json,urllib.request,ssl,sys,os\n"
                    "d=os.path.dirname(os.path.abspath(__file__))\n"
                    "with open(os.path.join(d,'config.json'),'r') as f: cfg=json.load(f)\n"
                    "with open(os.path.join(d,'payload.json'),'r',encoding='utf-8') as f: body=f.read().encode('utf-8')\n"
                    "req=urllib.request.Request(cfg['url'],data=body,headers={"
                    "'Content-Type':'application/json','Authorization':'Bearer '+cfg['key']})\n"
                    "ctx=ssl.create_default_context()\n"
                    "with urllib.request.urlopen(req,timeout=120,context=ctx) as r:\n"
                    " sys.stdout.buffer.write(r.read())\n"
                )

            # Try py -3 first (Windows), fall back to python3 / python
            for cmd in (["py", "-3"], ["python3"], ["python"]):
                try:
                    result = subprocess.run(
                        cmd + [script_path],
                        capture_output=True, timeout=130,
                    )
                    break
                except FileNotFoundError:
                    continue
            else:
                raise RuntimeError("No system Python found (tried py, python3, python)")

            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if api_key and len(api_key) > 8:
                    stderr = stderr.replace(api_key, "***")
                raise RuntimeError(f"LLM call failed: {stderr[:300]}")

            raw = result.stdout.decode("utf-8", errors="replace")
            if not raw.strip():
                raise RuntimeError("Empty response from LLM API")

            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"].strip()
            print(f"[Agender] LLM response:\n{content}")

            parsed = _extract_json(content)
            if parsed is None:
                self._error = f"Could not parse response: {content[:150]}"
            elif isinstance(parsed, dict):
                self._commands = [parsed]
            elif isinstance(parsed, list):
                self._commands = parsed if parsed else None
                if not parsed:
                    self._error = f"Empty command list: {content[:150]}"
            else:
                self._error = f"Unexpected format: {type(parsed).__name__}"
        except Exception as e:
            self._error = str(e)


class AGENDER_OT_scene_info(bpy.types.Operator):
    """Show current scene summary in chat"""
    bl_idname = "agender.scene_info"
    bl_label = "Scene"

    def execute(self, context):
        props = context.scene.agender
        result = executor._execute_one({"type": "scene_info", "params": {}})

        lines = [f"Scene: {result.get('scene', '?')}"]
        lines.append(f"Objects: {result.get('object_count', 0)}")
        for obj in result.get("objects", [])[:12]:
            lines.append(f"  • {obj['name']} ({obj['type']})")
        if result.get("object_count", 0) > 12:
            lines.append(f"  ... +{result['object_count'] - 12} more")
        fr = result.get("frame_range", [1, 250])
        lines.append(f"Frames: {fr[0]}-{fr[1]}")
        lines.append(f"Engine: {result.get('render_engine', '?')}")

        msg = props.messages.add()
        msg.role = "agent"
        msg.content = "\n".join(lines)

        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class AGENDER_OT_clear(bpy.types.Operator):
    """Clear chat history"""
    bl_idname = "agender.clear"
    bl_label = "Clear"

    def execute(self, context):
        context.scene.agender.messages.clear()
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class AGENDER_PT_chat(bpy.types.Panel):
    bl_label = "Agender"
    bl_idname = "AGENDER_PT_chat"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Agender"

    def draw(self, context):
        layout = self.layout
        props = context.scene.agender

        # --- Welcome ---
        if len(props.messages) == 0 and not props.is_thinking:
            box = layout.box()
            col = box.column(align=True)
            col.scale_y = 0.85
            col.label(text="Hi, I'm Agender.", icon="LIGHT")
            col.label(text="Your AI scene assistant.")
            col.separator()
            col.label(text="Try something like:")
            col.label(text="  'Add a red sphere at (2,0,1)'")
            col.label(text="  'Set up three-point lighting'")
            col.label(text="  'Animate the cube falling'")

        # --- Chat history ---
        max_shown = 30
        start_idx = max(0, len(props.messages) - max_shown)

        for i in range(start_idx, len(props.messages)):
            msg = props.messages[i]

            if msg.role == "user":
                box = layout.box()
                row = box.row(align=True)
                row.alignment = "LEFT"
                icon_col = row.column()
                icon_col.scale_x = 0.3
                icon_col.label(text="", icon="USER")
                text_col = row.column()
                text_col.scale_y = 0.85
                for line in _wrap(msg.content, 36):
                    text_col.label(text=line)
            else:
                box = layout.box()
                col = box.column(align=True)
                col.scale_y = 0.85
                content_lines = msg.content.split("\n")
                for j, line in enumerate(content_lines):
                    if not line:
                        continue
                    icon = "NONE"
                    if line.startswith("✓"):
                        icon = "CHECKMARK"
                        line = line[1:].strip()
                    elif line.startswith("✗"):
                        icon = "ERROR"
                        line = line[1:].strip()
                    elif j == 0:
                        icon = "LIGHT"

                    for wrapped in _wrap(line, 38):
                        col.label(text=wrapped, icon=icon)
                        icon = "NONE"

        # --- Thinking indicator ---
        if props.is_thinking:
            box = layout.box()
            row = box.row()
            row.alignment = "CENTER"
            row.label(text="Thinking...", icon="SORTTIME")

        layout.separator()

        # --- Input ---
        row = layout.row(align=True)
        row.prop(props, "prompt", text="", icon="OUTLINER_DATA_GP_LAYER")
        sub = row.row(align=True)
        sub.scale_x = 0.15
        sub.enabled = not props.is_thinking
        sub.operator("agender.send", text="", icon="PLAY")

        # --- Toolbar ---
        row = layout.row(align=True)
        row.scale_y = 0.85
        row.operator("agender.scene_info", icon="OUTLINER")
        row.operator("agender.clear", icon="TRASH")


class AGENDER_PT_settings(bpy.types.Panel):
    bl_label = "Settings"
    bl_idname = "AGENDER_PT_settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Agender"
    bl_parent_id = "AGENDER_PT_chat"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        props = context.scene.agender
        layout.prop(props, "api_base")
        layout.prop(props, "api_key")
        layout.prop(props, "model_id")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    AgenderMessage,
    AgenderProperties,
    AGENDER_OT_send,
    AGENDER_OT_scene_info,
    AGENDER_OT_clear,
    AGENDER_PT_chat,
    AGENDER_PT_settings,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.agender = bpy.props.PointerProperty(type=AgenderProperties)


def unregister():
    del bpy.types.Scene.agender
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
