#!/usr/bin/env python3
"""
blender_bridge.py — OpenClaw <-> Blender communication bridge.

Sends commands to the Blender addon's local HTTP server.
Used by the Blender剑魔 agent via exec.

Usage:
  py -3 blender_bridge.py scene-info
  py -3 blender_bridge.py execute --commands '[{"type":"add_primitive","params":{"type":"cube"}}]'
  py -3 blender_bridge.py execute --file commands.json
  py -3 blender_bridge.py asset-list
  py -3 blender_bridge.py asset-list --tag 家具 --query 椅子
  py -3 blender_bridge.py asset-register --filepath "C:\\assets\\chair.glb" --name "木椅" --tags "家具,日式"
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_HOST = "http://127.0.0.1:9876"


def _host():
    return os.environ.get("BLENDER_BRIDGE_HOST", DEFAULT_HOST)


def _request(path, data=None, timeout=300):
    url = f"{_host()}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"ERROR: Cannot connect to Blender at {url}", file=sys.stderr)
        print("Make sure Blender is running and the OpenClaw addon is enabled.", file=sys.stderr)
        print(f"Detail: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_scene_info(_args):
    result = _request("/scene-info")
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_execute(args):
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            commands = json.load(f)
    elif args.commands:
        commands = json.loads(args.commands)
    else:
        print("ERROR: Provide --commands or --file", file=sys.stderr)
        sys.exit(1)

    if isinstance(commands, dict):
        commands = [commands]

    timeout = args.timeout if hasattr(args, "timeout") and args.timeout else 300
    result = _request("/execute", {"commands": commands}, timeout=timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_asset_list(args):
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asset_index.json")
    if not os.path.exists(index_path):
        print("[]")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.tag:
        data = [a for a in data if args.tag in a.get("tags", [])]
    if args.query:
        q = args.query.lower()
        data = [a for a in data if q in a.get("name", "").lower() or q in a.get("id", "").lower()]

    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_asset_register(args):
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asset_index.json")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []

    asset_id = args.id or os.path.splitext(os.path.basename(args.filepath))[0]

    entry = {
        "id": asset_id,
        "name": args.name or asset_id,
        "tags": [t.strip() for t in args.tags.split(",")] if args.tags else [],
        "filepath": os.path.abspath(args.filepath),
        "source": args.source or "unknown",
    }

    existing_idx = next((i for i, a in enumerate(data) if a["id"] == asset_id), None)
    if existing_idx is not None:
        data[existing_idx] = entry
    else:
        data.append(entry)

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(json.dumps(entry, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="OpenClaw Blender Bridge")
    parser.add_argument("--host", default=None, help="Override Blender HTTP server URL")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scene-info", help="Get current scene information")

    exe = sub.add_parser("execute", help="Execute commands in Blender")
    exe.add_argument("--commands", help="JSON array of commands")
    exe.add_argument("--file", help="Path to JSON file containing commands")
    exe.add_argument("--timeout", type=int, default=300, help="Request timeout in seconds")

    als = sub.add_parser("asset-list", help="List registered assets")
    als.add_argument("--tag", help="Filter by tag")
    als.add_argument("--query", help="Search by name or id")

    reg = sub.add_parser("asset-register", help="Register a new asset")
    reg.add_argument("--filepath", required=True, help="Path to 3D file")
    reg.add_argument("--name", help="Display name")
    reg.add_argument("--id", help="Asset ID (default: filename without extension)")
    reg.add_argument("--tags", help="Comma-separated tags")
    reg.add_argument("--source", help="Source: neural4d, manual, meshy, tripo, etc.")

    args = parser.parse_args()

    if args.host:
        os.environ["BLENDER_BRIDGE_HOST"] = args.host

    handlers = {
        "scene-info": cmd_scene_info,
        "execute": cmd_execute,
        "asset-list": cmd_asset_list,
        "asset-register": cmd_asset_register,
    }

    handler = handlers.get(args.command)
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
