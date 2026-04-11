"""
Agender — AI chat panel for Blender (History + Chat panels).

Features:
 - OpenRouter models, session files under ~/.agender/sessions/
 - Optional reference image: Browse / paste file path / paste bitmap (Pillow)
 - Multimodal user messages (data URL) when an image is attached
 - Dock-left (single split), VIEW_3D + TEXT_EDITOR sidebars
"""

import base64
import bpy
import json
from bpy_extras.io_utils import ImportHelper
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid as _uuid
from datetime import datetime, timedelta
from . import executor


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Session persistence  (~/.agender/sessions/*.json)                      ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

_SESS_DIR = os.path.join(os.path.expanduser("~"), ".agender", "sessions")
_sess_cache: list = []
_sess_ts: float = 0.0


def _ensure_dir():
    os.makedirs(_SESS_DIR, exist_ok=True)


def _save_current(props):
    """Persist the active session to disk."""
    if len(props.messages) == 0:
        return
    _ensure_dir()
    uid = props.active_session_uid
    if not uid:
        uid = str(_uuid.uuid4())
        props.active_session_uid = uid
    title = "New Chat"
    for m in props.messages:
        if m.role == "user":
            title = m.content[:42].strip()
            break
    ts = props.session_timestamp
    if ts < 1.0:
        ts = time.time()
        props.session_timestamp = ts
    data = {
        "uid": uid,
        "title": title,
        "timestamp": ts,
        "messages": [{"role": m.role, "content": m.content} for m in props.messages],
    }
    path = os.path.join(_SESS_DIR, f"{uid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _refresh_sessions()


def _load_into(props, uid):
    """Load a session from disk into the active props."""
    path = os.path.join(_SESS_DIR, f"{uid}.json")
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    props.messages.clear()
    for m in data.get("messages", []):
        msg = props.messages.add()
        msg.role = m["role"]
        msg.content = m["content"]
    props.active_session_uid = uid
    props.session_timestamp = data.get("timestamp", time.time())
    return True


def _delete_session(uid):
    path = os.path.join(_SESS_DIR, f"{uid}.json")
    try:
        os.remove(path)
    except OSError:
        pass
    _refresh_sessions()


def _refresh_sessions():
    global _sess_cache, _sess_ts
    _ensure_dir()
    items = []
    for fn in os.listdir(_SESS_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_SESS_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            items.append({
                "uid": d["uid"],
                "title": d.get("title", "Untitled")[:42],
                "timestamp": d.get("timestamp", 0),
            })
        except Exception:
            pass
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    _sess_cache = items
    _sess_ts = time.time()


def _get_sessions():
    if time.time() - _sess_ts > 3.0:
        _refresh_sessions()
    return _sess_cache


def _date_group(ts):
    d = datetime.fromtimestamp(ts).date()
    today = datetime.now().date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if d > today - timedelta(days=7):
        return "Last 7 Days"
    if d > today - timedelta(days=30):
        return "Last 30 Days"
    return "Older"


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Helpers                                                                ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def _wrap(text, width=42):
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
    lines, ok = [], 0
    for r in results:
        ct = r.get("type", "?")
        if r.get("ok"):
            ok += 1
            detail = ""
            for key in ("object", "collection", "deleted"):
                if key in r:
                    detail = f" → {r[key]}"
                    break
            if "imported" in r:
                detail = f" → {', '.join(r['imported'])}"
            if "frames" in r:
                detail = f" ({len(r['frames'])} kf)"
            if "frame_range" in r:
                fr = r["frame_range"]
                detail = f" → {fr[0]}-{fr[1]}"
            lines.append(f"✓ {ct}{detail}")
        else:
            lines.append(f"✗ {ct}: {r.get('error', '?')[:50]}")
    lines.append(f"{ok}/{len(results)} OK")
    return "\n".join(lines)


def _extract_json(text):
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
                            return json.loads(text[start : i + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
    return None


def _effective_model(props):
    if props.custom_model and props.custom_model.strip():
        return props.custom_model.strip()
    return props.model_preset


_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
}


def _mime_for_ext(ext):
    ext = ext.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(ext, "application/octet-stream")


def _normalize_clipboard_path(raw):
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if s.lower().startswith("file://"):
        s = s[7:]
        if len(s) >= 3 and s[0] == "/" and s[2] == ":":
            s = s[1:]
    return os.path.normpath(s)


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Properties                                                             ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

_MODEL_LIST = [
    ("xiaomi/mimo-v2-pro", "MiMo v2 Pro", "Xiaomi MiMo V2 Pro — fast, capable"),
    ("openai/gpt-4o", "GPT-4o", "OpenAI GPT-4o"),
    ("anthropic/claude-sonnet-4", "Claude Sonnet 4", "Anthropic Claude Sonnet 4"),
    ("google/gemini-2.5-flash-preview", "Gemini 2.5 Flash", "Google Gemini 2.5 Flash"),
    ("deepseek/deepseek-chat-v3-0324", "DeepSeek V3", "DeepSeek Chat V3"),
    ("qwen/qwen3-235b-a22b", "Qwen3 235B", "Alibaba Qwen3 MoE 235B"),
]


class AgenderMessage(bpy.types.PropertyGroup):
    role: bpy.props.StringProperty()
    content: bpy.props.StringProperty()


class AgenderProperties(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(
        name="",
        description="Tell Agender what to do…",
    )
    messages: bpy.props.CollectionProperty(type=AgenderMessage)

    # LLM
    api_base: bpy.props.StringProperty(
        name="API Base",
        default="https://openrouter.ai/api/v1",
    )
    api_key: bpy.props.StringProperty(name="API Key", subtype="PASSWORD")
    model_preset: bpy.props.EnumProperty(
        name="Model",
        items=_MODEL_LIST,
        default="xiaomi/mimo-v2-pro",
    )
    custom_model: bpy.props.StringProperty(
        name="Custom Model",
        description="Enter a custom model ID to override the dropdown",
    )

    # Optional reference image (vision / multimodal APIs)
    image_path: bpy.props.StringProperty(
        name="Image",
        description="Reference image file sent with the next message (vision models)",
        default="",
        subtype="FILE_PATH",
        maxlen=1024,
    )

    # State
    is_thinking: bpy.props.BoolProperty(default=False)
    active_session_uid: bpy.props.StringProperty()
    session_timestamp: bpy.props.FloatProperty()


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  System prompt                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

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
- If the user attached a reference image, use it only as context; still output ONLY the JSON array.

Current scene:
{scene_context}"""


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Operators                                                              ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class AGENDER_OT_send(bpy.types.Operator):
    """Send message to Agender"""
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
            msg.content = "Couldn't produce commands. Try rephrasing?"
        else:
            try:
                results = executor.execute_commands(self._commands)
                msg = props.messages.add()
                msg.role = "agent"
                msg.content = _format_results(results)
            except Exception as e:
                msg = props.messages.add()
                msg.role = "agent"
                msg.content = f"✗ {str(e)[:150]}"

        _save_current(props)
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}

    def execute(self, context):
        props = context.scene.agender
        prompt = props.prompt.strip()
        img_raw = (props.image_path or "").strip().strip('"').strip("'")
        img_path = os.path.normpath(img_raw) if img_raw else ""
        has_img = bool(
            img_path and os.path.isfile(img_path)
            and os.path.splitext(img_path)[1].lower() in _IMAGE_EXTS
        )

        if not prompt and not has_img:
            self.report({"WARNING"}, "Enter text or attach an image")
            return {"CANCELLED"}
        if not props.api_key:
            self.report({"WARNING"}, "Set your API Key in Agender ▸ Settings first")
            return {"CANCELLED"}

        if not props.active_session_uid:
            props.active_session_uid = str(_uuid.uuid4())
            props.session_timestamp = time.time()

        lines = []
        if prompt:
            lines.append(prompt)
        if has_img:
            lines.append(f"📷 {os.path.basename(img_path)}")
        msg = props.messages.add()
        msg.role = "user"
        msg.content = "\n".join(lines) if lines else "📷 (image)"
        props.prompt = ""
        props.image_path = ""
        props.is_thinking = True

        img_for_api = img_path if has_img else ""

        try:
            info = executor._execute_one({"type": "scene_info", "params": {}})
            scene_ctx = json.dumps(info, ensure_ascii=False)
        except Exception:
            scene_ctx = "{}"

        system = _SYSTEM_PROMPT.replace("{scene_context}", scene_ctx)
        model = _effective_model(props)

        self._commands = None
        self._error = None
        self._thread = threading.Thread(
            target=self._llm_call,
            args=(props.api_base, props.api_key, model, system, prompt, img_for_api),
            daemon=True,
        )
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)

        for area in context.screen.areas:
            area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _llm_call(self, api_base, api_key, model_id, system, prompt, image_path):
        try:
            url = f"{api_base}/chat/completions"
            user_text = prompt if prompt else "(Use the attached image as context for Blender.)"
            if image_path and os.path.isfile(image_path):
                ext = os.path.splitext(image_path)[1].lower()
                if ext not in _IMAGE_EXTS:
                    raise RuntimeError(f"Unsupported image type: {ext}")
                with open(image_path, "rb") as f:
                    b64 = base64.standard_b64encode(f.read()).decode("ascii")
                mime = _mime_for_ext(ext)
                user_message = [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ]
            else:
                user_message = user_text

            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
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

            for cmd in (["py", "-3"], ["python3"], ["python"]):
                try:
                    result = subprocess.run(
                        cmd + [script_path],
                        capture_output=True,
                        timeout=130,
                    )
                    break
                except FileNotFoundError:
                    continue
            else:
                raise RuntimeError("No system Python found (tried py -3 / python3 / python)")

            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if api_key and len(api_key) > 8:
                    stderr = stderr.replace(api_key, "***")
                raise RuntimeError(f"LLM subprocess error: {stderr[:300]}")

            raw = result.stdout.decode("utf-8", errors="replace")
            if not raw.strip():
                raise RuntimeError("Empty response from LLM API")

            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"].strip()
            print(f"[Agender] LLM ({model_id}):\n{content}")

            parsed = _extract_json(content)
            if parsed is None:
                self._error = f"Can't parse LLM output: {content[:150]}"
            elif isinstance(parsed, dict):
                self._commands = [parsed]
            elif isinstance(parsed, list):
                self._commands = parsed or None
                if not parsed:
                    self._error = f"Empty command list: {content[:150]}"
            else:
                self._error = f"Unexpected type: {type(parsed).__name__}"
        except Exception as e:
            self._error = str(e)


class AGENDER_OT_new_chat(bpy.types.Operator):
    """Start a new chat session"""
    bl_idname = "agender.new_chat"
    bl_label = "New Chat"

    def execute(self, context):
        props = context.scene.agender
        _save_current(props)
        props.messages.clear()
        props.active_session_uid = str(_uuid.uuid4())
        props.session_timestamp = time.time()
        props.prompt = ""
        props.image_path = ""
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class AGENDER_OT_load_session(bpy.types.Operator):
    """Load a past chat session"""
    bl_idname = "agender.load_session"
    bl_label = "Load"
    session_uid: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.agender
        if self.session_uid == props.active_session_uid:
            return {"CANCELLED"}
        _save_current(props)
        if _load_into(props, self.session_uid):
            props.image_path = ""
            for area in context.screen.areas:
                area.tag_redraw()
        else:
            self.report({"WARNING"}, "Session file not found")
        return {"FINISHED"}


class AGENDER_OT_delete_session(bpy.types.Operator):
    """Delete a chat session"""
    bl_idname = "agender.delete_session"
    bl_label = "Delete"
    session_uid: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.agender
        _delete_session(self.session_uid)
        if props.active_session_uid == self.session_uid:
            props.messages.clear()
            props.active_session_uid = str(_uuid.uuid4())
            props.session_timestamp = time.time()
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class AGENDER_OT_scene_info(bpy.types.Operator):
    """Query scene and show summary in chat"""
    bl_idname = "agender.scene_info"
    bl_label = "Scene"

    def execute(self, context):
        props = context.scene.agender
        if not props.active_session_uid:
            props.active_session_uid = str(_uuid.uuid4())
            props.session_timestamp = time.time()
        result = executor._execute_one({"type": "scene_info", "params": {}})
        lines = [
            f"Scene: {result.get('scene', '?')}  |  "
            f"{result.get('object_count', 0)} objects"
        ]
        for obj in result.get("objects", [])[:12]:
            lines.append(f"  • {obj['name']} ({obj['type']})")
        if result.get("object_count", 0) > 12:
            lines.append(f"  … +{result['object_count'] - 12} more")
        fr = result.get("frame_range", [1, 250])
        lines.append(f"Frames {fr[0]}–{fr[1]}  |  {result.get('render_engine', '?')}")
        msg = props.messages.add()
        msg.role = "agent"
        msg.content = "\n".join(lines)
        _save_current(props)
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class AGENDER_OT_clear(bpy.types.Operator):
    """Clear current chat (does not delete session)"""
    bl_idname = "agender.clear"
    bl_label = "Clear"

    def execute(self, context):
        props = context.scene.agender
        props.messages.clear()
        props.active_session_uid = str(_uuid.uuid4())
        props.session_timestamp = time.time()
        props.image_path = ""
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class AGENDER_OT_browse_image(bpy.types.Operator, ImportHelper):
    bl_idname = "agender.browse_image"
    bl_label = "Reference Image"
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp;*.tif;*.tiff",
        options={"HIDDEN"},
    )

    def execute(self, context):
        context.scene.agender.image_path = self.filepath
        return {"FINISHED"}


class AGENDER_OT_paste_image_path(bpy.types.Operator):
    bl_idname = "agender.paste_image_path"
    bl_label = "Paste Image Path"
    bl_description = "Use file path from clipboard (copy path in Explorer)"

    def execute(self, context):
        cb = _normalize_clipboard_path(context.window_manager.clipboard)
        if not cb:
            self.report({"WARNING"}, "Clipboard is empty")
            return {"CANCELLED"}
        if not os.path.isfile(cb):
            self.report({"WARNING"}, "Clipboard is not an existing file path")
            return {"CANCELLED"}
        ext = os.path.splitext(cb)[1].lower()
        if ext not in _IMAGE_EXTS:
            self.report({"WARNING"}, "Not a supported image file type")
            return {"CANCELLED"}
        context.scene.agender.image_path = cb
        self.report({"INFO"}, f"Image set: {os.path.basename(cb)}")
        return {"FINISHED"}


class AGENDER_OT_paste_image_clipboard(bpy.types.Operator):
    bl_idname = "agender.paste_image_clipboard"
    bl_label = "Paste Image"
    bl_description = (
        "Save bitmap from clipboard via system Python + Pillow "
        "(py -3 -m pip install pillow)"
    )

    def execute(self, context):
        tmp_dir = tempfile.mkdtemp(prefix="agender_clip_")
        out_path = os.path.join(tmp_dir, "clipboard.png")
        script_path = os.path.join(tmp_dir, "grab_clip.py")
        script = (
            "import sys\n"
            "path = sys.argv[1]\n"
            "try:\n"
            "    from PIL import ImageGrab\n"
            "except ImportError:\n"
            "    print('NO_PIL', file=sys.stderr)\n"
            "    sys.exit(3)\n"
            "img = ImageGrab.grabclipboard()\n"
            "if img is None:\n"
            "    print('NO_IMAGE', file=sys.stderr)\n"
            "    sys.exit(2)\n"
            "if isinstance(img, list):\n"
            "    print('NOT_BITMAP', file=sys.stderr)\n"
            "    sys.exit(4)\n"
            "img.save(path, 'PNG')\n"
        )
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        result = None
        for cmd in (["py", "-3"], ["python3"], ["python"]):
            try:
                result = subprocess.run(
                    cmd + [script_path, out_path],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                break
            except FileNotFoundError:
                continue
        if result is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.report({"ERROR"}, "No system Python (py -3 / python3)")
            return {"CANCELLED"}

        err = (result.stderr or "").strip()
        ok = result.returncode == 0 and os.path.isfile(out_path)
        if not ok:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if "NO_PIL" in err:
                self.report({"ERROR"}, "Install Pillow: py -3 -m pip install pillow")
            elif "NO_IMAGE" in err:
                self.report({"WARNING"}, "Clipboard has no image (copy image or screenshot first)")
            else:
                self.report({"WARNING"}, f"Clipboard grab failed: {err[:160]}")
            return {"CANCELLED"}

        clip_dir = os.path.join(os.path.expanduser("~"), ".agender")
        os.makedirs(clip_dir, exist_ok=True)
        persist = os.path.join(clip_dir, "clipboard_last.png")
        try:
            shutil.copy2(out_path, persist)
        except OSError:
            persist = out_path
        shutil.rmtree(tmp_dir, ignore_errors=True)

        context.scene.agender.image_path = persist
        self.report({"INFO"}, "Image pasted from clipboard")
        return {"FINISHED"}


class AGENDER_OT_clear_image(bpy.types.Operator):
    bl_idname = "agender.clear_image"
    bl_label = "Clear Image"

    def execute(self, context):
        context.scene.agender.image_path = ""
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


def _area_window_region(area):
    for reg in area.regions:
        if reg.type == "WINDOW":
            return reg
    return area.regions[0] if area.regions else None


class AGENDER_OT_dock_left(bpy.types.Operator):
    """Split the widest 3D viewport once; open Agender in the left column."""
    bl_idname = "agender.dock_left"
    bl_label = "Dock Left"
    bl_description = (
        "Split the main 3D viewport once to add a left column for Agender "
        "(only works when there is a single 3D View area)"
    )

    def execute(self, context):
        view3ds = [a for a in context.screen.areas if a.type == "VIEW_3D"]
        if not view3ds:
            self.report({"WARNING"}, "No 3D Viewport found")
            return {"CANCELLED"}

        # Already split / multiple 3D areas: never split again from a thin strip
        if len(view3ds) > 1:
            self.report(
                {"INFO"},
                "Multiple 3D View areas already — drag borders to resize, "
                "or join areas (drag one onto another) then click Dock Left once",
            )
            return {"CANCELLED"}

        target = view3ds[0]
        min_w = 480
        if target.width < min_w:
            self.report(
                {"WARNING"},
                f"3D View too narrow ({target.width}px) — widen it before docking",
            )
            return {"CANCELLED"}

        win_reg = _area_window_region(target)
        if not win_reg:
            self.report({"WARNING"}, "Could not find window region for split")
            return {"CANCELLED"}

        n = len(list(context.screen.areas))
        try:
            with bpy.context.temp_override(area=target, region=win_reg):
                bpy.ops.screen.area_split(direction="VERTICAL", factor=0.28)
        except Exception as e:
            self.report({"WARNING"}, f"Split failed: {e}")
            return {"CANCELLED"}

        if len(list(context.screen.areas)) <= n:
            self.report({"WARNING"}, "Area split did not produce a new area")
            return {"CANCELLED"}

        views = sorted(
            [a for a in context.screen.areas if a.type == "VIEW_3D"],
            key=lambda a: a.x,
        )
        if len(views) >= 2:
            chat = views[0]
            for sp in chat.spaces:
                if hasattr(sp, "show_region_ui"):
                    sp.show_region_ui = True
                if hasattr(sp, "overlay"):
                    sp.overlay.show_overlays = False
                if hasattr(sp, "show_gizmo"):
                    sp.show_gizmo = False
                if hasattr(sp, "show_region_toolbar"):
                    sp.show_region_toolbar = False
                if hasattr(sp, "show_region_header"):
                    sp.show_region_header = False

        self.report({"INFO"}, "Docked once — open the Agender tab (N) in the left column")
        return {"FINISHED"}


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Panel draw mixins                                                      ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class _HistoryMixin:
    """Session history list — appears above the chat panel."""
    bl_label = "History"
    bl_region_type = "UI"
    bl_category = "Agender"
    bl_order = 0
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.operator("agender.new_chat", text="", icon="FILE_NEW")

    def draw(self, context):
        layout = self.layout
        props = context.scene.agender
        sessions = _get_sessions()

        if not sessions:
            layout.label(text="No past chats yet.", icon="INFO")
            return

        cur_group = None
        shown = 0
        for s in sessions:
            if shown >= 50:
                layout.label(text="…", icon="THREE_DOTS")
                break

            grp = _date_group(s["timestamp"])
            if grp != cur_group:
                cur_group = grp
                row = layout.row()
                row.scale_y = 0.7
                row.label(text=grp)

            row = layout.row(align=True)
            active = s["uid"] == props.active_session_uid
            icon = "RADIOBUT_ON" if active else "DOT"
            op = row.operator(
                "agender.load_session",
                text=s["title"][:32],
                icon=icon,
            )
            op.session_uid = s["uid"]

            sub = row.row(align=True)
            sub.scale_x = 0.25
            del_op = sub.operator("agender.delete_session", text="", icon="PANEL_CLOSE")
            del_op.session_uid = s["uid"]
            shown += 1


class _ChatMixin:
    """Main chat panel — messages, input, model selector."""
    bl_label = "Agender"
    bl_region_type = "UI"
    bl_category = "Agender"
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        props = context.scene.agender

        # ── Welcome ──────────────────────────────────────────────────
        if len(props.messages) == 0 and not props.is_thinking:
            box = layout.box()
            col = box.column(align=True)
            col.scale_y = 0.85
            col.label(text="Hi, I'm Agender.", icon="LIGHT")
            col.label(text="Your AI assistant for Blender.")
            col.separator()
            col.label(text="Try:", icon="QUESTION")
            col.label(text='  "Add a red sphere at (2,0,1)"')
            col.label(text='  "Animate the cube falling"')
            col.label(text='  "Set up three-point lighting"')

        # ── Messages ─────────────────────────────────────────────────
        max_shown = 50
        start = max(0, len(props.messages) - max_shown)
        for i in range(start, len(props.messages)):
            m = props.messages[i]
            if m.role == "user":
                box = layout.box()
                row = box.row(align=True)
                row.alignment = "LEFT"
                ic = row.column()
                ic.scale_x = 0.3
                ic.label(text="", icon="USER")
                tc = row.column()
                tc.scale_y = 0.85
                for ln in _wrap(m.content, 36):
                    tc.label(text=ln)
            else:
                box = layout.box()
                col = box.column(align=True)
                col.scale_y = 0.85
                for j, line in enumerate(m.content.split("\n")):
                    if not line:
                        continue
                    icon = "NONE"
                    if line.startswith("\u2713"):
                        icon = "CHECKMARK"
                        line = line[1:].strip()
                    elif line.startswith("\u2717"):
                        icon = "ERROR"
                        line = line[1:].strip()
                    elif j == 0:
                        icon = "LIGHT"
                    for w in _wrap(line, 38):
                        col.label(text=w, icon=icon)
                        icon = "NONE"

        # ── Thinking ─────────────────────────────────────────────────
        if props.is_thinking:
            box = layout.box()
            row = box.row()
            row.alignment = "CENTER"
            row.label(text="Thinking…", icon="SORTTIME")

        layout.separator()

        # ── Reference image (vision / multimodal) ───────────────────
        img_box = layout.box()
        img_box.label(text="Image (optional)", icon="IMAGE_DATA")
        img_box.prop(props, "image_path", text="")
        irow = img_box.row(align=True)
        irow.operator("agender.browse_image", text="Browse", icon="FILEBROWSER")
        irow.operator("agender.paste_image_path", text="Paste path", icon="PASTEDOWN")
        irow.operator("agender.paste_image_clipboard", text="Paste image", icon="TEXTURE")
        irow.operator("agender.clear_image", text="", icon="X")
        img_box.label(
            text="Paste image needs system Python + Pillow",
            icon="INFO",
        )

        # ── Input ────────────────────────────────────────────────────
        layout.prop(props, "prompt", text="", icon="OUTLINER_DATA_GP_LAYER")

        # ── Send button (full-width, separate row) ───────────────────
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.enabled = not props.is_thinking
        row.operator("agender.send", text="Send", icon="PLAY")

        # ── Model selector ───────────────────────────────────────────
        row = layout.row(align=True)
        row.prop(props, "model_preset", text="")

        # ── Toolbar ──────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 0.85
        row.operator("agender.scene_info", text="Scene", icon="OUTLINER")
        row.operator("agender.new_chat", text="New", icon="FILE_NEW")
        row.operator("agender.clear", text="", icon="TRASH")
        row.operator("agender.dock_left", text="", icon="WINDOW")


class _SettingsMixin:
    """API key, base URL, and custom model override."""
    bl_label = "Settings"
    bl_region_type = "UI"
    bl_category = "Agender"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        props = context.scene.agender
        layout.prop(props, "api_base")
        layout.prop(props, "api_key")
        layout.separator()
        layout.label(text="Model Override (leave empty to use dropdown):")
        layout.prop(props, "custom_model", text="")
        if props.custom_model and props.custom_model.strip():
            layout.label(text=f"Active: {props.custom_model.strip()}", icon="CHECKMARK")
        else:
            layout.label(text="Using dropdown selection", icon="INFO")


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Concrete panels — VIEW_3D                                              ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class AGENDER_PT_history(bpy.types.Panel, _HistoryMixin):
    bl_idname = "AGENDER_PT_history"
    bl_space_type = "VIEW_3D"


class AGENDER_PT_chat(bpy.types.Panel, _ChatMixin):
    bl_idname = "AGENDER_PT_chat"
    bl_space_type = "VIEW_3D"


class AGENDER_PT_settings(bpy.types.Panel, _SettingsMixin):
    bl_idname = "AGENDER_PT_settings"
    bl_space_type = "VIEW_3D"
    bl_parent_id = "AGENDER_PT_chat"


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Concrete panels — TEXT_EDITOR  (for left-side docking)                 ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class AGENDER_PT_history_te(bpy.types.Panel, _HistoryMixin):
    bl_idname = "AGENDER_PT_history_te"
    bl_space_type = "TEXT_EDITOR"


class AGENDER_PT_chat_te(bpy.types.Panel, _ChatMixin):
    bl_idname = "AGENDER_PT_chat_te"
    bl_space_type = "TEXT_EDITOR"


class AGENDER_PT_settings_te(bpy.types.Panel, _SettingsMixin):
    bl_idname = "AGENDER_PT_settings_te"
    bl_space_type = "TEXT_EDITOR"
    bl_parent_id = "AGENDER_PT_chat_te"


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Registration                                                           ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

_classes = (
    AgenderMessage,
    AgenderProperties,
    # Operators
    AGENDER_OT_send,
    AGENDER_OT_new_chat,
    AGENDER_OT_load_session,
    AGENDER_OT_delete_session,
    AGENDER_OT_scene_info,
    AGENDER_OT_clear,
    AGENDER_OT_browse_image,
    AGENDER_OT_paste_image_path,
    AGENDER_OT_paste_image_clipboard,
    AGENDER_OT_clear_image,
    AGENDER_OT_dock_left,
    # Panels — VIEW_3D
    AGENDER_PT_history,
    AGENDER_PT_chat,
    AGENDER_PT_settings,
    # Panels — TEXT_EDITOR
    AGENDER_PT_history_te,
    AGENDER_PT_chat_te,
    AGENDER_PT_settings_te,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.agender = bpy.props.PointerProperty(type=AgenderProperties)


def unregister():
    del bpy.types.Scene.agender
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
