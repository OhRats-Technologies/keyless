"""Synthetic 3D gaze lab — fake person, fake eyes, real candidate pipeline.

Run headless inside Blender:

    flatpak run org.blender.Blender --background --python \
        experiments/synth_lab/build_scene.py -- --outdir <dir>

The scene is a person (sphere head, two eyeballs with pupils) sitting in
front of a monitor whose UI we generate.  Per frame both eyes converge on one
button; the button rectangles are recorded in screen-pixel coordinates, so
every frame carries exact ground truth (target id + gaze pixel).  Two cameras
render each frame:

- ``screen_cam``  orthographic, fronto-parallel to the monitor: the "screenshot"
  that the real CandidateGenerator (OCR) is run on.
- ``webcam_cam``  perspective, on top of the monitor, at the face: the future
  gaze-model input.

``ground_truth.json`` records the button layout and per-frame targets.  No
legacy gaze code is used anywhere.
"""

from __future__ import annotations

import json
import math
import os
import sys

import bpy
import mathutils

# --- screen frame M0: pixels map linearly onto the monitor plane -----------
SCREEN_W_PX = 1280
SCREEN_H_PX = 720
MONITOR_W = 0.53  # metres
MONITOR_H = MONITOR_W * SCREEN_H_PX / SCREEN_W_PX
MONITOR_CENTER = mathutils.Vector((0.0, 0.0, 1.30))

BUTTONS = [
    # id, label, pixel centre (x, y), pixel size (w, h)
    ("btn-file", "File", 80, 34, 90, 44),
    ("btn-edit", "Edit", 180, 34, 90, 44),
    ("btn-view", "View", 280, 34, 90, 44),
    ("btn-run", "Run", 380, 34, 90, 44),
    ("btn-docs", "Docs", 480, 34, 90, 44),
    ("btn-logs", "Logs", 580, 34, 90, 44),
    ("btn-submit", "Submit", 420, 560, 260, 110),
    ("btn-cancel", "Cancel", 780, 560, 260, 110),
]

TARGETS = ["btn-file", "btn-submit", "btn-view", "btn-cancel", "btn-run", "btn-logs", "btn-submit", "btn-file"]

HEAD_CENTER = mathutils.Vector((0.0, -0.62, 1.32))


def px_to_world(x_px: float, y_px: float, y_depth: float = -0.002) -> mathutils.Vector:
    """Screen-pixel (top-left origin) to world on the monitor plane (-Y faces the user)."""
    u = (x_px / SCREEN_W_PX - 0.5) * MONITOR_W
    v = (0.5 - y_px / SCREEN_H_PX) * MONITOR_H
    return MONITOR_CENTER + mathutils.Vector((u, y_depth, v))


def clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def emission_material(name: str, color: tuple, strength: float = 1.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (*color, 1.0)
    emit.inputs["Strength"].default_value = strength
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


def diffuse_material(name: str, color: tuple):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    principled = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    principled.inputs["Base Color"].default_value = (*color, 1.0)
    return mat


def add_monitor() -> None:
    bpy.ops.mesh.primitive_plane_add(size=1.0)
    plane = bpy.context.active_object
    plane.name = "monitor_screen"
    plane.scale = (MONITOR_W, MONITOR_H, 1.0)
    plane.rotation_euler = (math.radians(90), 0.0, 0.0)
    plane.location = MONITOR_CENTER
    plane.data.materials.append(emission_material("screen_bg", (0.92, 0.92, 0.95), 1.0))

    # bezel (behind the panel, +Y side, so the -Y screen camera sees the panel itself)
    bpy.ops.mesh.primitive_cube_add()
    bezel = bpy.context.active_object
    bezel.name = "monitor_bezel"
    bezel.scale = (MONITOR_W / 2 + 0.015, 0.012, MONITOR_H / 2 + 0.015)
    bezel.location = MONITOR_CENTER + mathutils.Vector((0, 0.012, 0))
    bezel.data.materials.append(diffuse_material("bezel", (0.05, 0.05, 0.06)))

    # stand
    bpy.ops.mesh.primitive_cylinder_add(radius=0.015, depth=0.85)
    stand = bpy.context.active_object
    stand.location = MONITOR_CENTER + mathutils.Vector((0, 0.03, -MONITOR_H / 2 - 0.42))
    stand.data.materials.append(diffuse_material("stand", (0.2, 0.2, 0.22)))


def add_button_ui() -> None:
    for button_id, label, cx, cy, w, h in BUTTONS:
        # a size=1 plane's scale *is* its full extent in metres
        w_m = (w / SCREEN_W_PX) * MONITOR_W
        h_m = (h / SCREEN_H_PX) * MONITOR_H
        bpy.ops.mesh.primitive_plane_add(size=1.0)
        rect = bpy.context.active_object
        rect.name = f"bg_{button_id}"
        rect.scale = (w_m, h_m, 1.0)
        rect.rotation_euler = (math.radians(90), 0.0, 0.0)
        rect.location = px_to_world(cx, cy, y_depth=-0.003)
        rect.data.materials.append(emission_material(f"bgmat_{button_id}", (0.62, 0.68, 0.78), 1.0))

        curve = bpy.data.curves.new(f"text_{button_id}", type="FONT")
        curve.body = label
        curve.align_x = "CENTER"
        curve.align_y = "CENTER"
        curve.size = (h * 0.70 / SCREEN_H_PX) * MONITOR_H
        obj = bpy.data.objects.new(f"text_{button_id}", curve)
        bpy.context.collection.objects.link(obj)
        obj.rotation_euler = (math.radians(90), 0.0, 0.0)
        obj.location = px_to_world(cx, cy, y_depth=-0.004)
        obj.data.materials.append(emission_material(f"textmat_{button_id}", (0.02, 0.02, 0.03), 1.0))


def add_person() -> dict:
    """Fake person: head, eyes with mobile pupils, torso.  Returns parts."""
    head_mat = diffuse_material("skin", (0.62, 0.45, 0.35))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.11, segments=48, ring_count=32)
    head = bpy.context.active_object
    head.name = "head"
    head.scale = (0.92, 1.0, 1.06)
    head.location = HEAD_CENTER
    head.data.materials.append(head_mat)

    bpy.ops.mesh.primitive_cube_add()
    torso = bpy.context.active_object
    torso.name = "torso"
    torso.scale = (0.24, 0.14, 0.32)
    torso.location = HEAD_CENTER + mathutils.Vector((0, 0.02, -0.36))
    torso.data.materials.append(diffuse_material("shirt", (0.25, 0.30, 0.42)))

    eye_mat = diffuse_material("sclera", (0.9, 0.9, 0.9))
    pupil_mat = diffuse_material("pupil", (0.02, 0.02, 0.02))
    eyes = {}
    for side, sign in (("left", -1.0), ("right", 1.0)):
        # face points +Y (toward the monitor at y=0; the person sits at -Y)
        centre = HEAD_CENTER + mathutils.Vector((sign * 0.035, 0.092, 0.012))
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.013, segments=32, ring_count=16)
        ball = bpy.context.active_object
        ball.name = f"eye_{side}"
        ball.location = centre
        ball.data.materials.append(eye_mat)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.0065, segments=32, ring_count=16)
        pupil = bpy.context.active_object
        pupil.name = f"pupil_{side}"
        pupil.scale = (1.0, 1.0, 0.5)
        pupil.data.materials.append(pupil_mat)
        eyes[side] = {"centre": centre, "ball": ball, "pupil": pupil}
    return eyes


def aim_eyes(eyes: dict, target_world: mathutils.Vector, head: bpy.types.Object) -> None:
    """Converge both pupils on the target; nudge head yaw toward it."""
    delta = target_world - HEAD_CENTER
    yaw = math.atan2(delta.x, delta.y)
    head.rotation_euler = (0.0, 0.0, math.radians(math.degrees(yaw) * 0.35))
    for eye in eyes.values():
        direction = (target_world - eye["centre"]).normalized()
        eye["pupil"].location = eye["centre"] + direction * 0.0115
        eye["pupil"].rotation_euler = direction.to_track_quat("Z", "Y").to_euler()


def add_cameras() -> tuple:
    # screenshot camera: orthographic, fronto-parallel to the monitor
    screen_data = bpy.data.cameras.new("screen_cam")
    screen_data.type = "ORTHO"
    screen_data.ortho_scale = MONITOR_W
    screen_cam = bpy.data.objects.new("screen_cam", screen_data)
    bpy.context.collection.objects.link(screen_cam)
    screen_cam.location = MONITOR_CENTER + mathutils.Vector((0, -1.0, 0))
    screen_cam.rotation_euler = (math.radians(90), 0.0, 0.0)

    # webcam camera: on top of the monitor, aimed at the face
    web_data = bpy.data.cameras.new("webcam_cam")
    web_data.lens = 24.0
    web_data.sensor_width = 36.0
    web_cam = bpy.data.objects.new("webcam_cam", web_data)
    bpy.context.collection.objects.link(web_cam)
    web_cam.location = MONITOR_CENTER + mathutils.Vector((0, -0.04, MONITOR_H / 2 + 0.05))
    aim = HEAD_CENTER - web_cam.location
    web_cam.rotation_euler = aim.to_track_quat("-Z", "Y").to_euler()
    return screen_cam, web_cam


def add_lighting() -> None:
    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.35, 0.35, 0.38, 1.0)
    light_data = bpy.data.lights.new("key", type="AREA")
    light_data.energy = 120.0
    light_data.size = 0.6
    light = bpy.data.objects.new("key", light_data)
    bpy.context.collection.objects.link(light)
    light.location = (0.4, -1.2, 1.9)
    aim = HEAD_CENTER - light.location
    light.rotation_euler = aim.to_track_quat("-Z", "Y").to_euler()


def configure_render(outdir: str) -> None:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 12
    scene.cycles.max_bounces = 3
    scene.render.resolution_x = SCREEN_W_PX
    scene.render.resolution_y = SCREEN_H_PX
    scene.render.image_settings.file_format = "PNG"
    os.makedirs(outdir, exist_ok=True)


PERSON_PREFIXES = ("head", "torso", "eye_", "pupil_")


def render_frame(screen_cam, web_cam, index: int, outdir: str) -> None:
    scene = bpy.context.scene
    person_objects = [
        obj
        for obj in bpy.data.objects
        if obj.name.startswith(PERSON_PREFIXES)
    ]
    for camera, tag in ((screen_cam, "screen"), (web_cam, "webcam")):
        # the screenshot is the framebuffer: the person never occludes the panel
        for obj in person_objects:
            obj.hide_render = tag == "screen"
        scene.camera = camera
        scene.render.filepath = os.path.join(outdir, f"{tag}_{index:02d}.png")
        bpy.ops.render.render(write_still=True)


def main() -> None:
    argv = sys.argv
    outdir = os.path.join(os.path.dirname(os.path.abspath(argv[0])), "..", "..", ".tmp-synth-lab")
    if "--" in argv:
        rest = argv[argv.index("--") + 1:]
        if "--outdir" in rest:
            outdir = rest[rest.index("--outdir") + 1]
    outdir = os.path.abspath(outdir)
    blend_path = None
    if "--" in argv:
        rest = argv[argv.index("--") + 1:]
        if "--blend" in rest:
            blend_path = os.path.abspath(rest[rest.index("--blend") + 1])

    clear_scene()
    add_monitor()
    add_button_ui()
    eyes = add_person()
    screen_cam, web_cam = add_cameras()
    add_lighting()
    configure_render(outdir)

    buttons = {b[0]: b for b in BUTTONS}
    frames = []
    for index, target_id in enumerate(TARGETS):
        _, label, cx, cy, w, h = buttons[target_id]
        target_world = px_to_world(cx, cy)
        aim_eyes(eyes, target_world, bpy.data.objects["head"])
        render_frame(screen_cam, web_cam, index, outdir)
        frames.append(
            {
                "index": index,
                "target_id": target_id,
                "gaze_pixel": [float(cx), float(cy)],
                "screen_png": f"screen_{index:02d}.png",
                "webcam_png": f"webcam_{index:02d}.png",
            }
        )

    truth = {
        "width_px": SCREEN_W_PX,
        "height_px": SCREEN_H_PX,
        # bbox is [x, y, w, h] in screen pixels, top-left origin
        "buttons": [
            {"id": b[0], "label": b[1], "bbox": [b[2] - b[4] // 2, b[3] - b[5] // 2, b[4], b[5]]}
            for b in BUTTONS
        ],
        "frames": frames,
    }
    with open(os.path.join(outdir, "ground_truth.json"), "w") as handle:
        json.dump(truth, handle, indent=2)
    print(f"SYNTH_LAB_DONE {outdir} frames={len(frames)}")


main()
