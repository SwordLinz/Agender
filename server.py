"""
HTTP server running inside Blender for external control.

Listens on 127.0.0.1:9876. Commands arrive via HTTP and are queued
for execution on the main thread (bpy requires main-thread access).
"""

import bpy
import json
import threading
import queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from . import executor

_server = None
_server_thread = None
_command_queue = queue.Queue()
_result_store = {}
_timer_registered = False

PORT = 9876


class _BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True, "addon": "agender", "port": PORT})
            return

        if self.path == "/scene-info":
            result = self._queue_and_wait("scene_info", {})
            self._send_json(result)
            return

        self._send_json({"error": "Not found", "path": self.path}, 404)

    def do_POST(self):
        if self.path == "/execute":
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self._send_json({"ok": False, "error": "Empty body"}, 400)
                return

            body = json.loads(self.rfile.read(length).decode("utf-8"))
            commands = body.get("commands", [])
            if not commands:
                self._send_json({"ok": False, "error": "No commands provided"}, 400)
                return

            result = self._queue_and_wait("execute", commands, timeout=300)
            self._send_json(result)
            return

        self._send_json({"error": "Not found", "path": self.path}, 404)

    def _queue_and_wait(self, cmd_type, data, timeout=30):
        event = threading.Event()
        result_id = id(event)
        _command_queue.put((cmd_type, data, result_id, event))
        event.wait(timeout=timeout)
        return _result_store.pop(result_id, {"ok": False, "error": "Timeout"})


def _process_queue():
    """Timer callback — runs on Blender's main thread."""
    try:
        while not _command_queue.empty():
            cmd_type, data, result_id, event = _command_queue.get_nowait()
            try:
                if cmd_type == "scene_info":
                    result = executor._execute_one({"type": "scene_info", "params": {}})
                elif cmd_type == "execute":
                    result = {"ok": True, "results": executor.execute_commands(data)}
                else:
                    result = {"ok": False, "error": f"Unknown queue command: {cmd_type}"}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            _result_store[result_id] = result
            event.set()
    except queue.Empty:
        pass
    return 0.1


def start():
    global _server, _server_thread, _timer_registered
    if _server is not None:
        return

    try:
        _server = HTTPServer(("127.0.0.1", PORT), _BridgeHandler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()

        if not _timer_registered:
            bpy.app.timers.register(_process_queue, persistent=True)
            _timer_registered = True

        print(f"[Agender] Bridge server started on http://127.0.0.1:{PORT}")
    except OSError as e:
        print(f"[Agender] Failed to start bridge server on port {PORT}: {e}")


def stop():
    global _server, _server_thread, _timer_registered
    if _server:
        _server.shutdown()
        _server = None
        _server_thread = None

    if _timer_registered:
        try:
            bpy.app.timers.unregister(_process_queue)
        except ValueError:
            pass
        _timer_registered = False

    print("[Agender] Bridge server stopped")
