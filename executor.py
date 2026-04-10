"""
Command executor — maps JSON commands to bpy operations.

All commands are white-listed. No arbitrary code execution.
Rotation values are in degrees; converted to radians internally.
Color values are 0.0–1.0 per channel. Location units are meters.
"""

import bpy
import os
import math
from mathutils import Vector, Euler


def execute_commands(commands):
    """Execute a list of command dicts. Returns list of result dicts."""
    bpy.ops.ed.undo_push(message="Agender")
    results = []
    for cmd in commands:
        results.append(_execute_one(cmd))
    return results


def _execute_one(cmd):
    cmd_type = cmd.get("type")
    params = cmd.get("params", {})
    handler = _HANDLERS.get(cmd_type)
    if not handler:
        return {
            "ok": False,
            "type": cmd_type,
            "error": f"Unknown command: {cmd_type}",
            "available": list(_HANDLERS.keys()),
        }
    try:
        result = handler(params)
        return {"ok": True, "type": cmd_type, **result}
    except Exception as e:
        return {"ok": False, "type": cmd_type, "error": str(e)}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _vec(v, default=(0, 0, 0)):
    if v is None:
        return Vector(default)
    return Vector(v)


def _euler_deg(v):
    if v is None:
        return None
    return Euler([math.radians(x) for x in v])


def _apply_scale(obj, s):
    if s is None:
        return
    if isinstance(s, (int, float)):
        obj.scale = Vector((s, s, s))
    else:
        obj.scale = _vec(s, (1, 1, 1))


def _find_obj(name):
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object not found: '{name}'")
    return obj


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _scene_info(_p):
    scene = bpy.context.scene
    objects = []
    for obj in scene.objects:
        entry = {
            "name": obj.name,
            "type": obj.type,
            "location": [round(x, 4) for x in obj.location],
            "rotation_deg": [round(math.degrees(x), 2) for x in obj.rotation_euler],
            "scale": [round(x, 4) for x in obj.scale],
            "visible": obj.visible_get(),
        }
        if obj.parent:
            entry["parent"] = obj.parent.name
        if obj.active_material:
            entry["material"] = obj.active_material.name
        objects.append(entry)

    collections = []
    def _walk(col, depth=0):
        collections.append({
            "name": col.name,
            "depth": depth,
            "objects": len(col.objects),
        })
        for child in col.children:
            _walk(child, depth + 1)
    _walk(scene.collection)

    return {
        "scene": scene.name,
        "frame": scene.frame_current,
        "frame_range": [scene.frame_start, scene.frame_end],
        "render_engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "object_count": len(objects),
        "objects": objects,
        "collections": collections,
    }


def _import_asset(p):
    filepath = p["filepath"]
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    ext = os.path.splitext(filepath)[1].lower()
    importers = {
        ".glb":  lambda: bpy.ops.import_scene.gltf(filepath=filepath),
        ".gltf": lambda: bpy.ops.import_scene.gltf(filepath=filepath),
        ".fbx":  lambda: bpy.ops.import_scene.fbx(filepath=filepath),
        ".obj":  lambda: bpy.ops.wm.obj_import(filepath=filepath),
        ".stl":  lambda: bpy.ops.wm.stl_import(filepath=filepath),
        ".usd":  lambda: bpy.ops.wm.usd_import(filepath=filepath),
        ".usda": lambda: bpy.ops.wm.usd_import(filepath=filepath),
        ".usdc": lambda: bpy.ops.wm.usd_import(filepath=filepath),
        ".usdz": lambda: bpy.ops.wm.usd_import(filepath=filepath),
    }
    importer = importers.get(ext)
    if not importer:
        raise ValueError(f"Unsupported format: {ext}")

    importer()
    imported = list(bpy.context.selected_objects)

    if p.get("name") and len(imported) == 1:
        imported[0].name = p["name"]

    for obj in imported:
        if p.get("location"):
            obj.location = _vec(p["location"])
        rot = _euler_deg(p.get("rotation"))
        if rot:
            obj.rotation_euler = rot
        _apply_scale(obj, p.get("scale"))

    return {"imported": [o.name for o in imported]}


def _add_primitive(p):
    ptype = p.get("type", "cube").lower()
    loc = tuple(p.get("location", (0, 0, 0)))
    size = p.get("size", 1.0)

    ops_map = {
        "cube":       lambda: bpy.ops.mesh.primitive_cube_add(size=size, location=loc),
        "sphere":     lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=size / 2, location=loc),
        "uv_sphere":  lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=size / 2, location=loc),
        "ico_sphere": lambda: bpy.ops.mesh.primitive_ico_sphere_add(radius=size / 2, location=loc),
        "cylinder":   lambda: bpy.ops.mesh.primitive_cylinder_add(radius=size / 2, depth=size, location=loc),
        "cone":       lambda: bpy.ops.mesh.primitive_cone_add(radius1=size / 2, depth=size, location=loc),
        "plane":      lambda: bpy.ops.mesh.primitive_plane_add(size=size, location=loc),
        "torus":      lambda: bpy.ops.mesh.primitive_torus_add(location=loc, major_radius=size / 2, minor_radius=size / 6),
        "monkey":     lambda: bpy.ops.mesh.primitive_monkey_add(size=size, location=loc),
    }
    op = ops_map.get(ptype)
    if not op:
        raise ValueError(f"Unknown primitive: {ptype}. Options: {list(ops_map.keys())}")

    op()
    obj = bpy.context.active_object
    if p.get("name"):
        obj.name = p["name"]
    rot = _euler_deg(p.get("rotation"))
    if rot:
        obj.rotation_euler = rot
    _apply_scale(obj, p.get("scale"))

    return {"object": obj.name}


def _set_transform(p):
    obj = _find_obj(p["object"])
    if p.get("location"):
        obj.location = _vec(p["location"])
    rot = _euler_deg(p.get("rotation"))
    if rot:
        obj.rotation_euler = rot
    _apply_scale(obj, p.get("scale"))
    return {"object": obj.name}


def _add_light(p):
    light_type = p.get("type", "POINT").upper()
    valid_types = {"POINT", "SUN", "SPOT", "AREA"}
    if light_type not in valid_types:
        raise ValueError(f"Invalid light type: {light_type}. Options: {valid_types}")

    loc = tuple(p.get("location", (0, 0, 3)))
    bpy.ops.object.light_add(type=light_type, location=loc)
    obj = bpy.context.active_object
    light = obj.data

    if p.get("name"):
        obj.name = p["name"]
        light.name = p["name"]
    if p.get("energy") is not None:
        light.energy = p["energy"]
    if p.get("color"):
        light.color = tuple(p["color"][:3])
    rot = _euler_deg(p.get("rotation"))
    if rot:
        obj.rotation_euler = rot
    if p.get("size") is not None:
        if light_type == "AREA":
            light.size = p["size"]
        elif light_type in ("POINT", "SPOT"):
            light.shadow_soft_size = p["size"]
    if p.get("spot_size") is not None and light_type == "SPOT":
        light.spot_size = math.radians(p["spot_size"])

    return {"object": obj.name, "light": light.name}


def _add_camera(p):
    loc = tuple(p.get("location", (0, -5, 2)))
    bpy.ops.object.camera_add(location=loc)
    obj = bpy.context.active_object
    cam = obj.data

    if p.get("name"):
        obj.name = p["name"]
        cam.name = p["name"]
    rot = _euler_deg(p.get("rotation"))
    if rot:
        obj.rotation_euler = rot
    if p.get("lens") is not None:
        cam.lens = p["lens"]
    if p.get("sensor_width") is not None:
        cam.sensor_width = p["sensor_width"]
    if p.get("clip_start") is not None:
        cam.clip_start = p["clip_start"]
    if p.get("clip_end") is not None:
        cam.clip_end = p["clip_end"]
    if p.get("dof_object"):
        dof_target = bpy.data.objects.get(p["dof_object"])
        if dof_target:
            cam.dof.use_dof = True
            cam.dof.focus_object = dof_target
    if p.get("dof_distance") is not None:
        cam.dof.use_dof = True
        cam.dof.focus_distance = p["dof_distance"]
    if p.get("set_active", True):
        bpy.context.scene.camera = obj

    return {"object": obj.name, "camera": cam.name}


def _look_at(p):
    obj = _find_obj(p["object"])
    if p.get("target_object"):
        target = _find_obj(p["target_object"])
        target_loc = target.location.copy()
    elif p.get("target"):
        target_loc = _vec(p["target"])
    else:
        raise ValueError("Need 'target' [x,y,z] or 'target_object' name")

    direction = target_loc - obj.location
    rot_quat = direction.to_track_quat('-Z', 'Y')
    obj.rotation_euler = rot_quat.to_euler()
    return {"object": obj.name, "looking_at": [round(x, 4) for x in target_loc]}


def _set_material(p):
    obj = _find_obj(p["object"])
    mat_name = p.get("name", f"{obj.name}_material")

    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True

    bsdf = None
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            bsdf = node
            break

    if bsdf:
        if p.get("color"):
            c = p["color"]
            bsdf.inputs["Base Color"].default_value = (c[0], c[1], c[2], 1.0)
        if p.get("metallic") is not None:
            bsdf.inputs["Metallic"].default_value = p["metallic"]
        if p.get("roughness") is not None:
            bsdf.inputs["Roughness"].default_value = p["roughness"]
        if p.get("emission_color"):
            ec = p["emission_color"]
            bsdf.inputs["Emission Color"].default_value = (ec[0], ec[1], ec[2], 1.0)
        if p.get("emission_strength") is not None:
            bsdf.inputs["Emission Strength"].default_value = p["emission_strength"]
        if p.get("alpha") is not None:
            bsdf.inputs["Alpha"].default_value = p["alpha"]

    if obj.data and hasattr(obj.data, "materials"):
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)

    return {"object": obj.name, "material": mat.name}


def _delete_object(p):
    obj = _find_obj(p["object"])
    name = obj.name
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def _duplicate_object(p):
    obj = _find_obj(p["object"])
    new_obj = obj.copy()
    if obj.data and not p.get("linked", False):
        new_obj.data = obj.data.copy()
    bpy.context.collection.objects.link(new_obj)

    if p.get("name"):
        new_obj.name = p["name"]
    if p.get("location"):
        new_obj.location = _vec(p["location"])
    elif p.get("offset"):
        new_obj.location = obj.location + _vec(p["offset"])

    return {"object": new_obj.name, "source": obj.name}


def _set_parent(p):
    child = _find_obj(p["child"])
    parent = _find_obj(p["parent"])
    child.parent = parent
    if p.get("keep_transform", True):
        child.matrix_parent_inverse = parent.matrix_world.inverted()
    return {"child": child.name, "parent": parent.name}


def _collection_new(p):
    name = p["name"]
    col = bpy.data.collections.new(name)
    parent_name = p.get("parent")
    if parent_name:
        parent_col = bpy.data.collections.get(parent_name)
        if parent_col:
            parent_col.children.link(col)
        else:
            bpy.context.scene.collection.children.link(col)
    else:
        bpy.context.scene.collection.children.link(col)
    return {"collection": col.name}


def _move_to_collection(p):
    obj = _find_obj(p["object"])
    col = bpy.data.collections.get(p["collection"])
    if not col:
        raise ValueError(f"Collection not found: '{p['collection']}'")
    for c in obj.users_collection:
        c.objects.unlink(obj)
    col.objects.link(obj)
    return {"object": obj.name, "collection": col.name}


def _set_render(p):
    scene = bpy.context.scene
    if p.get("engine"):
        engine_map = {
            "eevee": "BLENDER_EEVEE_NEXT",
            "eevee_next": "BLENDER_EEVEE_NEXT",
            "cycles": "CYCLES",
            "workbench": "BLENDER_WORKBENCH",
        }
        scene.render.engine = engine_map.get(p["engine"].lower(), p["engine"])
    if p.get("resolution_x") is not None:
        scene.render.resolution_x = p["resolution_x"]
    if p.get("resolution_y") is not None:
        scene.render.resolution_y = p["resolution_y"]
    if p.get("samples") is not None:
        if scene.render.engine == "CYCLES":
            scene.cycles.samples = p["samples"]
        else:
            scene.eevee.taa_render_samples = p["samples"]
    if p.get("output_path"):
        scene.render.filepath = p["output_path"]
    if p.get("format"):
        fmt_map = {
            "png": "PNG", "jpeg": "JPEG", "jpg": "JPEG",
            "exr": "OPEN_EXR", "tiff": "TIFF", "bmp": "BMP",
        }
        scene.render.image_settings.file_format = fmt_map.get(
            p["format"].lower(), p["format"].upper()
        )
    return {
        "engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
    }


def _render(p):
    scene = bpy.context.scene
    if p.get("output_path"):
        scene.render.filepath = p["output_path"]
    animation = p.get("animation", False)
    if animation:
        bpy.ops.render.render(animation=True)
    else:
        bpy.ops.render.render(write_still=True)
    return {"output": scene.render.filepath, "animation": animation}


# ---------------------------------------------------------------------------
# Animation & Physics
# ---------------------------------------------------------------------------

def _set_frame_range(p):
    scene = bpy.context.scene
    if p.get("start") is not None:
        scene.frame_start = p["start"]
    if p.get("end") is not None:
        scene.frame_end = p["end"]
    if p.get("fps") is not None:
        scene.render.fps = p["fps"]
    if p.get("current") is not None:
        scene.frame_set(p["current"])
    return {
        "frame_range": [scene.frame_start, scene.frame_end],
        "fps": scene.render.fps,
        "current": scene.frame_current,
    }


def _set_keyframe(p):
    obj = _find_obj(p["object"])
    frame = p.get("frame", bpy.context.scene.frame_current)
    bpy.context.scene.frame_set(frame)

    if p.get("location") is not None:
        obj.location = _vec(p["location"])
        obj.keyframe_insert(data_path="location", frame=frame)
    if p.get("rotation") is not None:
        obj.rotation_euler = _euler_deg(p["rotation"])
        obj.keyframe_insert(data_path="rotation_euler", frame=frame)
    if p.get("scale") is not None:
        _apply_scale(obj, p["scale"])
        obj.keyframe_insert(data_path="scale", frame=frame)

    if p.get("interpolation"):
        interp = p["interpolation"].upper()
        if obj.animation_data and obj.animation_data.action:
            for fc in obj.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    if abs(kp.co[0] - frame) < 0.5:
                        kp.interpolation = interp

    return {"object": obj.name, "frame": frame}


def _keyframe_sequence(p):
    """Insert multiple keyframes at once for convenience."""
    obj = _find_obj(p["object"])
    keyframes = p.get("keyframes", [])
    if not keyframes:
        raise ValueError("'keyframes' list is required")

    inserted = []
    for kf in keyframes:
        frame = kf["frame"]
        bpy.context.scene.frame_set(frame)

        if kf.get("location") is not None:
            obj.location = _vec(kf["location"])
            obj.keyframe_insert(data_path="location", frame=frame)
        if kf.get("rotation") is not None:
            obj.rotation_euler = _euler_deg(kf["rotation"])
            obj.keyframe_insert(data_path="rotation_euler", frame=frame)
        if kf.get("scale") is not None:
            _apply_scale(obj, kf["scale"])
            obj.keyframe_insert(data_path="scale", frame=frame)
        inserted.append(frame)

    interp = p.get("interpolation")
    if interp and obj.animation_data and obj.animation_data.action:
        interp_upper = interp.upper()
        for fc in obj.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = interp_upper

    return {"object": obj.name, "frames": inserted}


def _clear_keyframes(p):
    obj = _find_obj(p["object"])
    obj.animation_data_clear()
    return {"object": obj.name}


def _add_rigid_body(p):
    obj = _find_obj(p["object"])
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    rb_type = p.get("type", "ACTIVE").upper()

    bpy.ops.rigidbody.object_add(type=rb_type)
    rb = obj.rigid_body

    if p.get("mass") is not None:
        rb.mass = p["mass"]
    if p.get("friction") is not None:
        rb.friction = p["friction"]
    if p.get("restitution") is not None:
        rb.restitution = p["restitution"]
    if p.get("collision_shape"):
        rb.collision_shape = p["collision_shape"].upper()
    if p.get("kinematic") is not None:
        rb.kinematic = p["kinematic"]

    return {"object": obj.name, "rigid_body_type": rb_type}


def _add_modifier(p):
    obj = _find_obj(p["object"])
    mod_type = p["modifier_type"].upper()
    mod_name = p.get("name", mod_type.title())

    mod = obj.modifiers.new(name=mod_name, type=mod_type)

    settings = p.get("settings", {})
    for key, val in settings.items():
        if hasattr(mod, key):
            setattr(mod, key, val)

    return {"object": obj.name, "modifier": mod.name, "type": mod_type}


def _shade_smooth(p):
    obj = _find_obj(p["object"])
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    smooth = p.get("smooth", True)
    if smooth:
        bpy.ops.object.shade_smooth()
    else:
        bpy.ops.object.shade_flat()
    return {"object": obj.name, "smooth": smooth}


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_HANDLERS = {
    "scene_info":          _scene_info,
    "import_asset":        _import_asset,
    "add_primitive":       _add_primitive,
    "set_transform":       _set_transform,
    "add_light":           _add_light,
    "add_camera":          _add_camera,
    "look_at":             _look_at,
    "set_material":        _set_material,
    "delete_object":       _delete_object,
    "duplicate_object":    _duplicate_object,
    "set_parent":          _set_parent,
    "collection_new":      _collection_new,
    "move_to_collection":  _move_to_collection,
    "set_render":          _set_render,
    "render":              _render,
    "set_frame_range":     _set_frame_range,
    "set_keyframe":        _set_keyframe,
    "keyframe_sequence":   _keyframe_sequence,
    "clear_keyframes":     _clear_keyframes,
    "add_rigid_body":      _add_rigid_body,
    "add_modifier":        _add_modifier,
    "shade_smooth":        _shade_smooth,
}
