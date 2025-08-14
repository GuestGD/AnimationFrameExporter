bl_info = {
    "name": "Animation Frame Exporter",
    "author": "Your Name",
    "version": (1, 7),
    "blender": (3, 0, 0),
    "location": "Properties > Object",
    "description": "Exports bone matrices as binary data or armature custom property",
    "warning": "",
    "doc_url": "",
    "category": "Import-Export",
}

import bpy
import os
import numpy as np
from mathutils import Matrix
import math

# Conversion matrix from Blender (Z-up) to Three.js (Y-up)
BLENDER_TO_THREE = Matrix.Rotation(-math.pi/2, 4, 'X')

# --------------------------------------------------------------------
# Property groups
# --------------------------------------------------------------------
class AnimationExportProperties(bpy.types.PropertyGroup):
    animation_name: bpy.props.StringProperty(
        name="Animation Name",
        default="",
        description="Custom name for exported files (leave empty to use action name)"
    )
    use_custom_name: bpy.props.BoolProperty(
        name="Use Custom Name",
        default=False,
        description="Use custom animation name instead of action name"
    )
    start_frame: bpy.props.IntProperty(
        name="Start Frame",
        default=1,
        min=1,
        description="First frame to export (0 for scene start)"
    )
    end_frame: bpy.props.IntProperty(
        name="End Frame",
        default=0,
        min=0,
        description="Last frame to export (0 for scene end)"
    )
    frame_step: bpy.props.IntProperty(
        name="Frame Step",
        default=1,
        min=1,
        description="Export every N frames"
    )
    export_all_frames: bpy.props.BoolProperty(
        name="Export All Frames",
        default=False,
        description="Ignore frame step and export every frame"
    )
    use_full_animation_range: bpy.props.BoolProperty(
        name="Use Full Animation Range",
        default=False,
        description="Export from frame 1 to last frame of current animation with specified step"
    )
    export_method: bpy.props.EnumProperty(
        name="Export Method",
        items=[
            ('BIN', "Binary File", "Export to binary file"),
            ('PROPERTY', "Armature Property", "Store raw matrices in armature custom property"),
        ],
        default='BIN',
        description="Choose how to export the animation data"
    )
    property_name: bpy.props.StringProperty(
        name="Property Name",
        default="animation_matrices",
        description="Name for the custom property to store raw matrix data"
    )
    export_mode: bpy.props.EnumProperty(
        name="Export Mode",
        items=[
            ('SINGLE', "Single Animation", "Export one animation at a time"),
            ('MULTI', "Multiple Animations", "Export multiple animations into one file"),
        ],
        default='SINGLE',
        description="Choose whether to export one animation or multiple animations together"
    )
    unit_name: bpy.props.StringProperty(
        name="Unit Name",
        default="unit",
        description="Base name for the exported animations (e.g., 'soldier' will create 'soldier_animations.bin')"
    )
    multi_export_all_frames: bpy.props.BoolProperty(
        name="Export All Frames For All",
        default=False,
        description="Ignore frame steps and export every frame for all animations"
    )
    # transition_frames is fixed at 10


class AnimationListItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Animation Name")
    action: bpy.props.PointerProperty(type=bpy.types.Action)
    include: bpy.props.BoolProperty(name="Include", default=True)
    frame_step: bpy.props.IntProperty(
        name="Frame Step",
        default=1,
        min=1,
        description="Export every N frames for this animation"
    )
    use_full_range: bpy.props.BoolProperty(
        name="Use Full Range",
        default=True,
        description="Export full animation range for this action"
    )
    custom_start: bpy.props.IntProperty(
        name="Start Frame",
        default=1,
        min=0,
        description="Custom start frame for this animation"
    )
    custom_end: bpy.props.IntProperty(
        name="End Frame",
        default=0,
        min=0,
        description="Custom end frame for this animation (0 for action end)"
    )

# --------------------------------------------------------------------
# UI Lists & Operators
# --------------------------------------------------------------------
class AnimationList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "include", text="")
            row.label(text=item.name)
            if item.include:
                sub = row.row(align=True)
                sub.active = item.include
                sub.prop(item, "frame_step", text="Step")
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text=item.name)


class AnimationList_OT_Refresh(bpy.types.Operator):
    bl_idname = "animation_list.refresh"
    bl_label = "Refresh Animation List"

    def execute(self, context):
        scene = context.scene
        scene.animation_list.clear()
        for action in bpy.data.actions:
            item = scene.animation_list.add()
            item.name = action.name
            item.action = action
            item.frame_step = 1
            item.use_full_range = True
            item.custom_start = 1
            item.custom_end = int(action.frame_range[1])
        return {'FINISHED'}


class AnimationList_OT_EditSettings(bpy.types.Operator):
    bl_idname = "animation_list.edit_settings"
    bl_label = "Edit Animation Settings"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.animation_list_index >= 0 and len(context.scene.animation_list) > 0

    def execute(self, context):
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        item = scene.animation_list[scene.animation_list_index]
        box = layout.box()
        box.label(text=item.name, icon='ACTION')
        row = box.row()
        row.prop(item, "use_full_range", text="Full Range")
        if not item.use_full_range:
            row = box.row()
            row.prop(item, "custom_start")
            row.prop(item, "custom_end")
        box.prop(item, "frame_step")

# --------------------------------------------------------------------
# Core export functions
# --------------------------------------------------------------------
def export_multiple_animations(context, unit_name, export_method='BIN',
                               property_name="animation_matrices", export_all_frames=False):
    try:
        arm = context.active_object
        if not arm or arm.type != 'ARMATURE':
            raise Exception("Please select an armature object first")

        scene = context.scene
        props = scene.animation_export_props          # <- keep this
        transition_frames = 10                        # fixed value
        animations_to_export = [item for item in scene.animation_list if item.include and item.action]

        if not animations_to_export:
            raise Exception("No animations selected for export")

        num_bones = len(arm.pose.bones)
        original_frame = scene.frame_current
        original_action = arm.animation_data.action if arm.animation_data else None

        # ----------------------------------------------------------------
        # Helper: collect matrices for current frame
        # ----------------------------------------------------------------
        def collect_matrices_at_frame(frame):
            scene.frame_set(frame)
            mats = []
            for bone in arm.pose.bones:
                mat = BLENDER_TO_THREE @ arm.matrix_world @ bone.matrix
                for col in range(4):
                    for row in range(4):
                        mats.append(mat[row][col])
            return np.array(mats, dtype=np.float32)

        # ----------------------------------------------------------------
        # 1. Rest pose (single frame)
        # ----------------------------------------------------------------
        rest_frame = 0 if scene.frame_start <= 0 else 1
        all_matrix_data = [collect_matrices_at_frame(rest_frame)]
        frame_counts = [1]
        frame_steps = [1]
        animation_ranges = [(rest_frame, rest_frame)]
        total_frames = 1
        animation_helpers = [{'name': f"{unit_name}Rest", 'start_idx': 0, 'end_idx': 0}]

        # ----------------------------------------------------------------
        # 2. Normal animations
        # ----------------------------------------------------------------
        for item in animations_to_export:
            action = item.action
            if not arm.animation_data:
                arm.animation_data_create()
            arm.animation_data.action = action

            if item.use_full_range:
                start_frame = int(action.frame_range[0])
                end_frame = int(action.frame_range[1])
            else:
                start_frame = item.custom_start
                end_frame = item.custom_end if item.custom_end else int(action.frame_range[1])

            if start_frame > end_frame:
                raise Exception(f"Start frame cannot be after end frame for animation: {action.name}")

            if export_all_frames:
                frames_to_export = list(range(start_frame, end_frame + 1))
                frame_step = 1
            else:
                frame_step = item.frame_step
                frames_to_export = list(range(start_frame, end_frame + 1, frame_step))
                if frames_to_export[-1] != end_frame:
                    frames_to_export.append(end_frame)

            num_frames = len(frames_to_export)
            frame_counts.append(num_frames)
            frame_steps.append(frame_step)
            animation_ranges.append((start_frame, end_frame))

            start_idx = total_frames
            end_idx = total_frames + num_frames - 1
            animation_helpers.append({
                'name': f"{action.name}",
                'start_idx': start_idx,
                'end_idx': end_idx
            })
            total_frames += num_frames

            anim_matrices = np.zeros(num_frames * num_bones * 16, dtype=np.float32)
            for f_idx, f_num in enumerate(frames_to_export):
                scene.frame_set(f_num)
                offset = f_idx * num_bones * 16
                idx = offset
                for bone in arm.pose.bones:
                    mat = BLENDER_TO_THREE @ arm.matrix_world @ bone.matrix
                    for col in range(4):
                        for row in range(4):
                            anim_matrices[idx] = mat[row][col]
                            idx += 1
            all_matrix_data.append(anim_matrices)

        # ----------------------------------------------------------------
        # 3. Real transition actions – exact steps you wrote
        # ----------------------------------------------------------------
        transitions = [(src, dst) for src in animations_to_export
                       for dst in animations_to_export if src != dst]

        def last_frame(item):
            return int(item.action.frame_range[1]) if item.use_full_range else (item.custom_end or int(item.action.frame_range[1]))

        def first_frame(item):
            return int(item.action.frame_range[0]) if item.use_full_range else item.custom_start

        original_action = arm.animation_data.action if arm.animation_data else None

        for src_item, dst_item in transitions:
            src_last = last_frame(src_item)
            dst_first = first_frame(dst_item)
            tr_name = f"{src_item.action.name}_To_{dst_item.action.name}"

            # 1. create the transition action
            tr_action = bpy.data.actions.new(name=tr_name)
            if arm.animation_data is None:
                arm.animation_data_create()

            # ---------- frame 1 : last pose of SOURCE ----------
            arm.animation_data.action = src_item.action
            scene.frame_set(src_last)
            bpy.context.view_layer.update()

            bpy.ops.object.mode_set(mode='POSE')
            bpy.ops.pose.select_all(action='SELECT')
            bpy.ops.pose.copy()

            arm.animation_data.action = tr_action
            scene.frame_set(1)
            bpy.ops.pose.paste(flipped=False)
            for bone in arm.pose.bones:
                bone.keyframe_insert(data_path="location", frame=1)
                bone.keyframe_insert(data_path="rotation_quaternion", frame=1)
                bone.keyframe_insert(data_path="scale", frame=1)

            # ---------- frame 10 : first pose of TARGET ----------
            arm.animation_data.action = dst_item.action
            scene.frame_set(dst_first)
            bpy.context.view_layer.update()

            bpy.ops.pose.select_all(action='SELECT')
            bpy.ops.pose.copy()

            arm.animation_data.action = tr_action
            scene.frame_set(10)
            bpy.ops.pose.paste(flipped=False)
            for bone in arm.pose.bones:
                bone.keyframe_insert(data_path="location", frame=10)
                bone.keyframe_insert(data_path="rotation_quaternion", frame=10)
                bone.keyframe_insert(data_path="scale", frame=10)

            # linear interpolation
            for fcu in tr_action.fcurves:
                for kp in fcu.keyframe_points:
                    kp.interpolation = 'LINEAR'

            # bake frames – always include first & last, respect step
            step = 1 if props.multi_export_all_frames else 1   # change here if you ever add a separate transition-step
            frames_to_export = list(range(1, transition_frames + 1, step))
            if frames_to_export[-1] != transition_frames:
                frames_to_export.append(transition_frames)
            tr_matrices = np.zeros(len(frames_to_export) * num_bones * 16, dtype=np.float32)
            arm.animation_data.action = tr_action
            for f_idx, f_num in enumerate(frames_to_export):
                scene.frame_set(f_num)
                off = f_idx * num_bones * 16
                idx = off
                for bone in arm.pose.bones:
                    mat = BLENDER_TO_THREE @ arm.matrix_world @ bone.matrix
                    for col in range(4):
                        for row in range(4):
                            tr_matrices[idx] = mat[row][col]
                            idx += 1

            # store
            all_matrix_data.append(tr_matrices)
            start_idx = total_frames
            end_idx = total_frames + 9
            animation_helpers.append({
                'name': tr_name,
                'start_idx': start_idx,
                'end_idx': end_idx
            })
            total_frames += len(frames_to_export)
            frame_counts.append(len(frames_to_export))
            frame_steps.append(1 if props.multi_export_all_frames else 1)  # same as step above
            animation_ranges.append((1, transition_frames))

        # restore original state
        if arm.animation_data and original_action:
            arm.animation_data.action = original_action

        # ----------------------------------------------------------------
        # 4. Export
        # ----------------------------------------------------------------
        combined_data = np.concatenate(all_matrix_data)

        if export_method == 'BIN':
            blend_path = bpy.data.filepath
            if not blend_path:
                raise Exception("Please save your blend file first to use binary export")
            export_dir = os.path.join(os.path.dirname(blend_path), "animation_export")
            os.makedirs(export_dir, exist_ok=True)

            base_filename = f"{unit_name}_animations"
            raw_path = os.path.join(export_dir, f"{base_filename}.bin")
            with open(raw_path, 'wb') as f:
                combined_data.tofile(f)

            js_path = os.path.join(export_dir, f"{base_filename}.js")
            with open(js_path, 'w') as f:
                f.write(f"// Animation data for {unit_name}\n")
                f.write("// Helper functions to set up animations:\n\n")
                f.write("// ==============================================\n")
                f.write(f"//   {unit_name.title()} animations\n")
                f.write("// ==============================================\n\n")

                # Separate rest / main animations from transitions
                rest_and_main = [a for a in animation_helpers if "_To_" not in a['name']]
                transitions    = [a for a in animation_helpers if "_To_" in a['name']]

                f.write("//Main animations\n")
                for anim in rest_and_main:
                    f.write(f'material.setAnimationFrames("{unit_name}", "{anim["name"]}", {anim["start_idx"]}, {anim["end_idx"]}, 30);\n')

                if transitions:
                    f.write("\n// Transition animations\n")
                for anim in transitions:
                    f.write(f'material.setAnimationFrames(\n')
                    f.write(f'  "{unit_name}",\n')
                    f.write(f'  "{anim["name"]}",\n')
                    f.write(f'  {anim["start_idx"]},\n')
                    f.write(f'  {anim["end_idx"]},\n')
                    f.write(f'  30,\n')
                    f.write(f'  true\n')
                    f.write(f');\n')
                f.write("\n")
                # ------------------------------------------------------------------
                #  Build the setAnimationTransitions calls
                # ------------------------------------------------------------------
                main_anims = [a['name'] for a in rest_and_main
                              if a['name'] != f"{unit_name}Rest"]   # ignore rest pose
                trans_map = {}        # src -> {dst: transition_name}

                for tr in transitions:
                    src_dst = tr['name'].replace(f"{unit_name}_", "").split("_To_")
                    if len(src_dst) != 2:
                        continue
                    src, dst = src_dst
                    trans_map.setdefault(src, {})[dst] = tr['name']

                if trans_map:
                    f.write("\n")
                for src in main_anims:
                    if src not in trans_map:
                        continue
                    f.write(f'material.setAnimationTransitions("{unit_name}", "{src}", {{\n')
                    pairs = [f'    {dst}: "{tr_name}"' for dst, tr_name in trans_map[src].items()]
                    f.write(",\n".join(pairs))
                    f.write("\n});\n")
                f.write("\n")
                f.write(f"const {unit_name}_animations = {{\n")
                f.write(f"  numBones: {num_bones},\n")
                f.write(f"  totalFrames: {total_frames},\n")
                f.write(f"  animationCount: {len(animation_helpers)},\n")
                f.write("  frameCounts: [")
                f.write(", ".join(map(str, frame_counts)))
                f.write("],\n")
                f.write("  frameSteps: [")
                f.write(", ".join(map(str, frame_steps)))
                f.write("],\n")
                f.write("  animationRanges: [\n")
                for rng in animation_ranges:
                    f.write(f"    [{rng[0]}, {rng[1]}],\n")
                f.write("  ],\n")
                f.write("  animationNames: [\n")
                for item in animation_helpers:
                    f.write(f"    '{item['name']}',\n")
                f.write("  ],\n")
                f.write("  boneNames: [\n")
                for bone in arm.pose.bones:
                    f.write(f"    '{bone.name}',\n")
                f.write("  ],\n")
                f.write("};\n")

            msg = (f"Exported {len(animation_helpers)} sequences "
                   f"({total_frames} total frames) with {num_bones} bones to:\n{raw_path}\n"
                   f"JavaScript helper: {js_path}")

        elif export_method == 'PROPERTY':
            matrix_list = combined_data.tolist()
            if property_name in arm:
                del arm[property_name]
            arm[property_name] = matrix_list
            arm[f"{property_name}_totalFrames"] = total_frames
            arm[f"{property_name}_numBones"] = num_bones
            arm[f"{property_name}_animationCount"] = len(animation_helpers)
            arm[f"{property_name}_frameSteps"] = str(frame_steps)
            msg = (f"Stored {len(animation_helpers)} sequences "
                   f"({total_frames} total frames) with {num_bones} bones "
                   f"in armature custom property '{property_name}'")

        # Restore original state
        scene.frame_set(original_frame)
        if arm.animation_data and original_action:
            arm.animation_data.action = original_action

        # switch back to Object mode
        bpy.ops.object.mode_set(mode='OBJECT')

        print(msg)
        bpy.ops.export_animation.show_message('INVOKE_DEFAULT', message=msg)
        return {'FINISHED'}

    except Exception as e:
        error_msg = f"Multi-export failed: {str(e)}"
        print(error_msg)
        bpy.ops.export_animation.show_message('INVOKE_DEFAULT', message=error_msg, is_error=True)
        return {'CANCELLED'}

# --------------------------------------------------------------------
# Everything below is unchanged from previous version
# --------------------------------------------------------------------
def export_animation_frames_raw(context, animation_name, use_custom_name, start_frame=None, end_frame=None,
                                frame_step=1, export_all_frames=False, use_full_animation_range=False,
                                export_method='BIN', property_name="animation_matrices"):
    """Single-animation export – unchanged."""
    try:
        arm = context.active_object
        if not arm or arm.type != 'ARMATURE':
            raise Exception("Please select an armature object first")

        action_name = ""
        if arm.animation_data and arm.animation_data.action:
            action_name = arm.animation_data.action.name

        if not use_custom_name and action_name:
            animation_name = action_name
        elif not animation_name:
            raise Exception("Please provide an animation name or ensure the armature has an action")

        if use_full_animation_range:
            if not arm.animation_data or not arm.animation_data.action:
                raise Exception("No animation found for full range export")
            action = arm.animation_data.action
            start_frame = 1
            end_frame = int(action.frame_range[1])
        else:
            scene = context.scene
            if start_frame is None:
                start_frame = scene.frame_start
            if end_frame is None:
                end_frame = scene.frame_end

        if start_frame > end_frame:
            raise Exception("Start frame cannot be after end frame")

        current_frame = context.scene.frame_current
        num_bones = len(arm.pose.bones)

        if export_all_frames:
            frames_to_export = range(start_frame, end_frame + 1)
        else:
            frames_to_export = list(range(start_frame, end_frame + 1, frame_step))
            if frames_to_export[-1] != end_frame:
                frames_to_export.append(end_frame)

        num_frames = len(frames_to_export)
        all_matrix_data = np.zeros(num_frames * num_bones * 16, dtype=np.float32)

        for frame_idx, frame_number in enumerate(frames_to_export):
            context.scene.frame_set(frame_number)
            frame_offset = frame_idx * num_bones * 16
            idx = frame_offset
            for bone in arm.pose.bones:
                mat = BLENDER_TO_THREE @ arm.matrix_world @ bone.matrix
                for col in range(4):
                    for row in range(4):
                        all_matrix_data[idx] = mat[row][col]
                        idx += 1

        if export_method == 'BIN':
            blend_path = bpy.data.filepath
            if not blend_path:
                raise Exception("Please save your blend file first to use binary export")
            export_dir = os.path.join(os.path.dirname(blend_path), "animation_export")
            os.makedirs(export_dir, exist_ok=True)

            base_filename = f"{animation_name}_f{start_frame}_{end_frame}_n{num_frames}"
            if not export_all_frames:
                base_filename += f"_s{frame_step}"

            raw_path = os.path.join(export_dir, f"{base_filename}.bin")
            with open(raw_path, 'wb') as f:
                all_matrix_data.tofile(f)

            js_path = os.path.join(export_dir, f"{base_filename}.js")
            with open(js_path, 'w') as f:
                f.write(f"// Animation data for {animation_name}\n")
                f.write(f"const {animation_name}_animation = {{\n")
                f.write(f"  numBones: {num_bones},\n")
                f.write(f"  numFrames: {num_frames},\n")
                f.write(f"  startFrame: {start_frame},\n")
                f.write(f"  endFrame: {end_frame},\n")
                f.write(f"  frameStep: {frame_step},\n")
                f.write(f"  exportAllFrames: {str(export_all_frames).lower()},\n")
                f.write("  boneNames: [\n")
                for bone in arm.pose.bones:
                    f.write(f"    '{bone.name}',\n")
                f.write("  ],\n")
                f.write("};\n\n")
                f.write(f"// Helper function to set up animation:\n")
                f.write(f"material.setAnimationFrames('{animation_name}', 0, {num_frames-1}, 30);\n")

            msg = f"Exported {num_frames} frames with {num_bones} bones to:\n{raw_path}\nJavaScript helper: {js_path}"

        elif export_method == 'PROPERTY':
            matrix_list = all_matrix_data.tolist()
            if property_name in arm:
                del arm[property_name]
            arm[property_name] = matrix_list
            arm[f"{property_name}_numFrames"] = num_frames
            arm[f"{property_name}_numBones"] = num_bones
            msg = f"Stored {num_frames} frames with {num_bones} bones in armature custom property '{property_name}'"

        context.scene.frame_set(current_frame)
        # switch back to Object mode
        bpy.ops.object.mode_set(mode='OBJECT')

        print(msg)
        bpy.ops.export_animation.show_message('INVOKE_DEFAULT', message=msg)
        return {'FINISHED'}

    except Exception as e:
        error_msg = f"Export failed: {str(e)}"
        print(error_msg)
        bpy.ops.export_animation.show_message('INVOKE_DEFAULT', message=error_msg, is_error=True)
        return {'CANCELLED'}

class ShowMessageOperator(bpy.types.Operator):
    bl_idname = "export_animation.show_message"
    bl_label = "Export Result"

    message: bpy.props.StringProperty(default="")
    is_error: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        return {'FINISHED'}

    def invoke(self, context, event):
        icon = 'ERROR' if self.is_error else 'INFO'
        title = "Export Error" if self.is_error else "Export Complete"
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        for line in self.message.split('\n'):
            layout.label(text=line)

class ExportAnimationPanel(bpy.types.Panel):
    bl_label = "Animation Frame Exporter"
    bl_idname = "OBJECT_PT_export_animation"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "object"

    def draw(self, context):
        layout = self.layout
        props = context.scene.animation_export_props

        if context.object and context.object.type == 'ARMATURE':
            box = layout.box()
            box.label(text="Export Mode", icon='EXPORT')
            box.prop(props, "export_mode", expand=True)

            if props.export_mode == 'SINGLE':
                box = layout.box()
                box.label(text="Animation Settings", icon='ANIM')
                arm = context.object
                action_name = ""
                if arm.animation_data and arm.animation_data.action:
                    action_name = arm.animation_data.action.name
                    box.label(text=f"Current Action: {action_name}", icon='ACTION')
                row = box.row()
                row.prop(props, "use_custom_name")
                if props.use_custom_name:
                    box.prop(props, "animation_name")

                box.label(text="Frame Range:", icon='TIME')
                row = box.row()
                row.prop(props, "use_full_animation_range")
                if not props.use_full_animation_range:
                    row = box.row()
                    row.prop(props, "start_frame")
                    row.prop(props, "end_frame")

                box.label(text="Frame Sampling:", icon='RENDER_ANIMATION')
                row = box.row()
                row.prop(props, "frame_step")
                row.prop(props, "export_all_frames")

                box.label(text="Export Method:", icon='EXPORT')
                box.prop(props, "export_method", expand=True)
                if props.export_method == 'PROPERTY':
                    box.prop(props, "property_name")
            else:
                box = layout.box()
                box.label(text="Multi-Export Settings", icon='ANIM_DATA')
                box.prop(props, "unit_name")
                # transition_frames is fixed at 10
                row = box.row()
                row.prop(props, "multi_export_all_frames", text="Export All Frames For All")

                box.label(text="Available Animations:", icon='ACTION')
                row = box.row()
                row.operator("animation_list.refresh", icon='FILE_REFRESH')

                box.template_list(
                    "AnimationList", "", context.scene, "animation_list",
                    context.scene, "animation_list_index", rows=4
                )
                if context.scene.animation_list_index >= 0 and len(context.scene.animation_list) > 0:
                    box.operator("animation_list.edit_settings", icon='PREFERENCES')

                box.label(text="Export Method:", icon='EXPORT')
                box.prop(props, "export_method", expand=True)
                if props.export_method == 'PROPERTY':
                    box.prop(props, "property_name")

            layout.operator("export_animation.export", text="Export Animation", icon='EXPORT')
        else:
            layout.label(text="Select an armature to export", icon='ERROR')

class ExportAnimationOperator(bpy.types.Operator):
    bl_idname = "export_animation.export"
    bl_label = "Export Animation Frames"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.animation_export_props
        if props.export_mode == 'SINGLE':
            return export_animation_frames_raw(
                context=context,
                animation_name=props.animation_name,
                use_custom_name=props.use_custom_name,
                start_frame=props.start_frame if props.start_frame != 0 and not props.use_full_animation_range else None,
                end_frame=props.end_frame if props.end_frame != 0 and not props.use_full_animation_range else None,
                frame_step=props.frame_step,
                export_all_frames=props.export_all_frames,
                use_full_animation_range=props.use_full_animation_range,
                export_method=props.export_method,
                property_name=props.property_name
            )
        else:
            return export_multiple_animations(
                context=context,
                unit_name=props.unit_name,
                export_method=props.export_method,
                property_name=props.property_name,
                export_all_frames=props.multi_export_all_frames
            )

# --------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------
def register():
    bpy.utils.register_class(AnimationExportProperties)
    bpy.utils.register_class(AnimationListItem)
    bpy.utils.register_class(AnimationList)
    bpy.utils.register_class(AnimationList_OT_Refresh)
    bpy.utils.register_class(AnimationList_OT_EditSettings)
    bpy.utils.register_class(ShowMessageOperator)
    bpy.utils.register_class(ExportAnimationPanel)
    bpy.utils.register_class(ExportAnimationOperator)
    bpy.types.Scene.animation_export_props = bpy.props.PointerProperty(type=AnimationExportProperties)
    bpy.types.Scene.animation_list = bpy.props.CollectionProperty(type=AnimationListItem)
    bpy.types.Scene.animation_list_index = bpy.props.IntProperty(name="Index for animation_list", default=0)

def unregister():
    bpy.utils.unregister_class(AnimationExportProperties)
    bpy.utils.unregister_class(AnimationListItem)
    bpy.utils.unregister_class(AnimationList)
    bpy.utils.unregister_class(AnimationList_OT_Refresh)
    bpy.utils.unregister_class(AnimationList_OT_EditSettings)
    bpy.utils.unregister_class(ShowMessageOperator)
    bpy.utils.unregister_class(ExportAnimationPanel)
    bpy.utils.unregister_class(ExportAnimationOperator)
    del bpy.types.Scene.animation_export_props
    del bpy.types.Scene.animation_list
    del bpy.types.Scene.animation_list_index

if __name__ == "__main__":
    register()