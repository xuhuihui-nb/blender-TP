import bpy
import bmesh
import mathutils
import time
from mathutils import kdtree
from bpy_extras.view3d_utils import (
    region_2d_to_origin_3d,
    region_2d_to_vector_3d,
    location_3d_to_region_2d
)

from .draw_utils import draw_callback, draw_text_callback, is_point_in_mask, clamp_to_mask_boundary

_active_draw_operator = None

def is_boundary_edge(e, grid_layer=None):
    """
    检查边是否是边界边（连通面数 <= 1，或者其连接的面属于不同的栅格填充区域，即跨区域边界）
    """
    if len(e.link_faces) <= 1:
        return True
    if not grid_layer:
        return False
    # 获取所有连接面的栅格 ID，如果有不同的 ID 则是拼接边界
    ids = {f[grid_layer] for f in e.link_faces}
    return len(ids) > 1

def ray_cast_multi(obj, origin_local, direction_local, depsgraph=None):
    """
    沿着射线进行迭代投影，获取所有相交点（前表面、后表面、以及深度方向上重叠的各个物体的进出点）。
    """
    hits = []
    curr_origin = origin_local.copy()
    eps = 0.001
    max_hits = 20
    
    for _ in range(max_hits):
        try:
            if depsgraph:
                success, location, normal, face_idx = obj.ray_cast(
                    curr_origin,
                    direction_local,
                    depsgraph=depsgraph
                )
            else:
                success, location, normal, face_idx = obj.ray_cast(
                    curr_origin,
                    direction_local
                )
        except Exception:
            try:
                success, location, normal, face_idx = obj.ray_cast(
                    curr_origin,
                    direction_local
                )
            except Exception:
                success = False
                
        if not success:
            break
            
        hits.append((location, normal, face_idx))
        # 沿着射线方向微调起点以穿透当前面，继续寻找下一个相交面
        curr_origin = location + direction_local * eps
        
    return hits

class OBJECT_OT_tp_topology_draw(bpy.types.Operator):
    bl_idname = "object.tp_topology_draw"
    bl_label = "TP拓扑绘制"
    bl_description = "在选中网格对象的表面绘制连续的拓扑线 (Ctrl + 左键拖动/单击)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.window_manager.tp_topology_running:
            return True
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT' and obj.name != "TP_Topology_Mesh"

    def ensure_topo_obj_exists(self, context):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        
        if not topo_obj:
            topo_mesh = bpy.data.meshes.get(topo_obj_name)
            if topo_mesh:
                try:
                    bpy.data.meshes.remove(topo_mesh)
                except Exception:
                    pass
            topo_mesh = bpy.data.meshes.new(topo_obj_name)
            topo_obj = bpy.data.objects.new(topo_obj_name, topo_mesh)
            context.collection.objects.link(topo_obj)
            
            topo_obj.show_in_front = True
            topo_obj.show_wire = True
            
            mat_name = "TP_Topology_Material"
            mat = bpy.data.materials.get(mat_name)
            if not mat:
                mat = bpy.data.materials.new(mat_name)
                mat.use_nodes = True
                mat.diffuse_color = (0.0, 1.0, 0.5, 1.0)
                
                if mat.node_tree:
                    nodes = mat.node_tree.nodes
                    principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                    if principled:
                        principled.inputs['Base Color'].default_value = (0.0, 1.0, 0.5, 1.0)
                        if 'Emission' in principled.inputs:
                            principled.inputs['Emission'].default_value = (0.0, 1.0, 0.5, 1.0)
                        if 'Emission Strength' in principled.inputs:
                            principled.inputs['Emission Strength'].default_value = 1.0
            if mat not in topo_obj.data.materials[:]:
                topo_obj.data.materials.append(mat)
                
            for o in context.selected_objects:
                o.select_set(False)
            topo_obj.select_set(True)
            context.view_layer.objects.active = topo_obj
            
            self.rebuild_kd_tree()
            
        if context.scene.tp_use_wrap:
            self.ensure_shrinkwrap_modifier(context, topo_obj)
        update_mirror_modifier(context)
        return topo_obj

    def ensure_shrinkwrap_modifier(self, context, topo_obj):
        """配置极其平滑且物理隔离的笼子级(On Cage)收缩包裹修改器"""
        ref_obj_name = getattr(self, 'ref_object_name', '')
        if not ref_obj_name:
            ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        if not ref_obj:
            return
            
        mod_name = "TP_Shrinkwrap"
        mod = topo_obj.modifiers.get(mod_name)
        if not mod:
            mod = topo_obj.modifiers.new(name=mod_name, type='SHRINKWRAP')
            
        mod.target = ref_obj
        
        # 1. 采用高度平滑且专为拓扑设计的 TARGET_PROJECT (目标法线投影) 模式，大幅减少跳跃与抖动
        try:
            mod.wrap_method = 'TARGET_PROJECT'
        except Exception:
            mod.wrap_method = 'NEAREST_SURFACEPOINT'
            
        # 2. 设置保持在表面上方，防止低模由于遮挡而插入高模内部，彻底解决闪烁问题
        try:
            mod.wrap_mode = 'ABOVE_SURFACE'
        except Exception:
            mod.wrap_mode = 'ON_SURFACE'
            
        mod.offset = 0.0005
        mod.show_in_editmode = True
        
        # 3. 关键选项：开启笼子显示，强制 Blender 编辑手柄(Gizmo)物理投射到高模表面，解决操作分离感
        mod.show_on_cage = True

    def enforce_topology_state(self, context):
        """强固拓扑模式所需的状态：实时保障 C++ 级底层物理吸附在调整时正常工作"""
        boundary_mode = context.scene.tp_boundary_mode
        if getattr(self, 'last_boundary_mode', None) != boundary_mode:
            self.last_boundary_mode = boundary_mode
            if boundary_mode:
                self.clear_internal_selections(context)
                try:
                    context.scene.tool_settings.mesh_select_mode = (False, True, False)
                except Exception:
                    pass
            else:
                try:
                    context.scene.tool_settings.mesh_select_mode = (True, False, False)
                except Exception:
                    pass

        ref_obj = bpy.data.objects.get(self.ref_object_name)
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        
        if ref_obj:
            if not ref_obj.hide_select:
                ref_obj.hide_select = True
            if ref_obj.select_get():
                ref_obj.select_set(False)
                
        if topo_obj:
            if context.view_layer.objects.active != topo_obj:
                context.view_layer.objects.active = topo_obj
            if not topo_obj.select_get():
                topo_obj.select_set(True)
            
            # 根据“包裹”状态动态控制修改器
            if context.scene.tp_use_wrap:
                self.ensure_shrinkwrap_modifier(context, topo_obj)
            else:
                mod = topo_obj.modifiers.get("TP_Shrinkwrap")
                if mod:
                    try:
                        topo_obj.modifiers.remove(mod)
                    except Exception:
                        pass
                        
            # 根据“对称”状态动态控制修改器
            update_mirror_modifier(context)
                    
        # 强制自动合并
        if not context.scene.tool_settings.use_mesh_automerge:
            context.scene.tool_settings.use_mesh_automerge = True
            
        # 根据“包裹”状态动态控制吸附
        if context.scene.tp_use_wrap:
            if not context.scene.tool_settings.use_snap:
                context.scene.tool_settings.use_snap = True
                
            try:
                if context.scene.tool_settings.snap_elements != {'FACE_NEAREST'}:
                    context.scene.tool_settings.snap_elements = {'FACE_NEAREST'}
            except Exception:
                try:
                    if context.scene.tool_settings.snap_elements != {'FACE'}:
                        context.scene.tool_settings.snap_elements = {'FACE'}
                except Exception:
                    pass
                    
            if context.scene.tool_settings.snap_target != 'CLOSEST':
                context.scene.tool_settings.snap_target = 'CLOSEST'
                
            if hasattr(context.scene.tool_settings, "use_snap_project"):
                if not context.scene.tool_settings.use_snap_project:
                    context.scene.tool_settings.use_snap_project = True

            if hasattr(context.scene.tool_settings, "use_snap_selectable"):
                if context.scene.tool_settings.use_snap_selectable:
                    context.scene.tool_settings.use_snap_selectable = False
            if hasattr(context.scene.tool_settings, "use_snap_self"):
                if context.scene.tool_settings.use_snap_self:
                    context.scene.tool_settings.use_snap_self = False
        else:
            # 还原为备份的原始吸附状态
            try:
                if hasattr(self, 'orig_use_snap'):
                    context.scene.tool_settings.use_snap = self.orig_use_snap
                if hasattr(self, 'orig_snap_elements'):
                    context.scene.tool_settings.snap_elements = self.orig_snap_elements
                if hasattr(self, 'orig_snap_target'):
                    context.scene.tool_settings.snap_target = self.orig_snap_target
                if hasattr(self, 'orig_use_snap_project') and hasattr(context.scene.tool_settings, 'use_snap_project'):
                    context.scene.tool_settings.use_snap_project = self.orig_use_snap_project
                if hasattr(self, 'orig_use_snap_selectable') and hasattr(context.scene.tool_settings, "use_snap_selectable"):
                    context.scene.tool_settings.use_snap_selectable = self.orig_use_snap_selectable
                if hasattr(self, 'orig_use_snap_self') and hasattr(context.scene.tool_settings, "use_snap_self"):
                    context.scene.tool_settings.use_snap_self = self.orig_use_snap_self
            except Exception:
                pass

    def invoke(self, context, event):
        if context.window_manager.tp_topology_running:
            context.window_manager.tp_topology_running = False
            return {'FINISHED'}

        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "必须在3D视图中运行此工具")
            return {'CANCELLED'}
            
        ref_obj = context.active_object
        if not ref_obj or ref_obj.type != 'MESH':
            self.report({'WARNING'}, "未选中有效的网格对象")
            return {'CANCELLED'}
            
        if ref_obj.name == "TP_Topology_Mesh":
            self.report({'WARNING'}, "不能将拓扑网格自身作为拓扑目标，请选择要拓扑的源高模网格")
            return {'CANCELLED'}
            
        self.ref_object_name = ref_obj.name
        context.window_manager.tp_ref_object_name = ref_obj.name
        self.is_drawing = False
        self.is_dragging = False
        self.is_polyline = False
        self.is_outside_drawing = False
        self._cursor_hidden = False
        self.is_grabbing = False
        self.grab_dragged = False
        self.grab_initial_cos = {}
        self.grab_active_vert_idx = None
        self.grab_mouse_start = (event.mouse_region_x, event.mouse_region_y)
        self.grab_initial_depth = 0.0
        self.grab_snap_target_idx = None
        self.stroke_points = []
        self.stroke_snap_indices = []
        self.last_mouse_coord = (event.mouse_region_x, event.mouse_region_y)
        self.last_mouse_coord_prev = (event.mouse_region_x, event.mouse_region_y)
        self.drag_start_coord = (event.mouse_region_x, event.mouse_region_y)
        self.hover_snap_pt = None
        self.kd_tree = None
        self.internal_grid_verts = set()
        self.stroke_history = []
        self.last_clicked_vert_idx = None
        self.last_clicked_cycles = []
        self.last_clicked_cycle_idx = -1
        self.last_ctrl_state = False
        self.subdiv_original_loops = None
        self.subdiv_multiplier = 1.0
        self.start_from_selected_v_co = None
        self.start_from_selected_v_idx = None
        self.max_drag_dist_from_start = 0.0
        self.ui_click_start_pos = None
        self.ui_click_edge_length = 0.1
        self.ui_is_dragging = False
        # Double-click detection tracking (time-based, since DOUBLE_CLICK value not fired in modal)
        self._boundary_last_click_time = 0.0
        self._boundary_last_click_idx = None
        self.is_smoothing = False
        self.smooth_mouse_start = (event.mouse_region_x, event.mouse_region_y)
        self.smooth_dragged = False
        self.smooth_brush_radius = 50.0
        self.alt_pressed = event.alt
        self.shift_pressed = event.shift
        self.was_loopcut_active = False
        
        ref_obj.hide_select = True
        ref_obj.select_set(False)
        
        if context.object and context.object.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass
                
        topo_obj = self.ensure_topo_obj_exists(context)
        
        for o in context.selected_objects:
            o.select_set(False)
        topo_obj.select_set(True)
        context.view_layer.objects.active = topo_obj
        
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            if context.scene.tp_boundary_mode:
                context.scene.tool_settings.mesh_select_mode = (False, True, False)
            else:
                context.scene.tool_settings.mesh_select_mode = (True, False, False)
        except Exception as e:
            print("Failed to enter Edit Mode:", e)
            
        self.rebuild_kd_tree()
        self.last_boundary_mode = context.scene.tp_boundary_mode
        if self.last_boundary_mode:
            self.clear_internal_selections(context)

        context.window_manager.tp_topology_running = True
        global _active_draw_operator
        _active_draw_operator = self

        args = (self, context)
        self.draw_handle_lines = bpy.types.SpaceView3D.draw_handler_add(
            draw_callback, args, 'WINDOW', 'POST_VIEW'
        )
        self.draw_handle_text = bpy.types.SpaceView3D.draw_handler_add(
            draw_text_callback, args, 'WINDOW', 'POST_PIXEL'
        )
        
        try:
            context.workspace.status_text_set("TP拓扑模式 | Ctrl+左键拖拽: 连续绘制 | Ctrl+左键单击: 绘制多段线 | Alt+左键: 选中圈/循环边 | 右键/回车: 提交 | ESC退出")
        except Exception:
            pass
            
        try:
            context.window.cursor_modal_set('CROSSHAIR')
        except Exception:
            pass
        context.window_manager.modal_handler_add(self)
        
        # 备份并替换用户的吸附状态为高模拓扑专用状态
        self.orig_use_snap = context.scene.tool_settings.use_snap
        self.orig_snap_elements = context.scene.tool_settings.snap_elements.copy() if hasattr(context.scene.tool_settings.snap_elements, "copy") else set(context.scene.tool_settings.snap_elements)
        self.orig_snap_target = context.scene.tool_settings.snap_target
        if hasattr(context.scene.tool_settings, "use_snap_project"):
            self.orig_use_snap_project = context.scene.tool_settings.use_snap_project
        
        self.orig_use_mesh_automerge = context.scene.tool_settings.use_mesh_automerge
        
        if hasattr(context.scene.tool_settings, "use_snap_selectable"):
            self.orig_use_snap_selectable = context.scene.tool_settings.use_snap_selectable
            context.scene.tool_settings.use_snap_selectable = False
            
        if hasattr(context.scene.tool_settings, "use_snap_self"):
            self.orig_use_snap_self = context.scene.tool_settings.use_snap_self
            context.scene.tool_settings.use_snap_self = False
            
        context.scene.tool_settings.use_mesh_automerge = True
        
        if context.scene.tp_use_wrap:
            context.scene.tool_settings.use_snap = True
            try:
                context.scene.tool_settings.snap_elements = {'FACE_NEAREST'}
            except Exception:
                try:
                    context.scene.tool_settings.snap_elements = {'FACE'}
                except Exception:
                    pass
            context.scene.tool_settings.snap_target = 'CLOSEST'
            if hasattr(context.scene.tool_settings, "use_snap_project"):
                context.scene.tool_settings.use_snap_project = True
        
        try:
            bpy.ops.ed.undo_push(message="进入拓扑模式")
        except Exception as e:
            print("Error pushing undo step:", e)
            
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def is_loopcut_active(self, context):
        # Check modal operators (Blender 4.2+)
        window = getattr(context, "window", None)
        if window and hasattr(window, "modal_operators"):
            for op in window.modal_operators:
                if "loopcut" in op.bl_idname.lower() or "loop_cut" in op.bl_idname.lower():
                    return True

        # Check active tool (if activated via toolbar)
        try:
            workspace = context.workspace
            if workspace and hasattr(workspace, "tools"):
                mode = getattr(context, "mode", 'EDIT_MESH')
                active_tool = workspace.tools.from_space_view3d_mode(mode)
                if active_tool and ("loopcut" in active_tool.idname.lower() or "loop_cut" in active_tool.idname.lower()):
                    return True
        except Exception as e:
            print("Error checking active tool:", e)

        return False

    def modal(self, context, event):
        if not context.window_manager.tp_topology_running:
            self.cleanup(context)
            self.report({'INFO'}, "已退出TP拓扑模式")
            return {'FINISHED'}

        if not context.region or not context.area:
            if event.type == 'ESC':
                self.cleanup(context)
                self.report({'INFO'}, "已退出TP拓扑模式")
                return {'FINISHED'}
            return {'PASS_THROUGH'}

        # Check if loop cut is active
        loopcut_active = self.is_loopcut_active(context)
        if loopcut_active:
            self.was_loopcut_active = True
            return {'PASS_THROUGH'}
        else:
            if getattr(self, 'was_loopcut_active', False):
                self.was_loopcut_active = False
                self.rebuild_kd_tree()
                self.subdiv_original_loops = None
                self.subdiv_multiplier = 1.0
                if context.scene.tp_boundary_mode:
                    self.clear_internal_selections(context)



        # Update alt_pressed and shift_pressed state and trigger redraw on change
        alt_state = event.alt
        if getattr(self, 'alt_pressed', False) != alt_state:
            self.alt_pressed = alt_state
            if context.area:
                context.area.tag_redraw()

        shift_state = event.shift
        if getattr(self, 'shift_pressed', False) != shift_state:
            self.shift_pressed = shift_state
            if context.area:
                context.area.tag_redraw()

        self.enforce_topology_state(context)

        # Track UI dragging to prevent dragging the tp_edge_length property
        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                is_in_ui = False
                for region in context.area.regions:
                    if region.type == 'UI':
                        if (region.x <= event.mouse_x <= region.x + region.width and
                            region.y <= event.mouse_y <= region.y + region.height):
                            is_in_ui = True
                            break
                if is_in_ui:
                    self.ui_click_start_pos = (event.mouse_x, event.mouse_y)
                    self.ui_click_edge_length = context.scene.tp_edge_length
                    self.ui_is_dragging = False
            elif event.value == 'RELEASE':
                self.ui_click_start_pos = None
                self.ui_is_dragging = False
        elif event.type == 'MOUSEMOVE':
            if getattr(self, 'ui_click_start_pos', None) is not None:
                dx = event.mouse_x - self.ui_click_start_pos[0]
                dy = event.mouse_y - self.ui_click_start_pos[1]
                if (dx * dx + dy * dy) > 64:  # distance > 8 pixels
                    self.ui_is_dragging = True

        # Reset the original loops cache if the user starts a drawing stroke or grab/move action.
        # We do NOT reset it when the Ctrl key is released, to allow continuous subdivision/unsubdivision adjustments without cumulative shape distortion.
        if event.value == 'PRESS':
            if event.type == 'G':
                self.subdiv_original_loops = None
                self.subdiv_multiplier = 1.0
            elif event.type == 'LEFTMOUSE' and event.ctrl:
                self.subdiv_original_loops = None
                self.subdiv_multiplier = 1.0

        if getattr(self, 'is_grabbing', False):
            return self.handle_grab_modal(context, event)

        if getattr(self, 'is_smoothing', False):
            return self.handle_smooth_modal(context, event)

        if event.ctrl and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            if getattr(context.scene, "tp_square_mode", False):
                curr_count = getattr(context.scene, "tp_square_count", '1')
                if event.type == 'WHEELUPMOUSE':
                    if curr_count == '1':
                        next_count = '4'
                    elif curr_count == '4':
                        next_count = '16'
                    elif curr_count == '16':
                        next_count = '64'
                    else:
                        next_count = '1'
                else:  # WHEELDOWNMOUSE
                    if curr_count == '1':
                        next_count = '64'
                    elif curr_count == '64':
                        next_count = '16'
                    elif curr_count == '16':
                        next_count = '4'
                    else:
                        next_count = '1'
                context.scene.tp_square_count = next_count
                if context.area:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            elif getattr(context.scene, "tp_circle_mode", False):
                curr_count = getattr(context.scene, "tp_circle_count", '4')
                if event.type == 'WHEELUPMOUSE':
                    if curr_count == '4':
                        next_count = '8_ring'
                    else:
                        next_count = '4'
                else:  # WHEELDOWNMOUSE
                    if curr_count == '4':
                        next_count = '8_ring'
                    else:
                        next_count = '4'
                context.scene.tp_circle_count = next_count
                if context.area:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if self.handle_loop_subdivision(context, event):
                return {'RUNNING_MODAL'}

        ctrl_pressed = event.ctrl
        if ctrl_pressed and not getattr(self, 'last_ctrl_state', False):
            topo_obj_name = "TP_Topology_Mesh"
            topo_obj = bpy.data.objects.get(topo_obj_name)
            if topo_obj and topo_obj.mode == 'EDIT':
                try:
                    bm_weld = bmesh.from_edit_mesh(topo_obj.data)
                    weld_dist = 0.001  # Safe threshold to remove exact duplicates on Ctrl press
                    bmesh.ops.remove_doubles(bm_weld, verts=bm_weld.verts, dist=weld_dist)
                    bmesh.update_edit_mesh(topo_obj.data)
                except Exception as e:
                    print("Error welding vertices on Ctrl press:", e)
        self.last_ctrl_state = ctrl_pressed

        is_square = getattr(context.scene, "tp_square_mode", False)
        is_circle = getattr(context.scene, "tp_circle_mode", False)
        if (is_square or is_circle) and ctrl_pressed:
            if not getattr(self, '_cursor_hidden', False):
                try:
                    context.window.cursor_modal_restore()
                except Exception:
                    pass
                try:
                    context.window.cursor_modal_set('NONE')
                except Exception:
                    pass
                self._cursor_hidden = True
        else:
            if getattr(self, '_cursor_hidden', False):
                try:
                    context.window.cursor_modal_restore()
                except Exception:
                    pass
                try:
                    context.window.cursor_modal_set('CROSSHAIR')
                except Exception:
                    pass
                self._cursor_hidden = False

        if event.type == 'ESC':
            self.cleanup(context)
            self.report({'INFO'}, "已退出TP拓扑模式")
            return {'FINISHED'}

        if event.value == 'RELEASE' and event.type in {'LEFT_CTRL', 'RIGHT_CTRL'}:
            if self.is_drawing:
                if len(self.stroke_points) >= 2:
                    self.create_geometry(context)
                    try:
                        bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                    except Exception as e:
                        print("Error pushing undo step:", e)
                    self.report({'INFO'}, "已提交绘制")
                self.stroke_points = []
                self.stroke_snap_indices = []
                self.is_drawing = False
                self.is_polyline = False
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

        if event.value == 'PRESS':
            if event.type == 'LEFTMOUSE':
                if event.shift and not event.alt and context.scene.tp_boundary_mode:
                    self.is_smoothing = True
                    self.smooth_mouse_start = (event.mouse_region_x, event.mouse_region_y)
                    self.last_mouse_coord = (event.mouse_region_x, event.mouse_region_y)
                    self.smooth_dragged = False
                    self.smooth_initial_cos = {}
                    self.smoothed_vert_indices = set()
                    # Make sure edit mode exists and is active
                    topo_obj = self.ensure_topo_obj_exists(context)
                    if topo_obj.mode != 'EDIT':
                        try:
                            bpy.ops.object.mode_set(mode='EDIT')
                        except Exception as e:
                            print("Failed to enter Edit Mode on smooth start:", e)
                            
                    # Backup bmesh for smooth cancel
                    try:
                        bm_smooth = bmesh.from_edit_mesh(topo_obj.data)
                        if hasattr(self, 'smooth_backup_bm') and self.smooth_backup_bm:
                            self.smooth_backup_bm.free()
                        self.smooth_backup_bm = bm_smooth.copy()
                    except Exception as e:
                        print("Error backing up bmesh for smoothing:", e)
                        self.smooth_backup_bm = None
                        
                    self.rebuild_kd_tree()
                    context.workspace.status_text_set("TP拓扑平滑边界 | 拖动鼠标: 平滑白线边界 | 释放左键: 确定 | ESC/右键: 取消")
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                elif event.alt:
                    return self.perform_loop_selection(context, (event.mouse_region_x, event.mouse_region_y), event.shift, alt_pressed=True)

            if event.type == 'Z' and event.ctrl:
                if event.shift:
                    try:
                        bpy.ops.ed.redo()
                    except Exception:
                        pass
                    self.subdiv_original_loops = None
                    self.subdiv_multiplier = 1.0
                    self.rebuild_kd_tree()
                    self.stroke_points = []
                    self.stroke_snap_indices = []
                    self.is_drawing = False
                    self.is_polyline = False
                    self.start_from_selected_v_co = None
                    self.start_from_selected_v_idx = None
                    self.enforce_topology_state(context)
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                else:
                    if self.is_drawing and self.is_polyline and self.stroke_points:
                        self.stroke_points.pop()
                        self.stroke_snap_indices.pop()
                        if not self.stroke_points:
                            self.is_drawing = False
                            self.is_polyline = False
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                    else:
                        try:
                            bpy.ops.ed.undo()
                        except Exception:
                            pass
                        self.subdiv_original_loops = None
                        self.subdiv_multiplier = 1.0
                        self.rebuild_kd_tree()
                        self.stroke_points = []
                        self.stroke_snap_indices = []
                        self.is_drawing = False
                        self.is_polyline = False
                        self.start_from_selected_v_co = None
                        self.start_from_selected_v_idx = None
                        self.enforce_topology_state(context)
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
            elif event.type == 'Y' and event.ctrl:
                try:
                    bpy.ops.ed.redo()
                except Exception:
                    pass
                self.subdiv_original_loops = None
                self.subdiv_multiplier = 1.0
                self.rebuild_kd_tree()
                self.stroke_points = []
                self.stroke_snap_indices = []
                self.is_drawing = False
                self.is_polyline = False
                self.start_from_selected_v_co = None
                self.start_from_selected_v_idx = None
                self.enforce_topology_state(context)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            elif event.type == 'RIGHTMOUSE':
                if self.is_drawing:
                    self.stroke_points = []
                    self.stroke_snap_indices = []
                    self.is_drawing = False
                    self.is_polyline = False
                    self.is_outside_drawing = False
                    self.start_from_selected_v_co = None
                    self.start_from_selected_v_idx = None
                    self.report({'INFO'}, "已取消绘制")
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                
            elif event.type in {'RET', 'NUMPAD_ENTER'}:
                if self.is_drawing and self.is_polyline:
                    if len(self.stroke_points) >= 2:
                        self.create_geometry(context)
                        try:
                            bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                        except Exception as e:
                            print("Error pushing undo step:", e)
                        self.report({'INFO'}, "已提交多段线")
                    self.stroke_points = []
                    self.stroke_snap_indices = []
                    self.is_drawing = False
                    self.is_polyline = False
                    self.start_from_selected_v_co = None
                    self.start_from_selected_v_idx = None
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}

        if event.type in {'MOUSEMOVE', 'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}:
            self.last_mouse_coord = (event.mouse_region_x, event.mouse_region_y)

        if event.ctrl:
            self.rebuild_kd_tree()
            snap_pt, _ = self.find_nearest_vertex(context, self.last_mouse_coord, threshold_pixels=20)
            self.hover_snap_pt = snap_pt
        else:
            self.hover_snap_pt = None

        if context.area:
            context.area.tag_redraw()

        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if not self.is_drawing:
            if event.type == 'G' and event.value == 'PRESS':
                topo_obj_name = "TP_Topology_Mesh"
                topo_obj = bpy.data.objects.get(topo_obj_name)
                if topo_obj and topo_obj.mode == 'EDIT':
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    bm.verts.ensure_lookup_table()
                    selected_verts = [v for v in bm.verts if v.select]
                    if not selected_verts:
                        coord = (event.mouse_region_x, event.mouse_region_y)
                        snap_pt, snap_v_idx = self.find_nearest_vertex(context, coord, threshold_pixels=20)
                        if snap_v_idx is not None and snap_v_idx < len(bm.verts):
                            v_to_select = bm.verts[snap_v_idx]
                            v_to_select.select = True
                            bm.select_history.clear()
                            bm.select_history.add(v_to_select)
                            bmesh.update_edit_mesh(topo_obj.data)
                            selected_verts = [v_to_select]
                            
                    if selected_verts:
                        self.rebuild_kd_tree()
                        self.is_grabbing = True
                        if hasattr(self, 'grab_backup_bm') and self.grab_backup_bm:
                            self.grab_backup_bm.free()
                        self.grab_backup_bm = bm.copy()
                        self.init_grab_influence(context, selected_verts)
                        
                        region = context.region
                        rv3d = context.space_data.region_3d
                        mouse_vec = mathutils.Vector((event.mouse_region_x, event.mouse_region_y))
                        min_dist = float('inf')
                        active_v = None
                        for v in selected_verts:
                            screen_coord = location_3d_to_region_2d(region, rv3d, topo_obj.matrix_world @ v.co)
                            if screen_coord:
                                dist = (screen_coord - mouse_vec).length
                                if dist < min_dist:
                                    min_dist = dist
                                    active_v = v
                        if not active_v:
                            active_history = bm.select_history.active
                            if active_history and isinstance(active_history, bmesh.types.BMVert) and active_history.select:
                                active_v = active_history
                            else:
                                active_v = selected_verts[0]
                                
                        self.grab_active_vert_idx = active_v.index
                        self.grab_mouse_start = (event.mouse_region_x, event.mouse_region_y)
                        active_start_world = topo_obj.matrix_world @ active_v.co
                        self.last_valid_active_world = active_start_world.copy()
                        ray_origin_start = region_2d_to_origin_3d(region, rv3d, self.grab_mouse_start)
                        ray_vector_start = region_2d_to_vector_3d(region, rv3d, self.grab_mouse_start)
                        self.grab_initial_depth = (active_start_world - ray_origin_start).dot(ray_vector_start)
                        self.grab_snap_target_idx = None
                        self.hover_snap_pt = None
                        
                        context.workspace.status_text_set("TP拓扑移动 | 移动鼠标: 调整位置 | 左键/回车: 确定并吸附合并 | 右键/ESC: 取消")
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}

            if not context.region or context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
                
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and not event.ctrl:
                if context.scene.tp_boundary_mode:
                    coord = (event.mouse_region_x, event.mouse_region_y)
                    self.rebuild_kd_tree()
                    element_type, target_indices, active_idx = self.find_nearest_boundary_element(context, coord, threshold_pixels=20)
                    
                    if element_type is not None and active_idx is not None:
                        # --- Double-click detection: two PRESS events within 0.35s on same vertex ---
                        now = time.time()
                        last_t = getattr(self, '_boundary_last_click_time', 0.0)
                        last_idx = getattr(self, '_boundary_last_click_idx', None)
                        if (now - last_t) < 0.35 and last_idx == active_idx:
                            # This is a double-click: toggle pin, skip grab
                            self._boundary_last_click_time = 0.0
                            self._boundary_last_click_idx = None
                            topo_obj = self.ensure_topo_obj_exists(context)
                            if topo_obj.mode != 'EDIT':
                                try:
                                    bpy.ops.object.mode_set(mode='EDIT')
                                except Exception as e:
                                    print("Failed to enter Edit Mode on double click:", e)
                            
                            # Cancel any pending grab
                            self.is_grabbing = False
                            self.is_dragging_grab = False
                            if getattr(self, 'grab_backup_bm', None):
                                self.grab_backup_bm.free()
                                self.grab_backup_bm = None
                            
                            bm = bmesh.from_edit_mesh(topo_obj.data)
                            bm.verts.ensure_lookup_table()
                            bm.verts.index_update()
                            # Get/create layers BEFORE fetching vertex reference:
                            # adding a new layer can rebuild BMesh internals and
                            # invalidate any pre-existing BMVert references.
                            pin_layer = bm.verts.layers.int.get("tp_is_pinned") or bm.verts.layers.int.new("tp_is_pinned")
                            co_layer = bm.verts.layers.float_vector.get("tp_pinned_co") or bm.verts.layers.float_vector.new("tp_pinned_co")
                            bm.verts.ensure_lookup_table()
                            if active_idx < len(bm.verts):
                                v = bm.verts[active_idx]
                                new_pin_val = 0 if v[pin_layer] == 1 else 1
                                v[pin_layer] = new_pin_val
                                v[co_layer] = v.co.copy()
                                bmesh.update_edit_mesh(topo_obj.data)
                                update_pinned_coordinates(context)
                                self.report({'INFO'}, "已固定点" if new_pin_val == 1 else "已取消固定点")
                                try:
                                    bpy.ops.ed.undo_push(message="TP 双击固定/取消固定")
                                except Exception:
                                    pass
                            context.area.tag_redraw()
                            return {'RUNNING_MODAL'}
                        else:
                            # Record this click for potential double-click on next press
                            self._boundary_last_click_time = now
                            self._boundary_last_click_idx = active_idx
                        # --------------------------------------------------------------------------

                    if element_type is not None:
                        topo_obj = self.ensure_topo_obj_exists(context)
                        if topo_obj.mode != 'EDIT':
                            try:
                                bpy.ops.object.mode_set(mode='EDIT')
                            except Exception as e:
                                print("Failed to enter Edit Mode on drag start:", e)
                                
                        bm = bmesh.from_edit_mesh(topo_obj.data)
                        bm.verts.ensure_lookup_table()
                        bm.verts.index_update()
                        
                        selected_verts = [bm.verts[idx] for idx in target_indices if idx < len(bm.verts)]
                        
                        if selected_verts:
                            # 1. Back up BMesh with current selections intact
                            if hasattr(self, 'grab_backup_bm') and self.grab_backup_bm:
                                self.grab_backup_bm.free()
                            self.grab_backup_bm = bm.copy()
                            
                            # 2. Initialize grab state
                            self.rebuild_kd_tree()
                            self.is_grabbing = True
                            self.is_dragging_grab = True
                            self.grab_dragged = False
                            self.init_grab_influence(context, selected_verts)
                            self.grab_active_vert_idx = active_idx
                            self.grab_mouse_start = coord
                            
                            region = context.region
                            rv3d = context.space_data.region_3d
                            if active_idx < len(bm.verts):
                                active_v = bm.verts[active_idx]
                            else:
                                active_v = selected_verts[0] if selected_verts else None
                                if active_v:
                                    active_idx = active_v.index
                            
                            if active_v:
                                active_start_world = topo_obj.matrix_world @ active_v.co
                            else:
                                active_start_world = topo_obj.matrix_world @ mathutils.Vector((0, 0, 0))
                            self.last_valid_active_world = active_start_world.copy()
                            ray_origin_start = region_2d_to_origin_3d(region, rv3d, self.grab_mouse_start)
                            ray_vector_start = region_2d_to_vector_3d(region, rv3d, self.grab_mouse_start)
                            self.grab_initial_depth = (active_start_world - ray_origin_start).dot(ray_vector_start)
                            self.grab_snap_target_idx = None
                            self.hover_snap_pt = None
                            
                            context.workspace.status_text_set("TP拓扑移动边界 | 拖动鼠标: 调整位置 | 释放左键: 确定并吸附合并 | ESC/右键: 取消")
                            context.area.tag_redraw()
                            return {'RUNNING_MODAL'}
                    else:
                        if self.check_click_internal(context, coord):
                            return {'RUNNING_MODAL'}
                            
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.ctrl:
                if getattr(context.scene, "tp_square_mode", False):
                    coord = (event.mouse_region_x, event.mouse_region_y)
                    topo_obj = self.ensure_topo_obj_exists(context)
                    if topo_obj.mode != 'EDIT':
                        try:
                            bpy.ops.object.mode_set(mode='EDIT')
                        except Exception as e:
                            print("Failed to enter Edit Mode:", e)
                    if context.view_layer.objects.active != topo_obj:
                        context.view_layer.objects.active = topo_obj
                    if not topo_obj.select_get():
                        topo_obj.select_set(True)
                    
                    world_pt, world_normal = None, None
                    ref_obj_name = getattr(self, 'ref_object_name', '')
                    if not ref_obj_name:
                        ref_obj_name = context.window_manager.tp_ref_object_name
                    ref_obj = bpy.data.objects.get(ref_obj_name)
                    if ref_obj:
                        region = context.region
                        rv3d = context.space_data.region_3d
                        ray_origin = region_2d_to_origin_3d(region, rv3d, coord)
                        ray_vector = region_2d_to_vector_3d(region, rv3d, coord)
                        matrix_world = ref_obj.matrix_world
                        matrix_inverse = matrix_world.inverted()
                        ray_origin_local = matrix_inverse @ ray_origin
                        ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
                        depsgraph = context.evaluated_depsgraph_get()
                        try:
                            success, location, normal, face_idx = ref_obj.ray_cast(
                                ray_origin_local,
                                ray_vector_local,
                                depsgraph=depsgraph
                            )
                        except Exception:
                            try:
                                success, location, normal, face_idx = ref_obj.ray_cast(
                                    ray_origin_local,
                                    ray_vector_local
                                )
                            except Exception:
                                success = False
                        if success:
                            local_pt = location + normal * 0.0005
                            world_pt = matrix_world @ local_pt
                            world_normal = (matrix_world.to_3x3() @ normal).normalized()
                    
                    if world_pt and world_normal:
                        if is_point_in_mask(world_pt, context):
                            self.report({'WARNING'}, "无法在对称遮罩区内绘制")
                            return {'RUNNING_MODAL'}
                            
                        rv3d = context.space_data.region_3d
                        inv_view = rv3d.view_matrix.inverted()
                        camera_right = inv_view.to_3x3() @ mathutils.Vector((1, 0, 0))
                        
                        right = (camera_right - camera_right.dot(world_normal) * world_normal).normalized()
                        up = world_normal.cross(right).normalized()
                        
                        d = context.scene.tp_edge_length
                        
                        # Determine grid size N
                        square_count = getattr(context.scene, "tp_square_count", '1')
                        if square_count == '4':
                            N = 2
                        elif square_count == '16':
                            N = 4
                        elif square_count == '64':
                            N = 8
                        else:
                            N = 1
                        
                        ref_matrix_world = ref_obj.matrix_world
                        ref_matrix_inverse = ref_matrix_world.inverted()
                        
                        bm = bmesh.from_edit_mesh(topo_obj.data)
                        bm.verts.ensure_lookup_table()
                        
                        weld_threshold = d * 0.75
                        topo_inv = topo_obj.matrix_world.inverted()
                        
                        grid_verts = {}
                        for i in range(N + 1):
                            for j in range(N + 1):
                                offset_right = (i - N / 2.0) * d
                                offset_up = (j - N / 2.0) * d
                                p_world = world_pt + offset_right * right + offset_up * up
                                
                                # Project onto reference mesh surface
                                local_target = ref_matrix_inverse @ p_world
                                success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                                if success:
                                    local_pt = location + normal * 0.0005
                                    p_world = ref_matrix_world @ local_pt
                                
                                # Weld check with existing vertices
                                found_v = None
                                if self.kd_tree:
                                    nearest = self.kd_tree.find(p_world)
                                    if nearest:
                                        co, index, dist = nearest
                                        if dist < weld_threshold:
                                            bm.verts.ensure_lookup_table()
                                            found_v = bm.verts[index]
                                
                                if found_v is None:
                                    l_co = topo_inv @ p_world
                                    found_v = bm.verts.new(l_co)
                                    
                                grid_verts[(i, j)] = found_v
                        
                        # Assign a new unique loop ID so it gets recognized as a grid region
                        grid_layer = bm.faces.layers.int.get("tp_is_grid") or bm.faces.layers.int.new("tp_is_grid")
                        max_id = max([f[grid_layer] for f in bm.faces] + [0])
                        loop_id = max_id + 1

                        new_faces = []
                        for i in range(N):
                            for j in range(N):
                                v1 = grid_verts[(i, j)]
                                v2 = grid_verts[(i + 1, j)]
                                v3 = grid_verts[(i + 1, j + 1)]
                                v4 = grid_verts[(i, j + 1)]
                                verts_to_use = [v1, v2, v3, v4]
                                
                                # Remove duplicates while preserving order
                                verts_to_use_unique = []
                                for v in verts_to_use:
                                    if v not in verts_to_use_unique:
                                        verts_to_use_unique.append(v)
                                if len(verts_to_use_unique) < 3:
                                    continue
                                    
                                try:
                                    face = bm.faces.new(verts_to_use_unique)
                                    if grid_layer:
                                        face[grid_layer] = loop_id
                                    new_faces.append(face)
                                except ValueError:
                                    # Face already exists or invalid
                                    pass
                                    
                        if not new_faces:
                            self.report({'WARNING'}, "方形太小或太靠近现有顶点，无法生成")
                            return {'RUNNING_MODAL'}
                            
                        try:
                            # Apply remove doubles to clean up close vertices
                            vert_count_pre = len(bm.verts)
                            internal_verts = self.get_internal_grid_vert_indices(bm)
                            inner_ring_verts = self.get_ring_inner_vertices(bm)
                            weld_verts = [v for v in bm.verts if v.index not in internal_verts and v not in inner_ring_verts]
                            bmesh.ops.remove_doubles(bm, verts=weld_verts, dist=weld_threshold)
                            vert_count_post = len(bm.verts)
                            
                            # Re-index bmesh vertices so that index lookup is correct
                            bm.verts.index_update()
                            new_indices = {v.index for f in new_faces if f.is_valid for v in f.verts if v.is_valid}
                            
                            bmesh.update_edit_mesh(topo_obj.data)
                            
                            if new_indices:
                                self.conform_to_surface(context, active_indices=new_indices)
                            
                            self.rebuild_kd_tree()
                            
                            try:
                                bpy.ops.ed.undo_push(message="绘制方形拓扑")
                            except Exception as e:
                                print("Error pushing undo step:", e)
                                
                            self.report({'INFO'}, f"已在表面生成方形拓扑面 (共 {len(new_faces)} 个面)")
                        except Exception as e:
                            self.report({'WARNING'}, f"生成失败: {str(e)}")
                            
                        context.area.tag_redraw()
                    else:
                        self.report({'WARNING'}, "未点击到模型表面，无法生成方形")
                        
                    return {'RUNNING_MODAL'}
                elif getattr(context.scene, "tp_circle_mode", False):
                    import math
                    coord = (event.mouse_region_x, event.mouse_region_y)
                    topo_obj = self.ensure_topo_obj_exists(context)
                    if topo_obj.mode != 'EDIT':
                        try:
                            bpy.ops.object.mode_set(mode='EDIT')
                        except Exception as e:
                            print("Failed to enter Edit Mode:", e)
                    if context.view_layer.objects.active != topo_obj:
                        context.view_layer.objects.active = topo_obj
                    if not topo_obj.select_get():
                        topo_obj.select_set(True)
                    
                    world_pt, world_normal = None, None
                    ref_obj_name = getattr(self, 'ref_object_name', '')
                    if not ref_obj_name:
                        ref_obj_name = context.window_manager.tp_ref_object_name
                    ref_obj = bpy.data.objects.get(ref_obj_name)
                    if ref_obj:
                        region = context.region
                        rv3d = context.space_data.region_3d
                        ray_origin = region_2d_to_origin_3d(region, rv3d, coord)
                        ray_vector = region_2d_to_vector_3d(region, rv3d, coord)
                        matrix_world = ref_obj.matrix_world
                        matrix_inverse = matrix_world.inverted()
                        ray_origin_local = matrix_inverse @ ray_origin
                        ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
                        depsgraph = context.evaluated_depsgraph_get()
                        try:
                            success, location, normal, face_idx = ref_obj.ray_cast(
                                ray_origin_local,
                                ray_vector_local,
                                depsgraph=depsgraph
                            )
                        except Exception:
                            try:
                                success, location, normal, face_idx = ref_obj.ray_cast(
                                    ray_origin_local,
                                    ray_vector_local
                                )
                            except Exception:
                                success = False
                        if success:
                            local_pt = location + normal * 0.0005
                            world_pt = matrix_world @ local_pt
                            world_normal = (matrix_world.to_3x3() @ normal).normalized()
                    
                    if world_pt and world_normal:
                        if is_point_in_mask(world_pt, context):
                            self.report({'WARNING'}, "无法在对称遮罩区内绘制")
                            return {'RUNNING_MODAL'}
                            
                        rv3d = context.space_data.region_3d
                        inv_view = rv3d.view_matrix.inverted()
                        camera_right = inv_view.to_3x3() @ mathutils.Vector((1, 0, 0))
                        
                        right = (camera_right - camera_right.dot(world_normal) * world_normal).normalized()
                        up = world_normal.cross(right).normalized()
                        
                        d = context.scene.tp_edge_length
                        
                        ref_matrix_world = ref_obj.matrix_world
                        ref_matrix_inverse = ref_matrix_world.inverted()
                        
                        bm = bmesh.from_edit_mesh(topo_obj.data)
                        bm.verts.ensure_lookup_table()
                        
                        circle_type = getattr(context.scene, "tp_circle_count", '4')
                        try:
                            with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_log:
                                f_log.write(f"[circle_click] circle_type={circle_type}, edge_length={d}\n")
                        except:
                            pass
                        weld_threshold = d * 0.75
                        
                        topo_inv = topo_obj.matrix_world.inverted()
                        
                        # Generate the vertices
                        if circle_type == '8_ring':
                            # 16 vertices: 8 outer, 8 inner
                            pts_to_create = []
                            for k in range(8):
                                theta = k * math.pi / 4.0
                                p_world = world_pt + (d * math.cos(theta)) * right + (d * math.sin(theta)) * up
                                pts_to_create.append(p_world)
                            d_inner = d * 0.6
                            for k in range(8):
                                theta = k * math.pi / 4.0
                                p_world = world_pt + (d_inner * math.cos(theta)) * right + (d_inner * math.sin(theta)) * up
                                pts_to_create.append(p_world)
                        else:
                            # 9 vertices: center + 8 outer
                            pts_to_create = [world_pt]
                            for k in range(8):
                                theta = k * math.pi / 4.0
                                p_world = world_pt + (d * math.cos(theta)) * right + (d * math.sin(theta)) * up
                                pts_to_create.append(p_world)
                        
                        created_verts = []
                        for idx, p_w in enumerate(pts_to_create):
                            # Project onto reference mesh surface
                            local_target = ref_matrix_inverse @ p_w
                            success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                            if success:
                                local_pt = location + normal * 0.0005
                                p_w = ref_matrix_world @ local_pt
                            
                            # Weld check with existing vertices
                            found_v = None
                            # Prevent the inner vertices (idx >= 8) of the ring from welding to anything during creation
                            is_inner = (circle_type == '8_ring' and idx >= 8)
                            if not is_inner and self.kd_tree:
                                nearest = self.kd_tree.find(p_w)
                                if nearest:
                                    co, index, dist = nearest
                                    if dist < weld_threshold:
                                        bm.verts.ensure_lookup_table()
                                        found_v = bm.verts[index]
                            
                            if found_v is None:
                                l_co = topo_inv @ p_w
                                found_v = bm.verts.new(l_co)
                            
                            created_verts.append(found_v)
                        
                        # Assign a new unique loop ID so it gets recognized as a grid region
                        grid_layer = bm.faces.layers.int.get("tp_is_grid") or bm.faces.layers.int.new("tp_is_grid")
                        max_id = max([f[grid_layer] for f in bm.faces] + [0])
                        loop_id = max_id + 1

                        # Define the quads
                        if circle_type == '8_ring':
                            quads_indices = []
                            for k in range(8):
                                # Outer k, Outer (k+1)%8, Inner (k+1)%8, Inner k
                                quads_indices.append([k, (k + 1) % 8, ((k + 1) % 8) + 8, k + 8])
                        else:
                            quads_indices = [
                                [0, 1, 2, 3],  # C, V0, V1, V2
                                [0, 3, 4, 5],  # C, V2, V3, V4
                                [0, 5, 6, 7],  # C, V4, V5, V6
                                [0, 7, 8, 1],  # C, V6, V7, V0
                            ]
                        
                        new_faces = []
                        for q_indices in quads_indices:
                            verts_to_use = [created_verts[idx] for idx in q_indices]
                            # Remove duplicates while preserving order
                            verts_to_use_unique = []
                            for v in verts_to_use:
                                if v not in verts_to_use_unique:
                                    verts_to_use_unique.append(v)
                            if len(verts_to_use_unique) < 3:
                                continue
                                
                            try:
                                face = bm.faces.new(verts_to_use_unique)
                                if grid_layer:
                                    face[grid_layer] = loop_id
                                new_faces.append(face)
                            except ValueError as e:
                                try:
                                    with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_log:
                                        f_log.write(f"[face_create_error] vertices={[v.index for v in verts_to_use_unique]}, error={str(e)}\n")
                                except:
                                    pass
                                
                        if not new_faces:
                            self.report({'WARNING'}, "圆形太小或太靠近现有顶点，无法生成")
                            return {'RUNNING_MODAL'}
                            
                        try:
                            # Update indices first so get_internal_grid_vert_indices has valid indices
                            bm.verts.index_update()
                            vert_count_pre = len(bm.verts)
                            internal_verts = self.get_internal_grid_vert_indices(bm)
                            excluded_indices = set(internal_verts)
                            
                            # Exclude all newly created vertices and all inner ring vertices of existing rings to prevent collapse
                            new_verts_set = set(created_verts)
                            inner_ring_verts = self.get_ring_inner_vertices(bm)
                            weld_verts = [v for v in bm.verts if (v.index not in excluded_indices) and (v not in new_verts_set) and (v not in inner_ring_verts)]
                            
                            bmesh.ops.remove_doubles(bm, verts=weld_verts, dist=weld_threshold)
                            vert_count_post = len(bm.verts)
                            
                            # Re-index bmesh vertices so that index lookup is correct
                            bm.verts.index_update()
                            new_indices = {v.index for f in new_faces if f.is_valid for v in f.verts if v.is_valid}
                            
                            bmesh.update_edit_mesh(topo_obj.data)
                            
                            if new_indices:
                                self.conform_to_surface(context, active_indices=new_indices)
                            
                            self.rebuild_kd_tree()
                            
                            try:
                                bpy.ops.ed.undo_push(message="绘制圆形拓扑")
                            except Exception as e:
                                print("Error pushing undo step:", e)
                                
                            self.report({'INFO'}, f"已在表面生成圆形拓扑面 (共 {len(new_faces)} 个面)")
                        except Exception as e:
                            self.report({'WARNING'}, f"生成失败: {str(e)}")
                            
                        context.area.tag_redraw()
                    else:
                        self.report({'WARNING'}, "未点击到模型表面，无法生成圆形")
                        
                    return {'RUNNING_MODAL'}

                coord = (event.mouse_region_x, event.mouse_region_y)
                self.drag_start_coord = coord
                self.last_mouse_coord_prev = coord
                
                self.start_from_selected_v_co = None
                self.start_from_selected_v_idx = None
                
                topo_obj = self.ensure_topo_obj_exists(context)
                
                if topo_obj.mode != 'EDIT':
                    try:
                        bpy.ops.object.mode_set(mode='EDIT')
                    except Exception as e:
                        print("Failed to enter Edit Mode on drawing start:", e)
                
                if context.view_layer.objects.active != topo_obj:
                    context.view_layer.objects.active = topo_obj
                if not topo_obj.select_get():
                    topo_obj.select_set(True)
                    
                bm = bmesh.from_edit_mesh(topo_obj.data)
                bm.verts.ensure_lookup_table()
                selected_verts = [v for v in bm.verts if v.select]
                if selected_verts:
                    active_v = bm.select_history.active if (bm.select_history.active and isinstance(bm.select_history.active, bmesh.types.BMVert)) else None
                    if active_v and active_v.select:
                        start_v = active_v
                    else:
                        start_v = selected_verts[-1]
                    self.start_from_selected_v_co = topo_obj.matrix_world @ start_v.co.copy()
                    self.start_from_selected_v_idx = start_v.index

                self.rebuild_kd_tree()
                
                snap_pt, snap_v = self.find_nearest_vertex(context, coord, threshold_pixels=20)
                pt = snap_pt if snap_pt else self.get_surface_point(context, coord)
                if pt:
                    if is_point_in_mask(pt, context):
                        self.report({'WARNING'}, "无法在对称遮罩区内绘制")
                        return {'RUNNING_MODAL'}
                    self.is_drawing = True
                    self.is_dragging = True
                    self.is_polyline = False
                    self.stroke_points = [pt]
                    self.stroke_snap_indices = [snap_v]
                    self.max_drag_dist_from_start = 0.0
                    self.is_outside_drawing = False
                    context.area.tag_redraw()
                else:
                    self.is_drawing = True
                    self.is_dragging = True
                    self.is_polyline = False
                    self.stroke_points = []
                    self.stroke_snap_indices = []
                    self.max_drag_dist_from_start = 0.0
                    self.is_outside_drawing = True
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                if context.scene.tp_boundary_mode:
                    self.clear_internal_selections(context)
                self.conform_to_surface(context)
                self.rebuild_kd_tree()
                
            return {'PASS_THROUGH'}
            
        else:
            if self.is_polyline:
                if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.ctrl:
                    coord = (event.mouse_region_x, event.mouse_region_y)
                    snap_pt, snap_v = self.find_nearest_vertex(context, coord, threshold_pixels=20)
                    pt = snap_pt if snap_pt else self.get_surface_point(context, coord)
                    if pt:
                        if is_point_in_mask(pt, context):
                            clamped_pt = clamp_to_mask_boundary(pt, context)
                            if (clamped_pt - self.stroke_points[-1]).length > 0.001:
                                self.stroke_points.append(clamped_pt)
                                self.stroke_snap_indices.append(snap_v)
                            if len(self.stroke_points) >= 2:
                                self.create_geometry(context)
                                try:
                                    bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                                    self.report({'INFO'}, "触碰遮罩区并贴合边界，已自动完成绘制")
                                except Exception as e:
                                    print("Error pushing undo step:", e)
                            self.stroke_points = []
                            self.stroke_snap_indices = []
                            self.is_drawing = False
                            self.is_polyline = False
                            self.start_from_selected_v_co = None
                            self.start_from_selected_v_idx = None
                            context.area.tag_redraw()
                            return {'RUNNING_MODAL'}

                        if (pt - self.stroke_points[-1]).length > 0.001:
                            self.stroke_points.append(pt)
                            self.stroke_snap_indices.append(snap_v)
                            
                            # 检测新加入的点是否使圈线闭合
                            is_closed = False
                            if len(self.stroke_points) >= 3:
                                start_snap = self.stroke_snap_indices[0] if (self.stroke_snap_indices and len(self.stroke_snap_indices) > 0) else None
                                end_snap = self.stroke_snap_indices[-1] if (self.stroke_snap_indices and len(self.stroke_snap_indices) > 1) else None
                                if start_snap is not None and end_snap is not None:
                                    if start_snap == end_snap:
                                        is_closed = True
                                    else:
                                        # 检测在已有网格中是否存在连接这两个顶点的路径
                                        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
                                        if topo_obj and topo_obj.mode == 'EDIT':
                                            try:
                                                bm = bmesh.from_edit_mesh(topo_obj.data)
                                                bm.verts.ensure_lookup_table()
                                                if start_snap < len(bm.verts) and end_snap < len(bm.verts):
                                                    v_start = bm.verts[start_snap]
                                                    v_end = bm.verts[end_snap]
                                                    path = self.find_shortest_path_edges_in_bm(bm, v_start, v_end)
                                                    if path is not None:
                                                        is_closed = True
                                            except Exception as e:
                                                print("Error checking path in BM:", e)
                                                
                                if not is_closed:
                                    region = context.region
                                    rv3d = context.space_data.region_3d
                                    p0_2d = location_3d_to_region_2d(region, rv3d, self.stroke_points[0])
                                    pn_2d = location_3d_to_region_2d(region, rv3d, self.stroke_points[-1])
                                    if p0_2d and pn_2d:
                                        if (p0_2d - pn_2d).length < 20:
                                            is_closed = True
                                            
                            if is_closed:
                                if len(self.stroke_points) >= 2:
                                    self.create_geometry(context)
                                    try:
                                        bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                                    except Exception as e:
                                        print("Error pushing undo step:", e)
                                    self.report({'INFO'}, "已闭合圈线并自动完成绘制")
                                self.stroke_points = []
                                self.stroke_snap_indices = []
                                self.is_drawing = False
                                self.is_polyline = False
                                self.start_from_selected_v_co = None
                                self.start_from_selected_v_idx = None
                                self.max_drag_dist_from_start = 0.0
                                context.area.tag_redraw()
                            else:
                                context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                    
                return {'RUNNING_MODAL'}
            else:
                if event.type == 'MOUSEMOVE':
                    if self.is_dragging:
                        if getattr(self, 'is_outside_drawing', False):
                            self.last_mouse_coord = (event.mouse_region_x, event.mouse_region_y)
                            context.area.tag_redraw()
                            return {'RUNNING_MODAL'}
                        coord = (event.mouse_region_x, event.mouse_region_y)
                        
                        # 只要鼠标在点的吸附范围内，就直接吸附合并，并结束这一笔的绘制
                        snap_pt, snap_v = self.find_nearest_vertex(context, coord, threshold_pixels=20)
                        if snap_v is not None:
                            # 确定起始顶点
                            start_v_idx = None
                            if getattr(self, 'start_from_selected_v_idx', None) is not None:
                                start_v_idx = self.start_from_selected_v_idx
                            elif self.stroke_snap_indices and self.stroke_snap_indices[0] is not None:
                                start_v_idx = self.stroke_snap_indices[0]
                                
                            should_end = False
                            if start_v_idx is None:
                                # 从空白处起笔，吸附到任何已有顶点都立即合并结束
                                should_end = True
                            else:
                                if snap_v != start_v_idx:
                                    # 吸附到非起点的其他顶点，直接合并结束
                                    should_end = True
                                else:
                                    # 吸附回起点（闭合环线），需要已经拉开距离
                                    if self.stroke_points:
                                        region = context.region
                                        rv3d = context.space_data.region_3d
                                        p0_2d = location_3d_to_region_2d(region, rv3d, self.stroke_points[0])
                                        pn_2d = mathutils.Vector(coord)
                                        if p0_2d and pn_2d:
                                            dist_from_start = (pn_2d - p0_2d).length
                                            if dist_from_start > 30 or len(self.stroke_points) >= 3:
                                                should_end = True
                                                
                            if should_end:
                                # 追加当前吸附点和索引，立即完成绘制
                                self.stroke_points.append(snap_pt)
                                self.stroke_snap_indices.append(snap_v)
                                if len(self.stroke_points) >= 2:
                                    self.create_geometry(context)
                                    self.conform_to_surface(context)
                                    self.rebuild_kd_tree()
                                    try:
                                        bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                                    except Exception as e:
                                        print("Error pushing undo step:", e)
                                    self.report({'INFO'}, "已自动吸附合并并完成绘制")
                                
                                # 重置所有绘制状态
                                self.stroke_points = []
                                self.stroke_snap_indices = []
                                self.is_drawing = False
                                self.is_dragging = False
                                self.is_polyline = False
                                self.start_from_selected_v_co = None
                                self.start_from_selected_v_idx = None
                                self.max_drag_dist_from_start = 0.0
                                context.area.tag_redraw()
                                return {'RUNNING_MODAL'}
                        
                        dx = coord[0] - self.last_mouse_coord_prev[0]
                        dy = coord[1] - self.last_mouse_coord_prev[1]
                        if (dx*dx + dy*dy) ** 0.5 > 12:
                            snap_pt, snap_v = self.find_nearest_vertex(context, coord, threshold_pixels=20)
                            pt = snap_pt if snap_pt else self.get_surface_point(context, coord)
                            if pt:
                                if is_point_in_mask(pt, context):
                                    clamped_pt = clamp_to_mask_boundary(pt, context)
                                    if (clamped_pt - self.stroke_points[-1]).length > 0.001:
                                        self.stroke_points.append(clamped_pt)
                                        self.stroke_snap_indices.append(snap_v)
                                    if len(self.stroke_points) >= 2:
                                        self.create_geometry(context)
                                        self.conform_to_surface(context)
                                        self.rebuild_kd_tree()
                                        try:
                                            bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                                            self.report({'INFO'}, "触碰遮罩区并贴合边界，已自动完成绘制")
                                        except Exception as e:
                                            print("Error pushing undo step:", e)
                                    self.stroke_points = []
                                    self.stroke_snap_indices = []
                                    self.is_drawing = False
                                    self.is_dragging = False
                                    self.is_polyline = False
                                    self.start_from_selected_v_co = None
                                    self.start_from_selected_v_idx = None
                                    self.max_drag_dist_from_start = 0.0
                                    context.area.tag_redraw()
                                    return {'RUNNING_MODAL'}

                                if (pt - self.stroke_points[-1]).length > 0.001:
                                    self.stroke_points.append(pt)
                                    self.stroke_snap_indices.append(snap_v)
                                    self.last_mouse_coord_prev = coord
                                    
                                    # 在连续绘制（拖拽）过程中，检测是否首尾闭合
                                    region = context.region
                                    rv3d = context.space_data.region_3d
                                    p0_2d = location_3d_to_region_2d(region, rv3d, self.stroke_points[0])
                                    pn_2d = location_3d_to_region_2d(region, rv3d, pt)
                                    
                                    if p0_2d and pn_2d:
                                        # 计算新点到起点的距离，并更新最大拉伸距离
                                        dist_from_start = (pn_2d - p0_2d).length
                                        if dist_from_start > self.max_drag_dist_from_start:
                                            self.max_drag_dist_from_start = dist_from_start
                                            
                                        # 只有当路径曾经离开起点超过 30 像素，才进行闭合判定
                                        is_closed = False
                                        if len(self.stroke_points) >= 3 and self.max_drag_dist_from_start > 30:
                                            start_snap = self.stroke_snap_indices[0] if (self.stroke_snap_indices and len(self.stroke_snap_indices) > 0) else None
                                            end_snap = self.stroke_snap_indices[-1] if (self.stroke_snap_indices and len(self.stroke_snap_indices) > 1) else None
                                            if start_snap is not None and end_snap is not None:
                                                if start_snap == end_snap:
                                                    is_closed = True
                                                else:
                                                    # 检测在已有网格中是否存在连接这两个顶点的路径
                                                    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
                                                    if topo_obj and topo_obj.mode == 'EDIT':
                                                        try:
                                                            bm = bmesh.from_edit_mesh(topo_obj.data)
                                                            bm.verts.ensure_lookup_table()
                                                            if start_snap < len(bm.verts) and end_snap < len(bm.verts):
                                                                v_start = bm.verts[start_snap]
                                                                v_end = bm.verts[end_snap]
                                                                path = self.find_shortest_path_edges_in_bm(bm, v_start, v_end)
                                                                if path is not None:
                                                                    is_closed = True
                                                        except Exception as e:
                                                            print("Error checking path in BM:", e)
                                                            
                                            if not is_closed:
                                                if dist_from_start < 20:
                                                    is_closed = True
                                                    
                                        if is_closed:
                                            if len(self.stroke_points) >= 2:
                                                self.create_geometry(context)
                                                try:
                                                    bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                                                except Exception as e:
                                                    print("Error pushing undo step:", e)
                                                self.report({'INFO'}, "已闭合圈线并自动完成绘制")
                                            self.stroke_points = []
                                            self.stroke_snap_indices = []
                                            self.is_drawing = False
                                            self.is_dragging = False
                                            self.is_polyline = False
                                            self.start_from_selected_v_co = None
                                            self.start_from_selected_v_idx = None
                                            self.max_drag_dist_from_start = 0.0
                                            context.area.tag_redraw()
                                        else:
                                            context.area.tag_redraw()
                                    else:
                                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                        
                elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    coord = (event.mouse_region_x, event.mouse_region_y)
                    dx = coord[0] - self.drag_start_coord[0]
                    dy = coord[1] - self.drag_start_coord[1]
                    click_dist = (dx*dx + dy*dy) ** 0.5
                    
                    if getattr(self, 'is_outside_drawing', False):
                        self.is_dragging = False
                        self.is_drawing = False
                        self.is_outside_drawing = False
                        if click_dist >= 8:
                            self.create_outside_drag_geometry(context, self.drag_start_coord, coord)
                        self.stroke_points = []
                        self.stroke_snap_indices = []
                        self.is_polyline = False
                        self.start_from_selected_v_co = None
                        self.start_from_selected_v_idx = None
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                    
                    if click_dist < 8:
                        self.is_polyline = True
                        self.is_dragging = False
                        
                        if getattr(self, 'start_from_selected_v_co', None) is not None:
                            if self.stroke_points and (self.start_from_selected_v_idx != self.stroke_snap_indices[0] and (self.start_from_selected_v_co - self.stroke_points[0]).length > 0.001):
                                self.stroke_points.insert(0, self.start_from_selected_v_co)
                                self.stroke_snap_indices.insert(0, self.start_from_selected_v_idx)
                        
                        context.area.tag_redraw()
                    else:
                        self.is_dragging = False
                        if len(self.stroke_points) >= 2:
                            self.create_geometry(context)
                            try:
                                bpy.ops.ed.undo_push(message="TP 拓扑绘制")
                            except Exception as e:
                                print("Error pushing undo step:", e)
                        self.stroke_points = []
                        self.stroke_snap_indices = []
                        self.is_drawing = False
                        self.is_polyline = False
                        context.area.tag_redraw()
                        
                    self.start_from_selected_v_co = None
                    self.start_from_selected_v_idx = None
                    return {'RUNNING_MODAL'}
                    
                return {'RUNNING_MODAL'}

    def get_surface_point(self, context, mouse_coord):
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        if not ref_obj:
            return None
            
        region = context.region
        rv3d = context.space_data.region_3d
        
        ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_vector = region_2d_to_vector_3d(region, rv3d, mouse_coord)
        
        matrix_world = ref_obj.matrix_world
        matrix_inverse = matrix_world.inverted()
        
        ray_origin_local = matrix_inverse @ ray_origin
        ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
        
        depsgraph = context.evaluated_depsgraph_get()
        
        try:
            success, location, normal, face_idx = ref_obj.ray_cast(
                ray_origin_local,
                ray_vector_local,
                depsgraph=depsgraph
            )
        except Exception:
            try:
                success, location, normal, face_idx = ref_obj.ray_cast(
                    ray_origin_local,
                    ray_vector_local
                )
            except Exception:
                success = False
                
        if success:
            local_pt = location + normal * 0.0005
            world_pt = matrix_world @ local_pt
            return world_pt
            
        return None

    def get_back_surface_point(self, context, mouse_coord):
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        if not ref_obj:
            return None
            
        region = context.region
        rv3d = context.space_data.region_3d
        
        ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_vector = region_2d_to_vector_3d(region, rv3d, mouse_coord)
        
        matrix_world = ref_obj.matrix_world
        matrix_inverse = matrix_world.inverted()
        
        ray_origin_local = matrix_inverse @ ray_origin
        ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
        
        depsgraph = context.evaluated_depsgraph_get()
        far_dist = max(5.0, ref_obj.dimensions.length * 2.0)
        
        try:
            success_front, loc_front, norm_front, face_front = ref_obj.ray_cast(
                ray_origin_local,
                ray_vector_local,
                depsgraph=depsgraph
            )
        except Exception:
            try:
                success_front, loc_front, norm_front, face_front = ref_obj.ray_cast(
                    ray_origin_local,
                    ray_vector_local
                )
            except Exception:
                success_front = False
                
        if success_front:
            ray_origin_back = loc_front + ray_vector_local * far_dist
            try:
                success_back, loc_back, norm_back, face_back = ref_obj.ray_cast(
                    ray_origin_back,
                    -ray_vector_local,
                    depsgraph=depsgraph
                )
            except Exception:
                try:
                    success_back, loc_back, norm_back, face_back = ref_obj.ray_cast(
                        ray_origin_back,
                        -ray_vector_local
                    )
                except Exception:
                    success_back = False
                    
            if success_back:
                local_pt = loc_back + norm_back * 0.0005
                world_pt = matrix_world @ local_pt
                return world_pt
                
        return None

    def get_internal_grid_vert_indices(self, bm):
        grid_layer = bm.faces.layers.int.get("tp_is_grid")
        if not grid_layer:
            return set()
            
        loop_faces = {}
        for f in bm.faces:
            lid = f[grid_layer]
            if lid > 0:
                loop_faces.setdefault(lid, []).append(f)
                
        all_boundary_verts = set()
        all_grid_verts = set()
        
        for lid, faces in loop_faces.items():
            loop_edges = set()
            for f in faces:
                loop_edges.update(f.edges)
                
            boundary_edges = {e for e in loop_edges if sum(1 for f in e.link_faces if f[grid_layer] == lid) == 1}
            
            for e in boundary_edges:
                for v in e.verts:
                    all_boundary_verts.add(v.index)
                    
            for f in faces:
                for v in f.verts:
                    all_grid_verts.add(v.index)
                    
        internal_verts = all_grid_verts - all_boundary_verts
        return internal_verts

    def get_ring_inner_vertices(self, bm):
        grid_layer = bm.faces.layers.int.get("tp_is_grid")
        if not grid_layer:
            return set()
            
        loop_faces = {}
        for f in bm.faces:
            lid = f[grid_layer]
            if lid > 0:
                loop_faces.setdefault(lid, []).append(f)
                
        inner_verts = set()
        for lid, faces in loop_faces.items():
            loop_edges = set()
            for f in faces:
                loop_edges.update(f.edges)
            # Boundary edges are those shared by exactly 1 face of this loop
            boundary_edges = {e for e in loop_edges if sum(1 for f in e.link_faces if f[grid_layer] == lid) == 1}
            if not boundary_edges:
                continue
                
            vert_to_edges = {}
            for e in boundary_edges:
                for v in e.verts:
                    vert_to_edges.setdefault(v, []).append(e)
                    
            visited_edges = set()
            loops = []
            for e in boundary_edges:
                if e in visited_edges:
                    continue
                loop_verts = []
                curr_e = e
                curr_v = e.verts[0]
                start_v = curr_v
                loop_verts.append(curr_v)
                visited_edges.add(curr_e)
                
                while True:
                    next_v = curr_e.other_vert(curr_v)
                    if next_v == start_v:
                        break
                    loop_verts.append(next_v)
                    next_edges = [edge for edge in vert_to_edges.get(next_v, []) if edge != curr_e and edge not in visited_edges]
                    if not next_edges:
                        break
                    curr_e = next_edges[0]
                    curr_v = next_v
                    visited_edges.add(curr_e)
                if len(loop_verts) >= 3:
                    loops.append(loop_verts)
                    
            if len(loops) > 1:
                loop_radii = []
                for loop in loops:
                    center = mathutils.Vector((0.0, 0.0, 0.0))
                    for v in loop:
                        center += v.co
                    center /= len(loop)
                    avg_dist = sum((v.co - center).length for v in loop) / len(loop)
                    loop_radii.append((avg_dist, loop))
                loop_radii.sort(key=lambda x: x[0])
                for v in loop_radii[0][1]:
                    inner_verts.add(v)
                    
        return inner_verts

    def dist_to_face(self, pt, face):
        verts = [v.co for v in face.verts]
        if len(verts) < 3:
            return float('inf')
        
        min_dist = float('inf')
        v0 = verts[0]
        for i in range(1, len(verts) - 1):
            v1 = verts[i]
            v2 = verts[i+1]
            closest = mathutils.geometry.closest_point_on_tri(pt, v0, v1, v2)
            dist = (pt - closest).length
            if dist < min_dist:
                min_dist = dist
        return min_dist

    def find_closest_loop_id(self, context, stroke_points, bm, topo_obj):
        grid_layer = bm.faces.layers.int.get("tp_is_grid")
        if not grid_layer:
            return None
            
        points_to_check = stroke_points
        if len(stroke_points) > 2:
            points_to_check = stroke_points[1:-1]
            
        if not points_to_check:
            points_to_check = stroke_points
            
        topo_matrix = topo_obj.matrix_world
        
        face_centroids = []
        for f in bm.faces:
            lid = f[grid_layer]
            if lid > 0:
                centroid_world = topo_matrix @ f.calc_center_median()
                face_centroids.append((centroid_world, lid))
                
        if not face_centroids:
            return None
            
        from collections import Counter
        detected_lids = []
        edge_len = getattr(context.scene, "tp_edge_length", 0.5)
        threshold = max(edge_len * 3.0, 1.0)
        
        for pt in points_to_check:
            min_dist = float('inf')
            best_lid = None
            for centroid, lid in face_centroids:
                dist = (pt - centroid).length
                if dist < min_dist:
                    min_dist = dist
                    best_lid = lid
            if best_lid is not None and min_dist < threshold:
                detected_lids.append(best_lid)
                
        if not detected_lids:
            return None
            
        most_common = Counter(detected_lids).most_common(1)
        if most_common:
            return most_common[0][0]
        return None

    def rebuild_kd_tree(self):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj:
            self.kd_tree = None
            self.internal_grid_verts = set()
            return
            
        matrix_world = topo_obj.matrix_world
        self.internal_grid_verts = set()
        
        if topo_obj.mode == 'EDIT':
            try:
                bm = bmesh.from_edit_mesh(topo_obj.data)
                bm.verts.index_update()
                if not bm.verts:
                    self.kd_tree = None
                    return
                self.internal_grid_verts = self.get_internal_grid_vert_indices(bm)
                self.kd_tree = kdtree.KDTree(len(bm.verts))
                for v in bm.verts:
                    self.kd_tree.insert(matrix_world @ v.co, v.index)
                self.kd_tree.balance()
            except Exception as e:
                print("Error building KDTree in EDIT mode:", e)
                self.kd_tree = None
        else:
            if not topo_obj.data.vertices:
                self.kd_tree = None
                return
            try:
                bm_temp = bmesh.new()
                bm_temp.from_mesh(topo_obj.data)
                self.internal_grid_verts = self.get_internal_grid_vert_indices(bm_temp)
                bm_temp.free()
            except Exception as e:
                print("Error getting internal grid verts in object mode:", e)
            self.kd_tree = kdtree.KDTree(len(topo_obj.data.vertices))
            for i, v in enumerate(topo_obj.data.vertices):
                self.kd_tree.insert(matrix_world @ v.co, i)
            self.kd_tree.balance()

    def find_nearest_vertex(self, context, mouse_coord, threshold_pixels=20, exclude_internal=True):
        if not self.kd_tree:
            return None, None
            
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj:
            return None, None
            
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        if not ref_obj:
            return None, None
            
        region = context.region
        rv3d = context.space_data.region_3d
        mouse_vec = mathutils.Vector(mouse_coord)
        
        ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_vector = region_2d_to_vector_3d(region, rv3d, mouse_coord)
        
        matrix_world = ref_obj.matrix_world
        matrix_inverse = matrix_world.inverted()
        ray_origin_local = matrix_inverse @ ray_origin
        ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
        
        success = False
        location = None
        try:
            depsgraph = context.evaluated_depsgraph_get()
            success, location, normal, face_idx = ref_obj.ray_cast(
                ray_origin_local,
                ray_vector_local,
                depsgraph=depsgraph
            )
        except Exception:
            try:
                success, location, normal, face_idx = ref_obj.ray_cast(
                    ray_origin_local,
                    ray_vector_local
                )
            except Exception:
                pass
                
        if success and location is not None:
            world_pt = matrix_world @ location
            depth = (world_pt - ray_origin).length
        else:
            depth = (ref_obj.location - ray_origin).length
            
        search_center = ray_origin + ray_vector * depth
        target_pt = world_pt if (success and location is not None) else search_center
        
        try:
            nearest = self.kd_tree.find_n(search_center, 50)
        except Exception:
            return None, None
            
        if not nearest:
            return None, None
            
        nearest_v_idx = None
        min_dist_px = threshold_pixels
        nearest_world_pt = None
        
        exclude_indices = getattr(self, 'internal_grid_verts', set()) if (context.scene.tp_boundary_mode and exclude_internal) else set()
        
        for co, index, dist in nearest:
            if index in exclude_indices:
                continue
            screen_coord = location_3d_to_region_2d(region, rv3d, co)
            if screen_coord:
                dist_px = (screen_coord - mouse_vec).length
                if dist_px < min_dist_px:
                    # Prevent snapping to vertices on the opposite side of the reference mesh in boundary mode
                    if context.scene.tp_boundary_mode and self.is_point_occluded(context, ref_obj, co, region, rv3d):
                        continue
                    if not self.check_line_crosses_ref_mesh(ref_obj, co, target_pt):
                        min_dist_px = dist_px
                        nearest_v_idx = index
                        nearest_world_pt = co
                    
        if nearest_v_idx is not None:
            return nearest_world_pt, nearest_v_idx
        return None, None

    def find_nearest_boundary_element(self, context, mouse_coord, threshold_pixels=20):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj or topo_obj.type != 'MESH':
            return None, [], None
            
        is_edit = (topo_obj.mode == 'EDIT')
        import bmesh
        
        if is_edit:
            bm = bmesh.from_edit_mesh(topo_obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(topo_obj.data)
            
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.verts.index_update()
        
        from .draw_utils import get_seam_target_edges_local
        try:
            boundary_edges = get_seam_target_edges_local(bm)
        except Exception as e:
            print("Error in get_seam_target_edges_local:", e)
            boundary_edges = set()
            
        boundary_verts = set()
        for e in boundary_edges:
            boundary_verts.update(e.verts)
            
        region = context.region
        rv3d = context.space_data.region_3d
        mouse_vec = mathutils.Vector(mouse_coord)
        
        min_v_dist = float('inf')
        best_v = None
        
        for v in boundary_verts:
            screen_coord = location_3d_to_region_2d(region, rv3d, topo_obj.matrix_world @ v.co)
            if screen_coord:
                dist = (screen_coord - mouse_vec).length
                if dist < min_v_dist:
                    min_v_dist = dist
                    best_v = v
                    
        if not is_edit:
            bm.free()
            
        if min_v_dist <= threshold_pixels and best_v is not None:
            return 'VERT', [best_v.index], best_v.index
            
        return None, [], None

    def check_click_internal(self, context, mouse_coord):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj or topo_obj.type != 'MESH':
            return False
            
        import bmesh
        from mathutils.bvhtree import BVHTree
        
        is_edit = (topo_obj.mode == 'EDIT')
        if is_edit:
            bm = bmesh.from_edit_mesh(topo_obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(topo_obj.data)
            
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        
        from .draw_utils import get_seam_target_edges_local
        try:
            boundary_edges = get_seam_target_edges_local(bm)
        except Exception as e:
            print("Error in get_seam_target_edges_local:", e)
            boundary_edges = set()
            
        boundary_verts = {v for e in boundary_edges for v in e.verts}
        
        region = context.region
        rv3d = context.space_data.region_3d
        mouse_vec = mathutils.Vector(mouse_coord)
        matrix_world = topo_obj.matrix_world
        
        threshold_pixels = 20
        
        # 1. Check if the click is close to any boundary vertex
        for v in boundary_verts:
            screen_coord = location_3d_to_region_2d(region, rv3d, matrix_world @ v.co)
            if screen_coord:
                if (screen_coord - mouse_vec).length < threshold_pixels:
                    if not is_edit:
                        bm.free()
                    return False
                    
        # 2. Check if the click is close to any boundary edge
        for e in boundary_edges:
            p1 = location_3d_to_region_2d(region, rv3d, matrix_world @ e.verts[0].co)
            p2 = location_3d_to_region_2d(region, rv3d, matrix_world @ e.verts[1].co)
            if p1 and p2:
                v = p2 - p1
                w = mouse_vec - p1
                c1 = w.dot(v)
                if c1 <= 0:
                    dist = w.length
                else:
                    c2 = v.dot(v)
                    if c2 <= c1:
                        dist = (mouse_vec - p2).length
                    else:
                        b = c1 / c2
                        pb = p1 + b * v
                        dist = (mouse_vec - pb).length
                if dist < threshold_pixels:
                    if not is_edit:
                        bm.free()
                    return False
                    
        # 3. Check if click is close to ANY vertex (which must be internal)
        nearest_pt, nearest_idx = self.find_nearest_vertex(context, mouse_coord, threshold_pixels=threshold_pixels, exclude_internal=False)
        if nearest_idx is not None:
            if not is_edit:
                bm.free()
            return True
            
        # 4. Check if click is close to ANY edge (which must be internal)
        nearest_edge = self.find_nearest_edge(context, mouse_coord, threshold_pixels=threshold_pixels, exclude_internal=False)
        if nearest_edge is not None:
            if not is_edit:
                bm.free()
            return True
            
        # 5. Check if raycast hits any face (which must be internal)
        ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_vector = region_2d_to_vector_3d(region, rv3d, mouse_coord)
        matrix_inverse = matrix_world.inverted()
        ray_origin_local = matrix_inverse @ ray_origin
        ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
        
        bvh = BVHTree.FromBMesh(bm)
        hit_loc, hit_normal, face_index, hit_dist = bvh.ray_cast(ray_origin_local, ray_vector_local)
        
        if not is_edit:
            bm.free()
            
        if hit_loc is not None:
            return True
            
        return False

    def clear_internal_selections(self, context):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj or topo_obj.type != 'MESH' or topo_obj.mode != 'EDIT':
            return
            
        import bmesh
        try:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            from .draw_utils import get_seam_target_edges_local
            boundary_edges = get_seam_target_edges_local(bm)
            boundary_verts = {v for e in boundary_edges for v in e.verts}
            
            changed = False
            for v in bm.verts:
                if v.select and v not in boundary_verts:
                    v.select = False
                    changed = True
            for e in bm.edges:
                if e.select and e not in boundary_edges:
                    e.select = False
                    changed = True
            for f in bm.faces:
                if f.select:
                    f.select = False
                    changed = True
                    
            if changed:
                bmesh.update_edit_mesh(topo_obj.data)
                context.area.tag_redraw()
        except Exception as e:
            print("Error clearing internal selections:", e)

    def get_boundary_paths_and_loops(self, bm, boundary_edges):
        # Build adjacency list
        adj = {}
        for e in boundary_edges:
            v1, v2 = e.verts[0].index, e.verts[1].index
            adj.setdefault(v1, set()).add(v2)
            adj.setdefault(v2, set()).add(v1)
            
        visited = set()
        paths_and_loops = []
        
        # 1. First find open paths (start at vertices with degree 1)
        for v_idx, neighbors in adj.items():
            if len(neighbors) == 1 and v_idx not in visited:
                path = [v_idx]
                visited.add(v_idx)
                curr = v_idx
                while True:
                    next_v = None
                    for n in adj[curr]:
                        if n not in visited:
                            next_v = n
                            break
                    if next_v is None:
                        break
                    path.append(next_v)
                    visited.add(next_v)
                    curr = next_v
                paths_and_loops.append({'closed': False, 'verts': path})
                
        # 2. Then find closed loops (remaining vertices)
        for v_idx in adj.keys():
            if v_idx not in visited:
                loop = [v_idx]
                visited.add(v_idx)
                curr = v_idx
                while True:
                    next_v = None
                    for n in adj[curr]:
                        if n not in visited:
                            next_v = n
                            break
                    if next_v is None:
                        break
                    loop.append(next_v)
                    visited.add(next_v)
                    curr = next_v
                # Check if closed (there is an edge between first and last)
                if loop[-1] in adj.get(loop[0], set()):
                    paths_and_loops.append({'closed': True, 'verts': loop})
                else:
                    paths_and_loops.append({'closed': False, 'verts': loop})
                    
        return paths_and_loops

    def init_grab_influence(self, context, selected_verts):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        self.grab_initial_cos = {}
        self.grab_weights = {}
        
        if not topo_obj or not selected_verts:
            return
            
        import bmesh
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        
        # Default behavior: only move selected verts with weight 1.0
        for v in selected_verts:
            self.grab_initial_cos[v.index] = v.co.copy()
            self.grab_weights[v.index] = 1.0
            
        # If tp_boundary_mode is enabled, calculate proportional influence along boundary
        if context.scene.tp_boundary_mode:
            # Check if any selected vert is on the boundary
            from .draw_utils import get_seam_target_edges_local
            try:
                boundary_edges = get_seam_target_edges_local(bm)
            except Exception as e:
                print("Error in get_seam_target_edges_local:", e)
                boundary_edges = set()
                
            boundary_verts = set()
            for e in boundary_edges:
                boundary_verts.update(e.verts)
                
            boundary_vert_indices = {bv.index for bv in boundary_verts}
            selected_boundary_indices = [v.index for v in selected_verts if v.index in boundary_vert_indices]
            
            if selected_boundary_indices:
                pin_layer = bm.verts.layers.int.get("tp_is_pinned")
                
                # Influence radius in steps for boundary dragging
                max_r = 5
                
                # Build adjacency list for boundary graph
                boundary_adj = {}
                for e in boundary_edges:
                    v1_idx = e.verts[0].index
                    v2_idx = e.verts[1].index
                    boundary_adj.setdefault(v1_idx, []).append(v2_idx)
                    boundary_adj.setdefault(v2_idx, []).append(v1_idx)
                
                # BFS queue and distances dict
                import collections
                queue = collections.deque()
                distances = {}
                
                # Initialize BFS with selected boundary vertices
                for idx in selected_boundary_indices:
                    distances[idx] = 0
                    queue.append(idx)
                    
                while queue:
                    curr_idx = queue.popleft()
                    curr_dist = distances[curr_idx]
                    
                    if curr_dist >= max_r:
                        continue
                        
                    # Traverse neighbors in the boundary graph
                    for nbr_idx in boundary_adj.get(curr_idx, []):
                        if nbr_idx not in distances:
                            distances[nbr_idx] = curr_dist + 1
                            
                            # If the neighbor is pinned, do not propagate past it
                            nbr_v = bm.verts[nbr_idx]
                            is_pinned = pin_layer and (nbr_v[pin_layer] == 1)
                            if not is_pinned:
                                queue.append(nbr_idx)
                                
                # Calculate weights for all reached vertices using custom step weights
                weight_lut = {1: 0.70, 2: 0.40, 3: 0.20, 4: 0.10}
                for v_idx, dist in distances.items():
                    if dist == 0:
                        continue  # Selected vertices already have weight 1.0 set
                    weight = weight_lut.get(dist, 0.0)
                    if weight > 0.0:
                        if v_idx not in self.grab_weights or weight > self.grab_weights[v_idx]:
                            self.grab_weights[v_idx] = weight
                            self.grab_initial_cos[v_idx] = bm.verts[v_idx].co.copy()



    def find_cycles_through_vertex(self, v_start, max_cycles=15, max_len=150, grid_layer=None):
        import collections
        neighbors = []
        # 获取所有符合边界条件的直连邻居（包括拼接处的共享边界）
        for e in v_start.link_edges:
            if is_boundary_edge(e, grid_layer):
                neighbors.append(e.other_vert(v_start))
                
        cycles = []
        seen_cycles = set()
        
        # 寻找在排除 v_start 后，任意两个邻居之间的最短路径
        # 这能保证我们在 branching 严重的网络中瞬时定位到通过 v_start 的闭合圈
        for i in range(len(neighbors)):
            for j in range(i + 1, len(neighbors)):
                w1 = neighbors[i]
                w2 = neighbors[j]
                
                queue = collections.deque([[w1]])
                visited = {v_start.index, w1.index}
                found_path = None
                
                while queue:
                    path = queue.popleft()
                    curr = path[-1]
                    
                    if curr.index == w2.index:
                        found_path = path
                        break
                        
                    if len(path) >= max_len:
                        continue
                        
                    for e in curr.link_edges:
                        if is_boundary_edge(e, grid_layer):
                            nbr = e.other_vert(curr)
                            if nbr.index not in visited:
                                visited.add(nbr.index)
                                queue.append(path + [nbr])
                                
                if found_path:
                    # 组合成闭合圈：v_start -> w1 -> ... -> w2 -> v_start
                    cycle = [v_start] + found_path
                    cycle_indices = [v.index for v in cycle]
                    canonical = tuple(sorted(cycle_indices))
                    if canonical not in seen_cycles:
                        seen_cycles.add(canonical)
                        cycles.append(cycle_indices)
                        if len(cycles) >= max_cycles:
                            break
            if len(cycles) >= max_cycles:
                break
                
        cycles.sort(key=lambda c: len(c))
        return cycles

    def find_nearest_edge(self, context, mouse_coord, threshold_pixels=20, exclude_internal=True):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj or topo_obj.mode != 'EDIT':
            return None
            
        bm = bmesh.from_edit_mesh(topo_obj.data)
        matrix_world = topo_obj.matrix_world
        region = context.region
        rv3d = context.space_data.region_3d
        p_mouse = mathutils.Vector(mouse_coord)
        
        boundary_edges = None
        if exclude_internal and getattr(context.scene, "tp_boundary_mode", False):
            from .draw_utils import get_seam_target_edges_local
            try:
                boundary_edges = get_seam_target_edges_local(bm)
            except Exception as e:
                print("Error getting boundary edges in find_nearest_edge:", e)
                
        nearest_edge = None
        min_dist = threshold_pixels
        
        for e in bm.edges:
            if boundary_edges is not None and e not in boundary_edges:
                continue
            p1 = location_3d_to_region_2d(region, rv3d, matrix_world @ e.verts[0].co)
            p2 = location_3d_to_region_2d(region, rv3d, matrix_world @ e.verts[1].co)
            if p1 and p2:
                v = p2 - p1
                w = p_mouse - p1
                c1 = w.dot(v)
                if c1 <= 0:
                    dist = w.length
                else:
                    c2 = v.dot(v)
                    if c2 <= c1:
                        dist = (p_mouse - p2).length
                    else:
                        b = c1 / c2
                        pb = p1 + b * v
                        dist = (p_mouse - pb).length
                
                if dist < min_dist:
                    min_dist = dist
                    nearest_edge = e
                    
        return nearest_edge

    def find_edge_chain(self, edge, grid_layer=None):
        chain_edges = {edge}
        
        curr_v = edge.verts[0]
        prev_e = edge
        while True:
            valid_edges = [e for e in curr_v.link_edges if is_boundary_edge(e, grid_layer)]
            if len(valid_edges) != 2:
                break
            next_e = valid_edges[0] if valid_edges[0] != prev_e else valid_edges[1]
            if next_e in chain_edges:
                break
            chain_edges.add(next_e)
            curr_v = next_e.other_vert(curr_v)
            prev_e = next_e
            
        curr_v = edge.verts[1]
        prev_e = edge
        while True:
            valid_edges = [e for e in curr_v.link_edges if is_boundary_edge(e, grid_layer)]
            if len(valid_edges) != 2:
                break
            next_e = valid_edges[0] if valid_edges[0] != prev_e else valid_edges[1]
            if next_e in chain_edges:
                break
            chain_edges.add(next_e)
            curr_v = next_e.other_vert(curr_v)
            prev_e = next_e
            
        return chain_edges

    def perform_loop_selection(self, context, mouse_coord, shift_pressed, alt_pressed=False):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if topo_obj and topo_obj.mode == 'EDIT':
            try:
                bm_weld = bmesh.from_edit_mesh(topo_obj.data)
                weld_dist = 0.001  # Safe threshold to remove exact duplicate vertices on select
                bmesh.ops.remove_doubles(bm_weld, verts=bm_weld.verts, dist=weld_dist)
                bmesh.update_edit_mesh(topo_obj.data)
            except Exception as e:
                print("Error welding vertices on Alt+Click:", e)
                
        self.rebuild_kd_tree()
        nearest_world_pt, nearest_v_idx = self.find_nearest_vertex(context, mouse_coord, threshold_pixels=20)
        
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        
        if topo_obj and topo_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            
            # 获取栅格层信息
            grid_layer = bm.faces.layers.int.get("tp_is_grid")
            
            if nearest_v_idx is not None and nearest_v_idx < len(bm.verts):
                v_target = bm.verts[nearest_v_idx]
                
                is_boundary_vert = False
                if context.scene.tp_boundary_mode:
                    from .draw_utils import get_seam_target_edges_local
                    try:
                        boundary_edges = get_seam_target_edges_local(bm)
                        is_boundary_vert = any(v_target in e.verts for e in boundary_edges)
                    except Exception as e:
                        print("Error checking boundary vert in loop selection:", e)

                if is_boundary_vert and not alt_pressed:
                    if shift_pressed:
                        v_target.select = not v_target.select
                        if v_target.select:
                            bm.select_history.clear()
                            bm.select_history.add(v_target)
                    else:
                        for v in bm.verts:
                            v.select = False
                        for e in bm.edges:
                            e.select = False
                        v_target.select = True
                        bm.select_history.clear()
                        bm.select_history.add(v_target)
                    
                    bmesh.update_edit_mesh(topo_obj.data)
                    self.report({'INFO'}, "已选中边界点")
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                
                cycles = self.find_cycles_through_vertex(v_target, grid_layer=grid_layer)
                
                if cycles:
                    cycles.sort(key=lambda c: len(c))
                    
                    last_clicked_vert = getattr(self, 'last_clicked_vert_idx', None)
                    last_clicked_cycles_list = getattr(self, 'last_clicked_cycles', None)
                    
                    is_toggle = (last_clicked_vert == v_target.index and last_clicked_cycles_list == cycles)
                    
                    if is_toggle:
                        prev_cycle = cycles[self.last_clicked_cycle_idx]
                        self.last_clicked_cycle_idx = (self.last_clicked_cycle_idx + 1) % len(cycles)
                    else:
                        prev_cycle = None
                        self.last_clicked_vert_idx = v_target.index
                        self.last_clicked_cycles = cycles
                        self.last_clicked_cycle_idx = 0
                        
                    selected_cycle = cycles[self.last_clicked_cycle_idx]
                    
                    if shift_pressed:
                        if is_toggle and prev_cycle:
                            for idx in prev_cycle:
                                if idx < len(bm.verts):
                                    bm.verts[idx].select = False
                            for i in range(len(prev_cycle)):
                                idx1 = prev_cycle[i]
                                idx2 = prev_cycle[(i + 1) % len(prev_cycle)]
                                if idx1 < len(bm.verts) and idx2 < len(bm.verts):
                                    v1 = bm.verts[idx1]
                                    v2 = bm.verts[idx2]
                                    edge = bm.edges.get((v1, v2))
                                    if edge:
                                        edge.select = False
                    else:
                        for v in bm.verts:
                            v.select = False
                        for e in bm.edges:
                            e.select = False
                            
                    for idx in selected_cycle:
                        if idx < len(bm.verts):
                            bm.verts[idx].select = True
                        
                    for i in range(len(selected_cycle)):
                        idx1 = selected_cycle[i]
                        idx2 = selected_cycle[(i + 1) % len(selected_cycle)]
                        if idx1 < len(bm.verts) and idx2 < len(bm.verts):
                            v1 = bm.verts[idx1]
                            v2 = bm.verts[idx2]
                            edge = bm.edges.get((v1, v2))
                            if edge:
                                edge.select = True
                            
                    bmesh.update_edit_mesh(topo_obj.data)
                    self.report({'INFO'}, f"已选中圈 ({self.last_clicked_cycle_idx + 1}/{len(cycles)})")
                else:
                    # 顶点环搜索失败，退回到边线选择作为退路
                    nearest_edge = self.find_nearest_edge(context, mouse_coord, threshold_pixels=20)
                    if nearest_edge:
                        success_native = False
                        if len(nearest_edge.link_faces) >= 1:
                            try:
                                if not shift_pressed:
                                    for v in bm.verts:
                                        v.select = False
                                    for e in bm.edges:
                                        e.select = False
                                
                                # 确保边被选中，并被设置为选择历史中的活动项
                                nearest_edge.select = True
                                bm.select_history.clear()
                                bm.select_history.add(nearest_edge)
                                bmesh.update_edit_mesh(topo_obj.data)
                                
                                selected_count_pre_op = sum(1 for e in bm.edges if e.select)
                                
                                # 确保拓扑对象为当前视图层的活动对象以调用此编辑模式操作符
                                context.view_layer.objects.active = topo_obj
                                bpy.ops.mesh.loop_select(extend=shift_pressed)
                                
                                # 重新加载网格数据并检查选择数量是否发生改变
                                bm = bmesh.from_edit_mesh(topo_obj.data)
                                bm.verts.ensure_lookup_table()
                                bm.edges.ensure_lookup_table()
                                
                                selected_count_post_op = sum(1 for e in bm.edges if e.select)
                                if selected_count_post_op > selected_count_pre_op:
                                    success_native = True
                                else:
                                    success_native = False
                            except Exception as e:
                                print("Native loop select failed, falling back:", e)
                                success_native = False
                                
                        if not success_native:
                            grid_layer = bm.faces.layers.int.get("tp_is_grid")
                            chain_edges = self.find_edge_chain(nearest_edge, grid_layer=grid_layer)
                            
                            if not shift_pressed:
                                for v in bm.verts:
                                    v.select = False
                                for e in bm.edges:
                                    e.select = False
                                    
                            for e in chain_edges:
                                e.select = True
                                e.verts[0].select = True
                                e.verts[1].select = True
                                
                            bmesh.update_edit_mesh(topo_obj.data)
                        self.report({'INFO'}, "已通过邻近边选中循环线")
                    else:
                        self.report({'INFO'}, "该点不属于任何闭合圈")
                    
            else:
                nearest_edge = self.find_nearest_edge(context, mouse_coord, threshold_pixels=20)
                if nearest_edge:
                    if context.scene.tp_boundary_mode and not alt_pressed:
                        # In boundary mode, without Alt, clicking an edge selects just that edge
                        if shift_pressed:
                            # Toggle selection of this edge
                            nearest_edge.select = not nearest_edge.select
                            if nearest_edge.select:
                                nearest_edge.verts[0].select = True
                                nearest_edge.verts[1].select = True
                            else:
                                # Deselect vertices if they are not connected to any other selected edge
                                for v in nearest_edge.verts:
                                    if not any(e.select for e in v.link_edges if e != nearest_edge):
                                        v.select = False
                        else:
                            for v in bm.verts:
                                v.select = False
                            for e in bm.edges:
                                e.select = False
                            nearest_edge.select = True
                            nearest_edge.verts[0].select = True
                            nearest_edge.verts[1].select = True
                            bm.select_history.clear()
                            bm.select_history.add(nearest_edge)
                        
                        bmesh.update_edit_mesh(topo_obj.data)
                        self.report({'INFO'}, "已加选边界边" if shift_pressed else "已选中边界边")
                    else:
                        success_native = False
                        if len(nearest_edge.link_faces) >= 1:
                            try:
                                if not shift_pressed:
                                    for v in bm.verts:
                                        v.select = False
                                    for e in bm.edges:
                                        e.select = False
                                
                                # 确保边被选中，并被设置为选择历史中的活动项
                                nearest_edge.select = True
                                bm.select_history.clear()
                                bm.select_history.add(nearest_edge)
                                bmesh.update_edit_mesh(topo_obj.data)
                                
                                selected_count_pre_op = sum(1 for e in bm.edges if e.select)
                                
                                # 确保拓扑对象为当前视图层的活动对象以调用此编辑模式操作符
                                context.view_layer.objects.active = topo_obj
                                bpy.ops.mesh.loop_select(extend=shift_pressed)
                                
                                # 重新加载网格数据并检查选择数量是否发生改变
                                bm = bmesh.from_edit_mesh(topo_obj.data)
                                bm.verts.ensure_lookup_table()
                                bm.edges.ensure_lookup_table()
                                
                                selected_count_post_op = sum(1 for e in bm.edges if e.select)
                                if selected_count_post_op > selected_count_pre_op:
                                    success_native = True
                                else:
                                    success_native = False
                            except Exception as e:
                                print("Native loop select failed, falling back:", e)
                                success_native = False
                                
                        if not success_native:
                            grid_layer = bm.faces.layers.int.get("tp_is_grid")
                            chain_edges = self.find_edge_chain(nearest_edge, grid_layer=grid_layer)
                            
                            if not shift_pressed:
                                for v in bm.verts:
                                    v.select = False
                                for e in bm.edges:
                                    e.select = False
                                    
                            for e in chain_edges:
                                e.select = True
                                e.verts[0].select = True
                                e.verts[1].select = True
                                
                            bmesh.update_edit_mesh(topo_obj.data)
                            
                        self.report({'INFO'}, "已选中循环边")
                else:
                    self.report({'INFO'}, "未检测到附近的点或边")
                    
            if context.scene.tp_boundary_mode:
                self.clear_internal_selections(context)
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def handle_smooth_modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self.last_mouse_coord = (event.mouse_region_x, event.mouse_region_y)
            if not getattr(self, 'smooth_dragged', False):
                dx = event.mouse_region_x - self.smooth_mouse_start[0]
                dy = event.mouse_region_y - self.smooth_mouse_start[1]
                if (dx * dx + dy * dy) > 25:  # 5 pixels threshold
                    self.smooth_dragged = True
            
            self.smooth_boundary_verts_in_brush(context, self.last_mouse_coord)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self.is_smoothing = False
            context.workspace.status_text_set(None)
            
            if not getattr(self, 'smooth_dragged', False):
                self.perform_loop_selection(context, self.smooth_mouse_start, event.shift)
            else:
                self.conform_to_surface(context)
                self.rebuild_kd_tree()
                # On success release, update the grids using the final conformed positions of smoothed boundary vertices
                if context.scene.tp_boundary_mode and getattr(self, 'smoothed_vert_indices', None):
                    try:
                        from .op_grid_fill import update_grids_for_vertices
                        update_grids_for_vertices(context, list(self.smoothed_vert_indices))
                    except Exception as e:
                        print("Error updating grids on smooth release:", e)
                try:
                    bpy.ops.ed.undo_push(message="TP 边界平滑")
                except Exception as e:
                    print("Error pushing undo step for smoothing:", e)
                    
            if getattr(self, 'smooth_backup_bm', None):
                self.smooth_backup_bm.free()
                self.smooth_backup_bm = None
            self.smooth_initial_cos = None
            self.smoothed_vert_indices = None
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        elif event.type in {'ESC', 'RIGHTMOUSE'}:
            topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
            if topo_obj and topo_obj.mode == 'EDIT':
                if getattr(self, 'smooth_backup_bm', None):
                    temp_mesh = bpy.data.meshes.new("_tp_smooth_restore_tmp")
                    try:
                        self.smooth_backup_bm.to_mesh(temp_mesh)
                        bm_restore = bmesh.from_edit_mesh(topo_obj.data)
                        bm_restore.clear()
                        bm_restore.from_mesh(temp_mesh)
                        bmesh.update_edit_mesh(topo_obj.data)
                    finally:
                        bpy.data.meshes.remove(temp_mesh)
                        self.smooth_backup_bm.free()
                        self.smooth_backup_bm = None
            self.is_smoothing = False
            self.smooth_initial_cos = None
            self.smoothed_vert_indices = None
            context.workspace.status_text_set(None)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}
            
        return {'RUNNING_MODAL'}

    def smooth_boundary_verts_in_brush(self, context, mouse_coord):
        radius = getattr(self, 'smooth_brush_radius', 50.0)
        strength = getattr(context.scene, "tp_smooth_strength", 0.2)
        
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if not topo_obj or topo_obj.mode != 'EDIT':
            return
            
        ref_obj_name = getattr(self, 'ref_object_name', '')
        if not ref_obj_name:
            ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        if not ref_obj:
            return
            
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        
        region = context.region
        rv3d = context.space_data.region_3d
        mouse_vec = mathutils.Vector(mouse_coord)
        
        pin_layer = bm.verts.layers.int.get("tp_is_pinned")
        
        boundary_edges = get_seam_target_edges(bm)
        boundary_verts = {v for e in boundary_edges for v in e.verts}

        verts_in_brush = []
        for v in bm.verts:
            if v not in boundary_verts:
                continue
                
            is_v_pinned = pin_layer and (v[pin_layer] == 1)
            if is_v_pinned:
                continue
            
            world_co = topo_obj.matrix_world @ v.co
            screen_coord = location_3d_to_region_2d(region, rv3d, world_co)
            if screen_coord:
                dist = (screen_coord - mouse_vec).length
                if dist <= radius:
                    verts_in_brush.append(v)
                    
        if not verts_in_brush:
            return
            
        # Store original coordinates before they are modified during this stroke
        if not hasattr(self, 'smooth_initial_cos') or self.smooth_initial_cos is None:
            self.smooth_initial_cos = {}
        for v in verts_in_brush:
            if v.index not in self.smooth_initial_cos:
                self.smooth_initial_cos[v.index] = v.co.copy()
            
        orig_cos = {v: v.co.copy() for v in bm.verts}
        
        new_cos = {}
        for v in verts_in_brush:
            boundary_neighbors = []
            for e in v.link_edges:
                if e in boundary_edges:
                    other_v = e.other_vert(v)
                    boundary_neighbors.append(other_v)
            
            if boundary_neighbors:
                smoothed_co = sum((orig_cos[n] for n in boundary_neighbors), mathutils.Vector()) / len(boundary_neighbors)
                curr_strength = strength * 0.5 if len(boundary_neighbors) == 1 else strength
                new_cos[v] = orig_cos[v] * (1.0 - curr_strength) + smoothed_co * curr_strength
                
        if not new_cos:
            return
            
        ref_matrix_world = ref_obj.matrix_world
        ref_matrix_inverse = ref_matrix_world.inverted()
        topo_world = topo_obj.matrix_world
        topo_inverse = topo_world.inverted()
        
        for v, local_co in new_cos.items():
            world_co = topo_world @ local_co
            local_target = ref_matrix_inverse @ world_co
            success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
            if success:
                local_pt = location + normal * 0.0005
                proposed_world_co = ref_matrix_world @ local_pt
                if is_point_in_mask(proposed_world_co, context):
                    pass
                else:
                    v.co = topo_inverse @ proposed_world_co
            else:
                if is_point_in_mask(world_co, context):
                    pass
                else:
                    v.co = local_co
                
        bmesh.update_edit_mesh(topo_obj.data)
        
        # Keep track of all vertices that were smoothed in this session
        if not hasattr(self, 'smoothed_vert_indices') or self.smoothed_vert_indices is None:
            self.smoothed_vert_indices = set()
        for v in new_cos.keys():
            self.smoothed_vert_indices.add(v.index)
            
        # Update the grids connected to the smoothed vertices in real-time
        if context.scene.tp_boundary_mode:
            try:
                from .op_grid_fill import update_grids_for_vertices
                update_grids_for_vertices(context, [v.index for v in new_cos.keys()], is_interactive=True)
            except Exception as e:
                print("Error updating grids for smoothed vertices in real-time:", e)
                
        context.area.tag_redraw()
        self.smooth_dragged = True

    def conform_to_surface(self, context, active_indices=None):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj:
            return
            
        ref_obj_name = getattr(self, 'ref_object_name', '')
        if not ref_obj_name:
            ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        if not ref_obj:
            return
            
        is_edit = (topo_obj.mode == 'EDIT')
        bm = bmesh.from_edit_mesh(topo_obj.data) if is_edit else bmesh.new()
        if not is_edit:
            bm.from_mesh(topo_obj.data)
            
        matrix_world = ref_obj.matrix_world
        matrix_inverse = matrix_world.inverted()
        topo_inverse = topo_obj.matrix_world.inverted()
        topo_world = topo_obj.matrix_world
        
        bm.verts.ensure_lookup_table()
        pin_layer = bm.verts.layers.int.get("tp_is_pinned")
        
        for v in bm.verts:
            if active_indices is not None and v.index not in active_indices:
                continue
            is_v_pinned = pin_layer and (v[pin_layer] == 1)
            if is_v_pinned:
                continue
            try:
                world_co = topo_world @ v.co
                local_target = matrix_inverse @ world_co
                success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                if success:
                    local_pt = location + normal * 0.0005
                    v.co = topo_inverse @ (matrix_world @ local_pt)
            except Exception:
                pass
                
        if is_edit:
            bmesh.update_edit_mesh(topo_obj.data)
        else:
            bm.to_mesh(topo_obj.data)
            bm.free()
            topo_obj.data.update()

    def get_or_create_vertex(self, bm, local_pt, threshold=0.001):
        for v in bm.verts:
            if (v.co - local_pt).length < threshold:
                return v
        new_v = bm.verts.new(local_pt)
        bm.verts.ensure_lookup_table()
        return new_v

    def resample_stroke(self, context, points, is_closed):
        n = len(points)
        if n < 2:
            return points
            
        dists = [0.0]
        for i in range(n - 1):
            dists.append(dists[-1] + (points[i+1] - points[i]).length)
            
        total_len = dists[-1]
        if total_len < 0.001:
            if context.scene.tp_use_fixed_point_count:
                return [points[0]] * max(1, context.scene.tp_fixed_point_count)
            return [points[0]] * 4
            
        if context.scene.tp_use_fixed_point_count:
            target_count = context.scene.tp_fixed_point_count
            if is_closed:
                M = max(3, target_count)
            else:
                M = max(2, target_count)
        else:
            edge_len = context.scene.tp_edge_length
            if edge_len < 0.001:
                edge_len = 0.001
            if is_closed:
                n_initial = max(3, round(total_len / edge_len))
            else:
                n_initial = max(2, round(total_len / edge_len) + 1)
                
            M = max(4, int((n_initial + 2) / 4) * 4)
        
        resampled = []
        
        if is_closed:
            for j in range(M):
                target_d = (j / M) * total_len
                idx = 1
                for k in range(1, len(dists)):
                    if dists[k] >= target_d:
                        idx = k
                        break
                
                d_start = dists[idx-1]
                d_end = dists[idx]
                segment_len = d_end - d_start
                factor = (target_d - d_start) / segment_len if segment_len > 0.0 else 0.0
                
                new_pt = points[idx-1].lerp(points[idx], factor)
                resampled.append(new_pt)
            resampled.append(resampled[0])
        else:
            for j in range(M):
                target_d = (j / (M - 1)) * total_len if M > 1 else 0.0
                idx = 1
                for k in range(1, len(dists)):
                    if dists[k] >= target_d:
                        idx = k
                        break
                
                d_start = dists[idx-1]
                d_end = dists[idx]
                segment_len = d_end - d_start
                factor = (target_d - d_start) / segment_len if segment_len > 0.0 else 0.0
                
                new_pt = points[idx-1].lerp(points[idx], factor)
                resampled.append(new_pt)
                
        return resampled

    def find_shortest_path_edges_in_bm(self, bm, v_start, v_end):
        if v_start == v_end:
            return []
            
        queue = [[v_start]]
        visited = {v_start.index}
        
        while queue:
            path = queue.pop(0)
            curr = path[-1]
            
            if curr.index == v_end.index:
                edges = []
                for i in range(len(path) - 1):
                    edge = bm.edges.get((path[i], path[i+1]))
                    if edge:
                        edges.append(edge)
                return edges
                
            for edge in curr.link_edges:
                nbr = edge.other_vert(curr)
                if nbr.index not in visited:
                    visited.add(nbr.index)
                    queue.append(path + [nbr])
                    
        return None

    def resample_stroke_segments(self, context, points, snap_indices, is_closed):
        n = len(points)
        if n < 2:
            return points, snap_indices
            
        edge_len = context.scene.tp_edge_length
        if edge_len < 0.001:
            edge_len = 0.001
            
        log_lines = []
        log_lines.append("--- Calling resample_stroke_segments ---")
        log_lines.append(f"Original points count: {n}, edge_len: {edge_len}, is_closed: {is_closed}")
        for i, p in enumerate(points):
            log_lines.append(f"  Point {i}: {p} (snap: {snap_indices[i]})")
            
        if is_closed:
            points = list(points)
            points[-1] = points[0]
            snap_indices = list(snap_indices)
            snap_indices[-1] = snap_indices[0]
            
        # Get reference object
        ref_obj_name = getattr(self, 'ref_object_name', '')
        if not ref_obj_name:
            ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        log_lines.append(f"ref_obj_name: {ref_obj_name}, found ref_obj: {ref_obj is not None}")
        
        ref_matrix = None
        ref_inverse = None
        if ref_obj:
            ref_matrix = ref_obj.matrix_world
            ref_inverse = ref_matrix.inverted()
            
        # Get 2D region/view info for raycasting
        region = context.region
        rv3d = context.space_data.region_3d
        
        # Calculate boundaries
        boundaries = []
        if getattr(self, 'is_polyline', False):
            log_lines.append("Polyline mode: treating all clicked points as corners (boundaries)")
            for i in range(n):
                boundaries.append((i, snap_indices[i]))
        else:
            log_lines.append("Drag mode: only snap points are boundaries")
            boundaries.append((0, snap_indices[0]))
            for i in range(1, n - 1):
                if snap_indices[i] is not None:
                    if snap_indices[i] != boundaries[-1][1]:
                        boundaries.append((i, snap_indices[i]))
            if n - 1 > boundaries[-1][0]:
                boundaries.append((n - 1, snap_indices[-1]))
            
        start_snap_idx = snap_indices[0]
        end_snap_idx = snap_indices[-1]
        path_edges = None
        if not is_closed and start_snap_idx is not None and end_snap_idx is not None:
            topo_obj_name = "TP_Topology_Mesh"
            topo_obj = bpy.data.objects.get(topo_obj_name)
            if topo_obj:
                if topo_obj.mode == 'EDIT':
                    bm_temp = bmesh.from_edit_mesh(topo_obj.data)
                else:
                    bm_temp = bmesh.new()
                    bm_temp.from_mesh(topo_obj.data)
                
                bm_temp.verts.ensure_lookup_table()
                bm_temp.edges.ensure_lookup_table()
                
                if start_snap_idx < len(bm_temp.verts) and end_snap_idx < len(bm_temp.verts):
                    v_start = bm_temp.verts[start_snap_idx]
                    v_end = bm_temp.verts[end_snap_idx]
                    path_edges = self.find_shortest_path_edges_in_bm(bm_temp, v_start, v_end)
                    
                if topo_obj.mode != 'EDIT':
                    bm_temp.free()
            
        num_segs = len(boundaries) - 1
        seg_lengths = []
        seg_points = []
        seg_snap_indices = []
        
        import math
        
        for j in range(num_segs):
            start_idx = boundaries[j][0]
            end_idx = boundaries[j+1][0]
            
            pts = points[start_idx : end_idx + 1]
            snaps = snap_indices[start_idx : end_idx + 1]
            
            # Densify this segment if there are large gaps (e.g. straight segments)
            dense_pts = []
            dense_snaps = []
            for i in range(len(pts) - 1):
                p_start = pts[i]
                p_end = pts[i+1]
                snap_start = snaps[i]
                
                dense_pts.append(p_start)
                dense_snaps.append(snap_start)
                
                segment_dist = (p_end - p_start).length
                if segment_dist > edge_len:
                    step_size = edge_len / 4.0
                    num_steps = int(math.ceil(segment_dist / step_size))
                    log_lines.append(f"  Densifying sub-segment {i} of seg {j} ({segment_dist:.4f} > {edge_len:.4f}) with {num_steps} steps...")
                    
                    p_start_2d = location_3d_to_region_2d(region, rv3d, p_start)
                    p_end_2d = location_3d_to_region_2d(region, rv3d, p_end)
                    
                    for s in range(1, num_steps):
                        factor = s / num_steps
                        pt_3d = None
                        if p_start_2d and p_end_2d:
                            pt_2d = p_start_2d.lerp(p_end_2d, factor)
                            pt_3d = self.get_surface_point(context, pt_2d)
                            
                        if pt_3d is None:
                            pt_3d = p_start.lerp(p_end, factor)
                            if ref_obj and ref_matrix and ref_inverse:
                                try:
                                    local_target = ref_inverse @ pt_3d
                                    success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                                    if success:
                                        local_pt = location + normal * 0.0005
                                        pt_3d = ref_matrix @ local_pt
                                except:
                                    pass
                        dense_pts.append(pt_3d)
                        dense_snaps.append(None)
            dense_pts.append(pts[-1])
            dense_snaps.append(snaps[-1])
            
            pts = dense_pts
            snaps = dense_snaps
            
            l_seg = 0.0
            for k in range(len(pts) - 1):
                l_seg += (pts[k+1] - pts[k]).length
                
            seg_lengths.append(l_seg)
            seg_points.append(pts)
            seg_snap_indices.append(snaps)
            
        if context.scene.tp_use_fixed_point_count:
            target_count = context.scene.tp_fixed_point_count
            if is_closed:
                E_target = max(max(3, num_segs), target_count)
            else:
                E_target = max(max(1, num_segs), target_count - 1)
        else:
            initial_edges = []
            for l_seg in seg_lengths:
                initial_edges.append(max(1, round(l_seg / edge_len)))
                
            total_initial_edges = sum(initial_edges)
            
            if is_closed:
                V_target = max(4, int((total_initial_edges + 2) / 4) * 4)
                E_target = V_target
            else:
                V_target = max(4, int((total_initial_edges + 1 + 2) / 4) * 4)
                E_target = V_target - 1
                
                if path_edges is not None:
                    E_existing = len(path_edges)
                    remainder = E_existing % 4
                    target_mod = (4 - remainder) % 4
                    current_mod = total_initial_edges % 4
                    diff = target_mod - current_mod
                    if diff > 2:
                        diff -= 4
                    elif diff < -2:
                        diff += 4
                    E_target = max(1, total_initial_edges + diff)
        
        total_len = sum(seg_lengths)
        log_lines.append(f"Total segment length (surface): {total_len:.4f}, E_target: {E_target}")
        if total_len < 0.001:
            distributed_edges = [max(1, E_target // num_segs)] * num_segs
            diff = E_target - sum(distributed_edges)
            for d in range(abs(diff)):
                idx = d % num_segs
                distributed_edges[idx] += 1 if diff > 0 else -1
        else:
            distributed_edges = []
            for l_seg in seg_lengths:
                distributed_edges.append(max(1, round((l_seg / total_len) * E_target)))
                
            diff = E_target - sum(distributed_edges)
            if diff != 0:
                remainders = []
                for idx, l_seg in enumerate(seg_lengths):
                    target = (l_seg / total_len) * E_target
                    err = target - distributed_edges[idx]
                    remainders.append((err, idx))
                
                if diff > 0:
                    remainders.sort(reverse=True, key=lambda x: x[0])
                    for d in range(diff):
                        idx = remainders[d % len(remainders)][1]
                        distributed_edges[idx] += 1
                else:
                    remainders.sort(key=lambda x: x[0])
                    for d in range(abs(diff)):
                        idx = remainders[d % len(remainders)][1]
                        if distributed_edges[idx] > 1:
                            distributed_edges[idx] -= 1
                            
        resampled_points = []
        resampled_snap_indices = []
        
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        ref_matrix = None
        ref_inverse = None
        if ref_obj:
            ref_matrix = ref_obj.matrix_world
            ref_inverse = ref_matrix.inverted()
            
        for j in range(num_segs):
            pts = seg_points[j]
            m_edges = distributed_edges[j]
            m_verts = m_edges + 1
            
            dists = [0.0]
            for k in range(len(pts) - 1):
                dists.append(dists[-1] + (pts[k+1] - pts[k]).length)
                
            total_seg_len = dists[-1]
            
            seg_resampled = []
            for k in range(m_verts):
                if total_seg_len < 0.001:
                    new_pt = pts[0]
                else:
                    target_d = (k / m_edges) * total_seg_len
                    idx_d = 1
                    for idx_k in range(1, len(dists)):
                        if dists[idx_k] >= target_d:
                            idx_d = idx_k
                            break
                    d_start = dists[idx_d-1]
                    d_end = dists[idx_d]
                    seg_len = d_end - d_start
                    factor = (target_d - d_start) / seg_len if seg_len > 0.0 else 0.0
                    new_pt = pts[idx_d-1].lerp(pts[idx_d], factor)
                
                if ref_obj and ref_matrix and ref_inverse:
                    try:
                        local_target = ref_inverse @ new_pt
                        success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                        if success:
                            local_pt = location + normal * 0.0005
                            new_pt = ref_matrix @ local_pt
                    except Exception:
                        pass
                        
                seg_resampled.append(new_pt)
                
            seg_resampled_snaps = [None] * m_verts
            seg_resampled_snaps[0] = seg_snap_indices[j][0]
            seg_resampled_snaps[-1] = seg_snap_indices[j][-1]
            
            if j == 0:
                resampled_points.extend(seg_resampled)
                resampled_snap_indices.extend(seg_resampled_snaps)
            else:
                resampled_points.extend(seg_resampled[1:])
                resampled_snap_indices.extend(seg_resampled_snaps[1:])
                
        if is_closed:
            resampled_points[-1] = resampled_points[0]
            resampled_snap_indices[-1] = resampled_snap_indices[0]
            
        log_lines.append(f"Returning resampled_points count: {len(resampled_points)}")
        for i, p in enumerate(resampled_points):
            log_lines.append(f"  Final Point {i}: {p}")
            
        try:
            with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f:
                f.write("\n".join(log_lines) + "\n")
        except:
            pass
            
        return resampled_points, resampled_snap_indices

    def create_geometry(self, context):
        self.subdiv_original_loops = None
        self.subdiv_multiplier = 1.0
        if not self.stroke_points or len(self.stroke_points) < 2:
            return
            
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj:
            return
            
        is_closed = False
        if len(self.stroke_points) >= 3:
            start_snap = self.stroke_snap_indices[0] if (self.stroke_snap_indices and len(self.stroke_snap_indices) > 0) else None
            end_snap = self.stroke_snap_indices[-1] if (self.stroke_snap_indices and len(self.stroke_snap_indices) > 1) else None
            if start_snap is not None and end_snap is not None and start_snap == end_snap:
                is_closed = True
            else:
                region = context.region
                rv3d = context.space_data.region_3d
                p0_2d = location_3d_to_region_2d(region, rv3d, self.stroke_points[0])
                pn_2d = location_3d_to_region_2d(region, rv3d, self.stroke_points[-1])
                if p0_2d and pn_2d:
                    if (p0_2d - pn_2d).length < 20:
                        is_closed = True
                        
        if not getattr(self, 'bypass_resample', False):
            self.stroke_points, self.stroke_snap_indices = self.resample_stroke_segments(
                context, self.stroke_points, self.stroke_snap_indices, is_closed
            )

        curr_indices = []

        if topo_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(topo_obj.data)
            inv_matrix = topo_obj.matrix_world.inverted()
            
            bm.verts.ensure_lookup_table()
            
            # Resolve the BMesh vertices for the stroke first, before any modification
            stroke_bm_verts = []
            for idx_pt, pt in enumerate(self.stroke_points):
                snap_idx = self.stroke_snap_indices[idx_pt]
                v = None
                if snap_idx is not None and snap_idx < len(bm.verts):
                    v = bm.verts[snap_idx]
                stroke_bm_verts.append(v)
                
            # If in boundary mode, check if we need to split an existing loop
            if context.scene.tp_boundary_mode:
                grid_layer = bm.faces.layers.int.get("tp_is_grid")
                if grid_layer:
                    target_lid = self.find_closest_loop_id(context, self.stroke_points, bm, topo_obj)
                    if target_lid is not None:
                        F_loop = [f for f in bm.faces if f[grid_layer] == target_lid]
                        if F_loop:
                            F_loop_set = set(F_loop)
                            E_loop_all = set()
                            for f in F_loop_set:
                                E_loop_all.update(f.edges)
                            E_boundary = {e for e in E_loop_all if len([f for f in e.link_faces if f[grid_layer] == target_lid]) == 1}
                            V_boundary = {v for e in E_boundary for v in e.verts}
                            
                            # Validate if it's actually a split line:
                            # 1. Stroke must not be a closed loop
                            # 2. Both start and end points of the stroke must connect to the boundary of the target loop
                            # 3. The middle point of the stroke must lie inside the target loop
                            is_split = False
                            if not is_closed and stroke_bm_verts and len(stroke_bm_verts) >= 2:
                                v_start = stroke_bm_verts[0]
                                v_end = stroke_bm_verts[-1]
                                if v_start in V_boundary and v_end in V_boundary:
                                    mid_idx = len(self.stroke_points) // 2
                                    if 0 < mid_idx < len(self.stroke_points) - 1:
                                        mid_pt_world = self.stroke_points[mid_idx]
                                        mid_pt_local = inv_matrix @ mid_pt_world
                                        
                                        min_dist_to_loop = float('inf')
                                        for f in F_loop:
                                            dist = self.dist_to_face(mid_pt_local, f)
                                            if dist < min_dist_to_loop:
                                                min_dist_to_loop = dist
                                                
                                        edge_len = getattr(context.scene, "tp_edge_length", 0.5)
                                        if min_dist_to_loop <= edge_len * 0.3:
                                            is_split = True
                                            
                            if is_split:
                                no_auto_layer = bm.edges.layers.int.get("tp_no_auto_fill")
                                if no_auto_layer:
                                    for e in E_boundary:
                                        if e.is_valid:
                                            e[no_auto_layer] = 0
                                            
                                E_internal = E_loop_all - E_boundary
                                V_internal = {v for f in F_loop_set for v in f.verts} - V_boundary
                                
                                bmesh.ops.delete(bm, geom=[f for f in F_loop if f.is_valid], context='FACES_ONLY')
                                bmesh.ops.delete(bm, geom=[e for e in E_internal if e.is_valid], context='EDGES')
                                bmesh.ops.delete(bm, geom=[v for v in V_internal if v.is_valid], context='VERTS')
                                
                                bm.verts.ensure_lookup_table()
                                bm.edges.ensure_lookup_table()
                                bm.faces.ensure_lookup_table()
                            
            bm_verts = []
            for idx_pt, pt in enumerate(self.stroke_points):
                local_pt = inv_matrix @ pt
                v = stroke_bm_verts[idx_pt]
                if v is not None and not v.is_valid:
                    v = None
                if v is None:
                    v = self.get_or_create_vertex(bm, local_pt)
                bm_verts.append(v)
                
            bm.verts.ensure_lookup_table()
            
            created_edges = []
            for i in range(len(bm_verts) - 1):
                v1, v2 = bm_verts[i], bm_verts[i+1]
                if v1 != v2:
                    edge = bm.edges.get((v1, v2))
                    if not edge:
                        edge = bm.edges.new((v1, v2))
                    created_edges.append(edge)
                    
            if getattr(self, 'is_creating_outside_loop', False):
                no_auto_layer = bm.edges.layers.int.get("tp_no_auto_fill") or bm.edges.layers.int.new("tp_no_auto_fill")
                for edge in created_edges:
                    if edge.is_valid:
                        edge[no_auto_layer] = 1
                
            bm.verts.index_update()
            curr_indices = [v.index for v in bm_verts if v is not None]
            bmesh.update_edit_mesh(topo_obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(topo_obj.data)
            inv_matrix = topo_obj.matrix_world.inverted()
            
            bm.verts.ensure_lookup_table()
            
            # Resolve the BMesh vertices for the stroke first, before any modification
            stroke_bm_verts = []
            for idx_pt, pt in enumerate(self.stroke_points):
                snap_idx = self.stroke_snap_indices[idx_pt]
                v = None
                if snap_idx is not None and snap_idx < len(bm.verts):
                    v = bm.verts[snap_idx]
                stroke_bm_verts.append(v)
                
            # If in boundary mode, check if we need to split an existing loop
            if context.scene.tp_boundary_mode:
                grid_layer = bm.faces.layers.int.get("tp_is_grid")
                if grid_layer:
                    target_lid = self.find_closest_loop_id(context, self.stroke_points, bm, topo_obj)
                    if target_lid is not None:
                        F_loop = [f for f in bm.faces if f[grid_layer] == target_lid]
                        if F_loop:
                            F_loop_set = set(F_loop)
                            E_loop_all = set()
                            for f in F_loop_set:
                                E_loop_all.update(f.edges)
                            E_boundary = {e for e in E_loop_all if len([f for f in e.link_faces if f[grid_layer] == target_lid]) == 1}
                            V_boundary = {v for e in E_boundary for v in e.verts}
                            
                            # Validate if it's actually a split line:
                            # 1. Stroke must not be a closed loop
                            # 2. Both start and end points of the stroke must connect to the boundary of the target loop
                            # 3. The middle point of the stroke must lie inside the target loop
                            is_split = False
                            if not is_closed and stroke_bm_verts and len(stroke_bm_verts) >= 2:
                                v_start = stroke_bm_verts[0]
                                v_end = stroke_bm_verts[-1]
                                if v_start in V_boundary and v_end in V_boundary:
                                    mid_idx = len(self.stroke_points) // 2
                                    if 0 < mid_idx < len(self.stroke_points) - 1:
                                        mid_pt_world = self.stroke_points[mid_idx]
                                        mid_pt_local = inv_matrix @ mid_pt_world
                                        
                                        min_dist_to_loop = float('inf')
                                        for f in F_loop:
                                            dist = self.dist_to_face(mid_pt_local, f)
                                            if dist < min_dist_to_loop:
                                                min_dist_to_loop = dist
                                                
                                        edge_len = getattr(context.scene, "tp_edge_length", 0.5)
                                        if min_dist_to_loop <= edge_len * 0.3:
                                            is_split = True
                                            
                            if is_split:
                                no_auto_layer = bm.edges.layers.int.get("tp_no_auto_fill")
                                if no_auto_layer:
                                    for e in E_boundary:
                                        if e.is_valid:
                                            e[no_auto_layer] = 0
                                            
                                E_internal = E_loop_all - E_boundary
                                V_internal = {v for f in F_loop_set for v in f.verts} - V_boundary
                                
                                bmesh.ops.delete(bm, geom=[f for f in F_loop if f.is_valid], context='FACES_ONLY')
                                bmesh.ops.delete(bm, geom=[e for e in E_internal if e.is_valid], context='EDGES')
                                bmesh.ops.delete(bm, geom=[v for v in V_internal if v.is_valid], context='VERTS')
                                
                                bm.verts.ensure_lookup_table()
                                bm.edges.ensure_lookup_table()
                                bm.faces.ensure_lookup_table()
                            
            bm_verts = []
            for idx_pt, pt in enumerate(self.stroke_points):
                local_pt = inv_matrix @ pt
                v = stroke_bm_verts[idx_pt]
                if v is not None and not v.is_valid:
                    v = None
                if v is None:
                    v = self.get_or_create_vertex(bm, local_pt)
                bm_verts.append(v)
                
            bm.verts.ensure_lookup_table()
            
            created_edges = []
            for i in range(len(bm_verts) - 1):
                v1, v2 = bm_verts[i], bm_verts[i+1]
                if v1 != v2:
                    edge = bm.edges.get((v1, v2))
                    if not edge:
                        edge = bm.edges.new((v1, v2))
                    created_edges.append(edge)
                    
            if getattr(self, 'is_creating_outside_loop', False):
                no_auto_layer = bm.edges.layers.int.get("tp_no_auto_fill") or bm.edges.layers.int.new("tp_no_auto_fill")
                for edge in created_edges:
                    if edge.is_valid:
                        edge[no_auto_layer] = 1
                
            bm.verts.index_update()
            curr_indices = [v.index for v in bm_verts if v is not None]
            bm.to_mesh(topo_obj.data)
            bm.free()
            topo_obj.data.update()
            
        if not hasattr(self, 'stroke_history'):
            self.stroke_history = []
        self.stroke_history.append(curr_indices)
        if len(self.stroke_history) > 2:
            self.stroke_history.pop(0)

        active_indices = set()
        if topo_obj.mode == 'EDIT':
            bm_temp = bmesh.from_edit_mesh(topo_obj.data)
            bm_temp.verts.ensure_lookup_table()
            existing_indices = {v.index for v in bm_temp.verts}
        else:
            bm_temp = bmesh.new()
            bm_temp.from_mesh(topo_obj.data)
            bm_temp.verts.ensure_lookup_table()
            existing_indices = {v.index for v in bm_temp.verts}

        validated_history = []
        for stroke in self.stroke_history:
            valid_stroke = [idx for idx in stroke if idx in existing_indices]
            if valid_stroke:
                validated_history.append(valid_stroke)
                active_indices.update(valid_stroke)
        self.stroke_history = validated_history

        expanded_active_indices = set(active_indices)
        for idx in active_indices:
            if idx < len(bm_temp.verts):
                v = bm_temp.verts[idx]
                for edge in v.link_edges:
                    nbr = edge.other_vert(v)
                    expanded_active_indices.add(nbr.index)

        if topo_obj.mode != 'EDIT':
            bm_temp.free()

        if expanded_active_indices:
            self.conform_to_surface(context, active_indices=expanded_active_indices)
        
        self.rebuild_kd_tree()

        # Select the last vertex of the newly created line
        if topo_obj.mode == 'EDIT' and curr_indices:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
            for f in bm.faces:
                f.select = False
            
            last_idx = curr_indices[-1]
            if last_idx < len(bm.verts):
                last_v = bm.verts[last_idx]
                last_v.select = True
                bm.select_history.clear()
                bm.select_history.add(last_v)
            bmesh.update_edit_mesh(topo_obj.data)

            # Check if a closed loop is generated, and if so, perform grid fill immediately
            if context.scene.tp_auto_grid_fill:
                from .op_grid_fill import analyze_selection, find_minimum_cycle_basis, trace_cycle_verts, check_is_grid_filled
                
                bm = bmesh.from_edit_mesh(topo_obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                
                # Save original selection state (which is just the last vertex at this point)
                orig_sel_verts = [v.index for v in bm.verts if v.select]
                orig_sel_edges = [e.index for e in bm.edges if e.select]
                orig_sel_faces = [f.index for f in bm.faces if f.select]
                
                # Select all vertices and edges of the newly created stroke to check if they form/part of a closed loop
                for v in bm.verts:
                    v.select = (v.index in curr_indices)
                for e in bm.edges:
                    e.select = (e.verts[0].index in curr_indices and e.verts[1].index in curr_indices)
                try:
                    with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_log:
                        f_log.write(f"  [op_draw select] curr_indices: {curr_indices}\n")
                        f_log.write(f"  [op_draw select] selected in bm: {[v.index for v in bm.verts if v.select]}\n")
                except:
                    pass
                # Analyze selection to see if it contains a valid unfilled closed loop
                components, err_msg = analyze_selection(bm, is_auto=True)
                try:
                    with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_log:
                        f_log.write(f"\n--- op_draw check: analyze_selection returned {len(components)} components, err_msg={err_msg} ---\n")
                        for c_idx, c in enumerate(components):
                            f_log.write(f"  Comp {c_idx}: type={c['type']}, verts={len(c['vert_indices'])}, is_grid_filled={c['is_grid_filled']}\n")
                except:
                    pass
                
                has_valid_unfilled_loop = False
                if components and not err_msg:
                    for comp in components:
                        if comp['type'] in {'loop', 'non_linear_loops'} and not comp.get('is_grid_filled', False):
                            if context.scene.tp_use_fixed_point_count:
                                if comp['type'] == 'loop':
                                    num_points = len(comp['vert_indices'])
                                    if num_points % 4 != 0:
                                        continue
                                elif comp['type'] == 'non_linear_loops':
                                    raw_cycles = find_minimum_cycle_basis(bm, comp['verts'], comp['edges'])
                                    if raw_cycles:
                                        all_cycles_valid = True
                                        for c in raw_cycles:
                                            cycle_verts = trace_cycle_verts(c)
                                            if not check_is_grid_filled(bm, cycle_verts, c):
                                                if len(cycle_verts) % 4 != 0:
                                                    all_cycles_valid = False
                                                    break
                                        if not all_cycles_valid:
                                            continue
                                    else:
                                        continue
                            has_valid_unfilled_loop = True
                            break
                
                if not getattr(self, 'prevent_auto_grid_fill', False) and has_valid_unfilled_loop:
                    # We have a valid unfilled closed loop!
                    # Keep the selection of the new stroke vertices, so the grid fill operator knows what to fill
                    bmesh.update_edit_mesh(topo_obj.data)
                    
                    # Run the grid fill operator
                    try:
                        bpy.ops.object.tp_topology_grid_fill(is_auto=True)
                    except Exception as e:
                        import traceback
                        tb_str = traceback.format_exc()
                        try:
                            with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_err:
                                f_err.write("--- Auto grid fill error ---\n" + tb_str + "\n")
                        except:
                            pass
                        print("Auto grid fill error:", e)
                    
                    # After grid fill, restore to only selecting the last vertex of the stroke if it exists and is valid
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    bm.verts.ensure_lookup_table()
                    for v in bm.verts:
                        v.select = False
                    for e in bm.edges:
                        e.select = False
                    for f in bm.faces:
                        f.select = False
                    
                    if last_idx < len(bm.verts):
                        last_v = bm.verts[last_idx]
                        if last_v.is_valid:
                            last_v.select = True
                            bm.select_history.clear()
                            bm.select_history.add(last_v)
                    bmesh.update_edit_mesh(topo_obj.data)
                else:
                    # No closed loop was formed, restore the selection (the last vertex)
                    for v in bm.verts:
                        v.select = (v.index in orig_sel_verts)
                    for e in bm.edges:
                        e.select = (e.index in orig_sel_edges)
                    for f in bm.faces:
                        f.select = (f.index in orig_sel_faces)
                    bmesh.update_edit_mesh(topo_obj.data)

    def create_outside_drag_geometry(self, context, start_2d, end_2d):
        import math
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        if not ref_obj:
            return
            
        region = context.region
        rv3d = context.space_data.region_3d
        
        # 计算屏幕距离与采样点数
        dx = end_2d[0] - start_2d[0]
        dy = end_2d[1] - start_2d[1]
        dist_px = (dx*dx + dy*dy) ** 0.5
        if dist_px < 8:
            return
            
        num_samples = max(50, int(dist_px / 5.0))
        
        matrix_world = ref_obj.matrix_world
        matrix_inverse = matrix_world.inverted()
        
        # 1. 离散采样并进行多重投射，收集所有前/后表面及重叠物体的进出段 (Intervals)
        all_intervals = [[] for _ in range(num_samples + 1)]
        
        start_vec = mathutils.Vector(start_2d)
        end_vec = mathutils.Vector(end_2d)
        depsgraph = context.evaluated_depsgraph_get()
        
        for i in range(num_samples + 1):
            factor = i / num_samples
            pt_2d = start_vec.lerp(end_vec, factor)
            
            ray_origin = region_2d_to_origin_3d(region, rv3d, pt_2d)
            ray_vector = region_2d_to_vector_3d(region, rv3d, pt_2d)
            
            ray_origin_local = matrix_inverse @ ray_origin
            ray_vector_local = matrix_inverse.to_3x3() @ ray_vector
            
            # 使用多重穿透投影获取该射线下的所有碰撞点
            hits_local = ray_cast_multi(ref_obj, ray_origin_local, ray_vector_local, depsgraph)
            
            # 两两成对：每个奇数段（入点/前表面）与偶数段（出点/后表面）代表一个实心几何体厚度区间
            num_pairs = len(hits_local) // 2
            for k in range(num_pairs):
                loc_front, norm_front, _ = hits_local[2*k]
                loc_back, norm_back, _ = hits_local[2*k + 1]
                
                local_pt_front = loc_front + norm_front * 0.0005
                world_pt_front = matrix_world @ local_pt_front
                
                local_pt_back = loc_back + norm_back * 0.0005
                world_pt_back = matrix_world @ local_pt_back
                
                # 计算相机空间深度（沿着视线方向的投影距离）
                depth = (world_pt_front - ray_origin).dot(ray_vector)
                
                all_intervals[i].append({
                    'front': world_pt_front,
                    'back': world_pt_back,
                    'depth': depth
                })
                
        # 2. 轨迹关联追踪 (Track Association)：区分深度方向上重叠的各个物体（例如并排重叠的圆锥）
        active_tracks = []
        finished_tracks = []
        
        edge_len = context.scene.tp_edge_length
        # 关联阈值：如果相邻采样点的 3D 距离在此范围内，视为同一个物体
        assoc_threshold = edge_len * 3.0
        
        for i in range(num_samples + 1):
            intervals = all_intervals[i]
            matched_intervals = set()
            matched_tracks = set()
            matches = []
            
            # 对所有仍在活动的轨迹，计算其末端点到当前各个区间的距离
            for t_idx, track in enumerate(active_tracks):
                if track['indices'][-1] == i - 1:
                    last_front = track['front_pts'][-1]
                    for val_idx, interval in enumerate(intervals):
                        dist = (interval['front'] - last_front).length
                        if dist < assoc_threshold:
                            matches.append((dist, t_idx, val_idx))
                            
            # 优先匹配距离最近 of 区间
            matches.sort(key=lambda x: x[0])
            for dist, t_idx, val_idx in matches:
                if t_idx not in matched_tracks and val_idx not in matched_intervals:
                    matched_tracks.add(t_idx)
                    matched_intervals.add(val_idx)
                    track = active_tracks[t_idx]
                    track['indices'].append(i)
                    track['front_pts'].append(intervals[val_idx]['front'])
                    track['back_pts'].append(intervals[val_idx]['back'])
                    track['depths'].append(intervals[val_idx]['depth'])
                    
            # 未被匹配的当前区间，作为新轨迹启动（解决新物体探出）
            for val_idx, interval in enumerate(intervals):
                if val_idx not in matched_intervals:
                    active_tracks.append({
                        'indices': [i],
                        'front_pts': [interval['front']],
                        'back_pts': [interval['back']],
                        'depths': [interval['depth']]
                    })
            
            # 整理活动轨迹：若某轨迹在此步没有匹配，则已结束
            new_active_tracks = []
            for track in active_tracks:
                if track['indices'][-1] == i:
                    new_active_tracks.append(track)
                else:
                    if len(track['indices']) >= 2:
                        finished_tracks.append(track)
            active_tracks = new_active_tracks
            
        for track in active_tracks:
            if len(track['indices']) >= 2:
                finished_tracks.append(track)
                
        # 3. 对每条独立轨迹，在沿表面方向进行突变深谷（Prominence-based Valley）二次切分
        final_tracks = []
        prominence_threshold = edge_len * 2.0
        
        for track in finished_tracks:
            front_pts_track = track['front_pts']
            back_pts_track = track['back_pts']
            depths_track = track['depths']
            indices_track = track['indices']
            
            n_pts = len(front_pts_track)
            if n_pts < 5:
                final_tracks.append(track)
                continue
                
            # 五邻域滑动均值平滑，消除表面凹凸噪声
            smoothed_depths = list(depths_track)
            for i in range(2, n_pts - 2):
                smoothed_depths[i] = sum(depths_track[i+k] for k in range(-2, 3)) / 5.0
                
            split_indices = []
            for i in range(2, n_pts - 2):
                # 寻找局部最大深度点（离相机最远的谷底）
                if smoothed_depths[i] > smoothed_depths[i-1] and smoothed_depths[i] > smoothed_depths[i+1]:
                    left_min = min(smoothed_depths[0 : i])
                    right_min = min(smoothed_depths[i+1 : n_pts])
                    
                    prominence_left = smoothed_depths[i] - left_min
                    prominence_right = smoothed_depths[i] - right_min
                    
                    # 只有突出度（高度差）超过阈值时才进行物理断开
                    if prominence_left > prominence_threshold and prominence_right > prominence_threshold:
                        split_indices.append(i)
                        
            # 对轨迹进行断开并生成子轨迹
            curr_start = 0
            for split_idx in sorted(split_indices):
                if split_idx - curr_start >= 2:
                    final_tracks.append({
                        'indices': indices_track[curr_start : split_idx],
                        'front_pts': front_pts_track[curr_start : split_idx],
                        'back_pts': back_pts_track[curr_start : split_idx],
                        'depths': depths_track[curr_start : split_idx]
                    })
                curr_start = split_idx + 1
            if n_pts - curr_start >= 2:
                final_tracks.append({
                    'indices': indices_track[curr_start : n_pts],
                    'front_pts': front_pts_track[curr_start : n_pts],
                    'back_pts': back_pts_track[curr_start : n_pts],
                    'depths': depths_track[curr_start : n_pts]
                })
                
        # 4. 针对最终的所有独立分块，分别生成闭合线圈并创建几何体
        geom_created = False
        for track in final_tracks:
            seg_front = track['front_pts']
            seg_back = track['back_pts']
            
            # 首尾相连组成闭合曲线：前表面路径 + 反转后的后表面路径 + 回到起点
            loop_points = list(seg_front) + list(reversed(seg_back)) + [seg_front[0]]
            
            dists = [0.0]
            for idx in range(len(loop_points) - 1):
                dists.append(dists[-1] + (loop_points[idx+1] - loop_points[idx]).length)
            total_perimeter = dists[-1]
            
            if total_perimeter > 0.001:
                n_initial = max(3, round(total_perimeter / edge_len))
                E_target = max(4, int((n_initial + 2) / 4) * 4)
                
                final_loop_points = []
                for j in range(E_target):
                    target_d = (j / E_target) * total_perimeter
                    idx_d = 1
                    for idx_k in range(1, len(dists)):
                        if dists[idx_k] >= target_d:
                            idx_d = idx_k
                            break
                    d_start = dists[idx_d-1]
                    d_end = dists[idx_d]
                    seg_len = d_end - d_start
                    factor = (target_d - d_start) / seg_len if seg_len > 0.0 else 0.0
                    p_new = loop_points[idx_d-1].lerp(loop_points[idx_d], factor)
                    
                    if ref_obj:
                        try:
                            local_target = matrix_inverse @ p_new
                            success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                            if success:
                                local_pt = location + normal * 0.0005
                                p_new = matrix_world @ local_pt
                        except Exception:
                            pass
                    final_loop_points.append(p_new)
                final_loop_points.append(final_loop_points[0])
                loop_points = final_loop_points
                
            self.stroke_points = loop_points
            self.stroke_snap_indices = [None] * len(loop_points)
            self.is_polyline = False
            self.bypass_resample = True
            
            self.prevent_auto_grid_fill = True
            self.is_creating_outside_loop = True
            try:
                self.create_geometry(context)
            finally:
                self.prevent_auto_grid_fill = False
                self.is_creating_outside_loop = False
                
            self.bypass_resample = False
            geom_created = True
            
        if geom_created:
            try:
                bpy.ops.ed.undo_push(message="TP 包围拓扑绘制")
            except Exception as e:
                print("Error pushing undo step:", e)
            self.report({'INFO'}, "已生成包围拓扑线")

    def cleanup(self, context):
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        try:
            context.window.cursor_set('DEFAULT')
        except Exception:
            pass
        global _active_draw_operator
        _active_draw_operator = None
        try:
            context.window_manager.tp_topology_running = False
        except Exception:
            pass
        self.is_outside_drawing = False

        # 还原用户的吸附设置
        try:
            context.scene.tool_settings.use_snap = self.orig_use_snap
            if hasattr(self, 'orig_use_snap_project') and hasattr(context.scene.tool_settings, 'use_snap_project'):
                context.scene.tool_settings.use_snap_project = self.orig_use_snap_project
            if hasattr(self, 'orig_use_snap_selectable') and hasattr(context.scene.tool_settings, "use_snap_selectable"):
                context.scene.tool_settings.use_snap_selectable = self.orig_use_snap_selectable
            if hasattr(self, 'orig_use_snap_self') and hasattr(context.scene.tool_settings, "use_snap_self"):
                context.scene.tool_settings.use_snap_self = self.orig_use_snap_self
        except Exception:
            pass

        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if topo_obj:
            try:
                self.conform_to_surface(context)
            except Exception as e:
                print("Error doing final conform on cleanup:", e)
                
            # 清理临时修改器
            mod = topo_obj.modifiers.get("TP_Shrinkwrap")
            if mod:
                try:
                    topo_obj.modifiers.remove(mod)
                except Exception as e:
                    print("Error removing modifier on cleanup:", e)

        ref_obj = bpy.data.objects.get(self.ref_object_name)
        if ref_obj:
            try:
                ref_obj.hide_select = False
            except Exception as e:
                print("Error restoring selectability:", e)
                
        if self.draw_handle_lines:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle_lines, 'WINDOW')
            except Exception as e:
                print("Error removing draw_handle_lines:", e)
            self.draw_handle_lines = None
            
        if self.draw_handle_text:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle_text, 'WINDOW')
            except Exception as e:
                print("Error removing draw_handle_text:", e)
            self.draw_handle_text = None
            
        self.stroke_history = []
            
        if hasattr(self, 'grab_backup_bm') and self.grab_backup_bm:
            try:
                self.grab_backup_bm.free()
            except Exception:
                pass
            self.grab_backup_bm = None
            
        if hasattr(self, 'smooth_backup_bm') and self.smooth_backup_bm:
            try:
                self.smooth_backup_bm.free()
            except Exception:
                pass
            self.smooth_backup_bm = None

        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def trace_boundary_loop_from_edges(self, bm, E_boundary):
        if not E_boundary:
            return []
        adj = {}
        for e in E_boundary:
            for v in e.verts:
                adj.setdefault(v, []).append(e)
                
        start_v = None
        for v, edges in adj.items():
            if len(edges) == 2:
                start_v = v
                break
        if not start_v:
            return []
            
        loop_verts = []
        curr_v = start_v
        prev_edge = None
        visited = set()
        
        while curr_v and curr_v not in visited:
            loop_verts.append(curr_v)
            visited.add(curr_v)
            next_v = None
            for e in adj[curr_v]:
                if e != prev_edge:
                    next_v = e.other_vert(curr_v)
                    prev_edge = e
                    break
            if next_v == start_v:
                break
            curr_v = next_v
        return loop_verts

    def trace_all_boundary_components(self, E_boundary):
        """Trace ALL separate closed boundary loops from a set of edges.
        Returns a list of vertex lists, one per connected boundary component.
        Handles ring/annulus topology where boundary_edges form two separate loops."""
        if not E_boundary:
            return []
        adj = {}
        for e in E_boundary:
            for v in e.verts:
                adj.setdefault(v, []).append(e)

        # Only process vertices with exactly 2 boundary edges (valid for closed loops)
        valid_starts = [v for v, edges in adj.items() if len(edges) == 2]
        if not valid_starts:
            return []

        visited_verts = set()
        all_components = []

        for start_v in valid_starts:
            if start_v in visited_verts:
                continue
            # Trace the closed loop starting from start_v
            loop_verts = []
            curr_v = start_v
            prev_edge = None
            local_visited = set()
            while curr_v and curr_v not in local_visited:
                loop_verts.append(curr_v)
                local_visited.add(curr_v)
                next_v = None
                for e in adj[curr_v]:
                    if e != prev_edge:
                        next_v = e.other_vert(curr_v)
                        prev_edge = e
                        break
                if next_v == start_v:
                    break
                curr_v = next_v
            if len(loop_verts) >= 3:
                all_components.append(loop_verts)
                visited_verts.update(loop_verts)

        return all_components

    def find_all_loops(self, bm):
        loops = []
        grid_layer = bm.faces.layers.int.get("tp_is_grid")
        
        # 1. Find all rasterized loops
        if grid_layer:
            loop_ids = set()
            for f in bm.faces:
                if f[grid_layer] > 0:
                    loop_ids.add(f[grid_layer])
                    
            for lid in sorted(loop_ids):
                F_loop = [f for f in bm.faces if f[grid_layer] == lid]
                if not F_loop:
                    continue
                F_loop_set = set(F_loop)
                E_loop_all = set()
                for f in F_loop:
                    E_loop_all.update(f.edges)
                    
                E_boundary = {e for e in E_loop_all if len([f for f in e.link_faces if f[grid_layer] == lid]) == 1}
                V_boundary = {v for e in E_boundary for v in e.verts}
                
                # Check boundary vertex degrees in boundary graph to detect pinch points (degree != 2)
                bnd_adj = {v: [] for v in V_boundary}
                for e in E_boundary:
                    v1, v2 = e.verts
                    bnd_adj[v1].append(v2)
                    bnd_adj[v2].append(v1)
                    
                is_pinched = any(len(neighbors) != 2 for neighbors in bnd_adj.values())
                
                if is_pinched:
                    # Genuinely pinched - skip
                    continue
                
                # Trace all boundary components (handles both disk and ring/annulus topology)
                all_components = self.trace_all_boundary_components(E_boundary)
                total_traced = sum(len(c) for c in all_components)
                
                if not all_components:
                    continue

                if len(all_components) == 1 and total_traced >= len(V_boundary):
                    # Simple disk topology: single boundary loop
                    ordered_verts = all_components[0]
                    loops.append({
                        'type': 'rasterized',
                        'loop_id': lid,
                        'verts': ordered_verts,
                        'faces': F_loop
                    })
                elif len(all_components) == 2 and total_traced >= len(V_boundary):
                    # Ring/annulus topology: two boundary loops (inner + outer extrusion ring)
                    # Identify the outer boundary as the one NOT shared with any other loop_id disk
                    comp0, comp1 = all_components[0], all_components[1]
                    comp0_set = set(comp0)
                    comp1_set = set(comp1)
                    
                    # The inner boundary shares verts with other loop_ids' territory
                    # The outer boundary has edges that are pure wire or open boundary
                    def component_shares_with_other_loops(comp_set):
                        for v in comp_set:
                            for f in v.link_faces:
                                if f[grid_layer] > 0 and f[grid_layer] != lid:
                                    return True
                        return False
                    
                    comp0_shared = component_shares_with_other_loops(comp0_set)
                    comp1_shared = component_shares_with_other_loops(comp1_set)
                    
                    if comp0_shared and not comp1_shared:
                        inner_verts, outer_verts = comp0, comp1
                    elif comp1_shared and not comp0_shared:
                        inner_verts, outer_verts = comp1, comp0
                    else:
                        # Fallback: larger component is outer
                        if len(comp0) >= len(comp1):
                            outer_verts, inner_verts = comp0, comp1
                        else:
                            outer_verts, inner_verts = comp1, comp0
                    
                    loops.append({
                        'type': 'ring',
                        'loop_id': lid,
                        'verts': outer_verts,       # outer boundary (drives subdivision)
                        'inner_verts': inner_verts,  # inner boundary (shared with inner disk)
                        'faces': F_loop
                    })
                    

        # 2. Find all unrasterized loops (wire loops) and open paths (line segments)
        wire_edges = [e for e in bm.edges if len(e.link_faces) == 0]
        if wire_edges:
            adj = {}
            for e in wire_edges:
                for v in e.verts:
                    adj.setdefault(v, []).append(e)
                    
            visited = set()
            for v in list(adj.keys()):
                if v in visited:
                    continue
                    
                comp_verts = []
                comp_edges = set()
                queue = [v]
                visited.add(v)
                
                while queue:
                    curr = queue.pop(0)
                    comp_verts.append(curr)
                    
                    for e in adj[curr]:
                        comp_edges.add(e)
                        v2 = e.other_vert(curr)
                        if v2 not in visited:
                            visited.add(v2)
                            queue.append(v2)
                
                # Analyze degrees of vertices in the component
                degrees = {cv: len(adj[cv]) for cv in comp_verts}
                max_degree = max(degrees.values()) if degrees else 0
                
                # Check if it's a simple closed loop (all vertices degree 2)
                is_simple_loop = all(d == 2 for d in degrees.values())
                
                if is_simple_loop and len(comp_verts) >= 3:
                    ordered_verts = self.trace_boundary_loop_from_edges(bm, comp_edges)
                    if ordered_verts:
                        loops.append({
                            'type': 'unrasterized',
                            'verts': ordered_verts
                        })
                elif max_degree <= 2:
                    # Check if it's a simple open path (line segment)
                    deg1_verts = [cv for cv, d in degrees.items() if d == 1]
                    if len(deg1_verts) == 2:
                        start_v = deg1_verts[0]
                        ordered_verts = []
                        curr_v = start_v
                        prev_edge = None
                        path_visited = set()
                        
                        while curr_v and curr_v not in path_visited:
                            ordered_verts.append(curr_v)
                            path_visited.add(curr_v)
                            
                            next_edge = None
                            for e in adj[curr_v]:
                                if e != prev_edge and e in comp_edges:
                                    next_edge = e
                                    break
                            if not next_edge:
                                break
                            prev_edge = next_edge
                            curr_v = next_edge.other_vert(curr_v)
                            
                        if len(ordered_verts) >= 2:
                            loops.append({
                                'type': 'open_path',
                                'verts': ordered_verts
                            })
                else:
                    # Complex topology: spliced loops / branching network
                    # Decompose into arc segments between junction/endpoint vertices.
                    # Junction vertices: degree != 2 (endpoints deg=1, crossings deg>=3)
                    junction_verts = {cv for cv, d in degrees.items() if d != 2}
                    
                    # Trace each arc: start at a junction vertex, walk through
                    # degree-2 intermediate vertices until the next junction vertex.
                    arc_edge_visited = set()
                    for start_v in junction_verts:
                        for start_edge in adj.get(start_v, []):
                            if start_edge in arc_edge_visited or start_edge not in comp_edges:
                                continue
                            # Walk this arc
                            arc_verts = [start_v]
                            prev_edge = start_edge
                            arc_edge_visited.add(start_edge)
                            curr_v = start_edge.other_vert(start_v)
                            
                            while curr_v not in junction_verts:
                                arc_verts.append(curr_v)
                                # Find the next edge (degree-2 interior vertex has exactly 2 edges)
                                next_edge = None
                                for e in adj.get(curr_v, []):
                                    if e != prev_edge and e in comp_edges:
                                        next_edge = e
                                        break
                                if not next_edge:
                                    break
                                arc_edge_visited.add(next_edge)
                                prev_edge = next_edge
                                curr_v = next_edge.other_vert(curr_v)
                            
                            arc_verts.append(curr_v)  # append the terminal junction vert
                            
                            if len(arc_verts) >= 2:
                                loops.append({
                                    'type': 'open_path',
                                    'verts': arc_verts
                                })
                            
        return loops

    def handle_loop_subdivision(self, context, event=None):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj or topo_obj.mode != 'EDIT':
            return False
            
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        
        # 1. Initialize the original loops cache if this is the start of a scroll sequence
        if getattr(self, 'subdiv_original_loops', None) is None:
            loops = self.find_all_loops(bm)
            if not loops:
                self.report({'INFO'}, "未检测到任何已绘制的栅格、圈或线段，无法细分")
                return True
                
            # Count occurrences of each vertex across all loops to find splicing points
            vert_loop_count = {}
            for loop in loops:
                for v in loop['verts']:
                    vert_loop_count[v] = vert_loop_count.get(v, 0) + 1
            
            self.subdiv_original_loops = []
            for loop in loops:
                splicing_indices = set()
                for idx, v in enumerate(loop['verts']):
                    if vert_loop_count[v] > 1:
                        splicing_indices.add(idx)
                        
                entry = {
                    'type': loop['type'],
                    'coords': [v.co.copy() for v in loop['verts']],
                    'splicing_indices': splicing_indices,
                    'original_count': len(loop['verts']),
                    'loop_id': loop.get('loop_id', None)
                }
                # For ring loops, also save the inner boundary coords
                if loop['type'] == 'ring' and 'inner_verts' in loop:
                    entry['inner_coords'] = [v.co.copy() for v in loop['inner_verts']]
                    entry['inner_count'] = len(loop['inner_verts'])
                self.subdiv_original_loops.append(entry)
            self.subdiv_multiplier = 1.0

            
        # 2. Update the subdivision multiplier
        prev_multiplier = getattr(self, 'subdiv_multiplier', 1.0)
        if event is not None:
            if event.type == 'WHEELUPMOUSE':
                self.subdiv_multiplier += 0.1
            else:
                # Check if any loop can still be unsubdivided (i.e. has more edges than its minimum limit)
                # Skip ring loops - their density follows the inner disk loop
                can_unsubdivide = False
                for orig_loop in self.subdiv_original_loops:
                    if orig_loop['type'] == 'ring':
                        continue
                    orig_count = orig_loop['original_count']
                    splicing_indices = orig_loop.get('splicing_indices', set())
                    is_open = (orig_loop['type'] == 'open_path')
                    orig_edges = (orig_count - 1) if is_open else orig_count
                    
                    current_edges = orig_edges * self.subdiv_multiplier
                    if orig_loop['type'] == 'rasterized':
                        current_edges = round(current_edges / 2) * 2
                    else:
                        current_edges = round(current_edges)
                        
                    if orig_loop['type'] == 'open_path':
                        S_len = len(splicing_indices | {0, orig_count - 1})
                        num_segs = S_len - 1
                        min_allowed = max(2, num_segs + 1)
                    else:
                        num_segs = len(splicing_indices) if splicing_indices else 1
                        base_min = 8 if orig_loop['type'] == 'rasterized' else 4
                        min_allowed = max(base_min, num_segs)
                        
                    min_allowed_edges = (min_allowed - 1) if is_open else min_allowed
                    
                    if current_edges > min_allowed_edges:
                        can_unsubdivide = True
                        break
                        
                if not can_unsubdivide:
                    self.report({'INFO'}, "已达到最小细分限制，无法继续反细分")
                    return True
                    
                self.subdiv_multiplier -= 0.1
                if self.subdiv_multiplier < 0.1:
                    self.subdiv_multiplier = 0.1
            
        # Backup the BMesh in case recreation or grid-fill fails
        bm_backup = bm.copy()
            
        # 3. Calculate new counts and verify if any loop is valid
        # Skip ring loops — they are rebuilt by bridging inner/outer boundaries later
        loop_updates = []
        for orig_loop in self.subdiv_original_loops:
            if orig_loop['type'] == 'ring':
                continue
            orig_count = orig_loop['original_count']
            splicing_indices = orig_loop.get('splicing_indices', set())
            
            if orig_loop['type'] == 'open_path':
                S_len = len(splicing_indices | {0, orig_count - 1})
                num_segs = S_len - 1
                min_allowed = max(2, num_segs + 1)
            else:
                num_segs = len(splicing_indices) if splicing_indices else 1
                base_min = 8 if orig_loop['type'] == 'rasterized' else 4
                min_allowed = max(base_min, num_segs)
                
            is_open = (orig_loop['type'] == 'open_path')
            orig_edges = (orig_count - 1) if is_open else orig_count
            new_edges = orig_edges * self.subdiv_multiplier
            
            if orig_loop['type'] == 'rasterized':
                new_edges = round(new_edges / 2) * 2
            else:
                new_edges = round(new_edges)
                
            min_allowed_edges = (min_allowed - 1) if is_open else min_allowed
            if new_edges < min_allowed_edges:
                new_edges = min_allowed_edges
                
            new_count = (new_edges + 1) if is_open else new_edges

                
            loop_updates.append({
                'orig_loop': orig_loop,
                'new_count': new_count
            })
            
        # Find all current loops in the BMesh to delete them
        current_loops = self.find_all_loops(bm)
        if not current_loops:
            self.subdiv_original_loops = None
            bm_backup.free()
            return True
            
        def get_segment_coords(coords, start_idx, end_idx):
            N = len(coords)
            seg_coords = []
            for idx in range(start_idx, end_idx + 1):
                actual_idx = idx % N
                seg_coords.append(coords[actual_idx].copy())
            return seg_coords

        # 4. Generate resampled coordinates from the ORIGINAL loop coordinates
        for update in loop_updates:
            orig_loop = update['orig_loop']
            new_count = update['new_count']
            
            P = orig_loop['coords']
            N = len(P)
            splicing_indices = orig_loop.get('splicing_indices', set())
            is_open = (orig_loop['type'] == 'open_path')
            
            if is_open:
                S = sorted(list(splicing_indices | {0, N - 1}))
            else:
                if not splicing_indices:
                    S = [0, N]
                else:
                    sorted_splits = sorted(list(splicing_indices))
                    S = sorted_splits + [sorted_splits[0] + N]
            
            segments = []
            total_len = 0.0
            for i in range(len(S) - 1):
                start_idx = S[i]
                end_idx = S[i+1]
                seg_coords = get_segment_coords(P, start_idx, end_idx)
                
                dists = [0.0]
                for j in range(len(seg_coords) - 1):
                    dists.append(dists[-1] + (seg_coords[j+1] - seg_coords[j]).length)
                seg_len = dists[-1]
                
                segments.append({
                    'coords': seg_coords,
                    'dists': dists,
                    'length': seg_len
                })
                total_len += seg_len
                
            target_edges = (new_count - 1) if is_open else new_count
            num_segs = len(segments)
            
            if total_len > 1e-5:
                raw_edges = [target_edges * (seg['length'] / total_len) for seg in segments]
                edges = [max(1, round(re)) for re in raw_edges]
            else:
                edges = [max(1, target_edges // num_segs)] * num_segs
                
            diff = target_edges - sum(edges)
            while diff != 0:
                if diff > 0:
                    best_idx = -1
                    best_val = -1e9
                    for idx in range(num_segs):
                        val = (raw_edges[idx] - edges[idx]) if total_len > 1e-5 else 0
                        if val > best_val:
                            best_val = val
                            best_idx = idx
                    edges[best_idx] += 1
                    diff -= 1
                else:
                    best_idx = -1
                    best_val = -1e9
                    for idx in range(num_segs):
                        if edges[idx] > 1:
                            val = (edges[idx] - raw_edges[idx]) if total_len > 1e-5 else 0
                            if val > best_val:
                                best_val = val
                                best_idx = idx
                    if best_idx == -1:
                        break
                    edges[best_idx] -= 1
                    diff += 1
                    
            resampled_segments = []
            for i, seg in enumerate(segments):
                seg_coords = seg['coords']
                seg_dists = seg['dists']
                seg_len = seg['length']
                seg_edges = edges[i]
                
                new_seg_coords = []
                for j in range(seg_edges + 1):
                    target_d = (j / seg_edges) * seg_len if seg_edges > 0 and seg_len > 0.0 else 0.0
                    idx = 0
                    while idx < len(seg_dists) - 1 and seg_dists[idx+1] < target_d:
                        idx += 1
                    d0 = seg_dists[idx]
                    d1 = seg_dists[idx+1]
                    s_len = d1 - d0
                    if s_len > 1e-5:
                        factor = (target_d - d0) / s_len
                    else:
                        factor = 0.0
                    p_new = seg_coords[idx] + factor * (seg_coords[idx+1] - seg_coords[idx])
                    new_seg_coords.append(p_new)
                resampled_segments.append(new_seg_coords)
                
            new_coords = []
            for i, seg_resampled in enumerate(resampled_segments):
                if i == 0:
                    new_coords.extend(seg_resampled)
                else:
                    new_coords.extend(seg_resampled[1:])
            if not is_open and len(new_coords) > 1:
                new_coords.pop()
                
            update['new_coords'] = new_coords
            
        # Calculate predicted edge length to verify if it goes below 0.05
        if event is not None:
            predicted_loop_edge_lens = []
            for update in loop_updates:
                new_count = update['new_count']
                P = update['new_coords']
                perimeter = 0.0
                is_open = (update['orig_loop']['type'] == 'open_path')
                
                for i in range(len(P)):
                    p1 = P[i]
                    if is_open:
                        if i == len(P) - 1:
                            continue
                        p2 = P[i + 1]
                    else:
                        p2 = P[(i + 1) % len(P)]
                    perimeter += (p2 - p1).length
                    
                if new_count > 0:
                    edge_count = (new_count - 1) if is_open else new_count
                    if edge_count > 0:
                        predicted_loop_edge_lens.append(perimeter / edge_count)
                    
            if predicted_loop_edge_lens:
                predicted_avg_edge_len = sum(predicted_loop_edge_lens) / len(predicted_loop_edge_lens)
                if predicted_avg_edge_len < 0.05 and self.subdiv_multiplier > prev_multiplier:
                    # Revert the multiplier
                    self.subdiv_multiplier = prev_multiplier
                    
                    bm_backup.free()
                    self.report({'WARNING'}, "滚轮调整边长已停止（最低0.05），低于0.05请使用侧栏面板的“边长”进行微调。")
                    context.area.tag_redraw()
                    return True
            
        # Collect ring loop data BEFORE deletion (save outer boundary coordinates)
        ring_loop_data = []
        for orig_loop in self.subdiv_original_loops:
            if orig_loop['type'] != 'ring':
                continue
            # Resample the outer boundary to match the new inner loop vertex count.
            # The outer boundary drives the outer shape of the ring.
            outer_coords = orig_loop['coords']  # outer boundary original coords
            inner_coords = orig_loop.get('inner_coords', [])  # inner boundary original coords
            loop_id = orig_loop.get('loop_id', None)
            
            # Determine the target count for the ring's outer boundary.
            # Find the corresponding inner disk's new_count from loop_updates
            # (the inner disk is the rasterized loop whose outer boundary = ring's inner boundary).
            # As a heuristic, we set outer boundary count = inner boundary new count.
            # First: find inner disk loop_update that shares this ring's inner boundary.
            # Fallback: resample outer boundary to have same count as inner boundary's new count.
            # We'll compute by using the inner boundary original count and current multiplier.
            inner_orig_count = orig_loop.get('inner_count', len(inner_coords))
            outer_orig_count = len(outer_coords)
            
            # Determine target outer count: same ratio as inner (preserve ring density ratio)
            inner_new_edges = round(inner_orig_count * self.subdiv_multiplier)
            inner_new_edges = max(4, inner_new_edges)
            # Make inner_new_edges even for rasterized compatibility
            inner_new_edges = round(inner_new_edges / 2) * 2
            outer_new_count = inner_new_edges  # ring outer boundary matches inner boundary count
            
            # Resample outer boundary to outer_new_count points (closed loop)
            def resample_closed_loop(coords, target_count):
                if target_count < 3 or not coords:
                    return list(coords)
                P = coords
                N = len(P)
                dists = [0.0]
                for i in range(N):
                    p1 = P[i]
                    p2 = P[(i + 1) % N]
                    dists.append(dists[-1] + (p2 - p1).length)
                total = dists[-1]
                if total < 1e-8:
                    return [P[0].copy()] * target_count
                result = []
                for j in range(target_count):
                    td = (j / target_count) * total
                    idx = 0
                    while idx < len(dists) - 1 and dists[idx + 1] < td:
                        idx += 1
                    d0 = dists[idx]
                    d1 = dists[idx + 1] if idx + 1 < len(dists) else dists[-1]
                    seg_len = d1 - d0
                    if seg_len > 1e-8:
                        f = (td - d0) / seg_len
                    else:
                        f = 0.0
                    p1 = P[idx % N]
                    p2 = P[(idx + 1) % N]
                    result.append(p1 + f * (p2 - p1))
                return result

            resampled_outer = resample_closed_loop(outer_coords, outer_new_count)
            
            ring_loop_data.append({
                'loop_id': loop_id,
                'outer_new_count': outer_new_count,
                'resampled_outer': resampled_outer,
                'inner_orig_count': inner_orig_count,
                'inner_new_count': inner_new_edges,
            })

        # 5. Delete all current loops in the BMesh
        # For ring loops: also delete their faces (their verts get destroyed when inner disk verts are deleted)
        bm.select_history.clear()
        verts_to_delete = set()
        for loop in current_loops:
            if loop['type'] == 'rasterized':
                for f in loop['faces']:
                    if f.is_valid:
                        verts_to_delete.update(f.verts)
            elif loop['type'] == 'ring':
                # Delete ring faces by deleting all their unique verts
                # (inner boundary shared with inner disk will be deleted via inner disk deletion)
                for f in loop['faces']:
                    if f.is_valid:
                        verts_to_delete.update(f.verts)
            else:
                for v in loop['verts']:
                    if v.is_valid:
                        verts_to_delete.add(v)
                        
        bmesh.ops.delete(bm, geom=list(verts_to_delete), context='VERTS')
        
        # 6. Recreate the loops with their resampled coordinates
        created_loops = []
        for update in loop_updates:
            coords = update['new_coords']
            orig_loop = update['orig_loop']
            
            new_verts = []
            for co in coords:
                v = bm.verts.new(co)
                new_verts.append(v)
            bm.verts.ensure_lookup_table()
            
            new_edges = []
            if orig_loop['type'] == 'open_path':
                for i in range(len(new_verts) - 1):
                    v1 = new_verts[i]
                    v2 = new_verts[i + 1]
                    e = bm.edges.new((v1, v2))
                    new_edges.append(e)
            else:
                for i in range(len(new_verts)):
                    v1 = new_verts[i]
                    v2 = new_verts[(i + 1) % len(new_verts)]
                    e = bm.edges.new((v1, v2))
                    new_edges.append(e)
            bm.edges.ensure_lookup_table()
            
            # NOTE: indices are captured BEFORE update_edit_mesh — they may be
            # -1 for newly-created verts. We store the bmesh vert objects
            # directly so we can re-resolve indices after the mesh is flushed.
            created_loops.append({
                'type': orig_loop['type'],
                'new_verts': new_verts,   # live BMVert refs, resolved after update
                'loop_id': orig_loop.get('loop_id', None)
            })
            
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
            
        bmesh.update_edit_mesh(topo_obj.data)
        # index_update() assigns stable indices to all verts (including newly
        # created ones which have index -1 until this is called).
        bm.verts.index_update()
        # Now that the mesh is flushed, resolve stable vert indices
        bm.verts.ensure_lookup_table()
        for loop_info in created_loops:
            loop_info['vert_indices'] = [v.index for v in loop_info['new_verts'] if v.is_valid and v.index >= 0]
        self.rebuild_kd_tree()
            
        # 7. Grid-fill all rasterized loops in a SINGLE call so that spliced rings
        #    (which share boundary vertices) are filled together as a coordinated group.
        #    Filling them one-by-one fails for spliced pairs: after the first ring is
        #    filled, its shared-boundary verts are no longer on any wire outer edge, so
        #    analyze_selection cannot find the second ring's boundary.
        grid_fill_success = True
        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = topo_obj
        
        rasterized_loops = [li for li in created_loops if li['type'] == 'rasterized']
        if rasterized_loops:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            # Clear all selection first
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
            for f in bm.faces:
                f.select = False
            
            # Select vertices from ALL rasterized loops at once
            all_selected_verts = set()
            for loop_info in rasterized_loops:
                for idx in loop_info['vert_indices']:
                    if idx >= 0 and idx < len(bm.verts):
                        bm.verts[idx].select = True
                        all_selected_verts.add(bm.verts[idx])
            
            for e in bm.edges:
                if e.verts[0] in all_selected_verts and e.verts[1] in all_selected_verts:
                    e.select = True
            
            bmesh.update_edit_mesh(topo_obj.data)
            
            try:
                res = bpy.ops.object.tp_topology_grid_fill()
                if 'FINISHED' not in res:
                    grid_fill_success = False
            except Exception as e:
                print("Error running grid fill during global subdivision:", e)
                grid_fill_success = False

        # 7.5 Rebuild ring loops (boundary mode extrusion outer rings)
        # After the inner disk is grid-filled, find the inner disk's outer perimeter
        # and create new ring faces bridging it to the resampled outer boundary.
        if ring_loop_data and grid_fill_success:
            try:
                bm = bmesh.from_edit_mesh(topo_obj.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                grid_layer = bm.faces.layers.int.get("tp_is_grid")
                if not grid_layer:
                    grid_layer = bm.faces.layers.int.new("tp_is_grid")
                
                for rld in ring_loop_data:
                    ring_lid = rld['loop_id']
                    inner_new_count = rld['inner_new_count']
                    resampled_outer = rld['resampled_outer']
                    outer_new_count = rld['outer_new_count']
                    
                    if not ring_lid or not resampled_outer:
                        continue
                    
                    # Find the inner disk loop_id that the ring was adjacent to.
                    # The inner disk's faces share boundary with the (now-deleted) ring.
                    # Identify the inner disk's outer boundary (perimeter) verts.
                    # Strategy: scan all non-ring filled loops and find the boundary component
                    # whose count is closest to inner_new_count.
                    
                    # Scan all filled rasterized loops (any lid != ring_lid and lid > 0)
                    filled_lids = set()
                    for f in bm.faces:
                        lid = f[grid_layer]
                        if lid > 0 and lid != ring_lid:
                            filled_lids.add(lid)

                    
                    best_inner_verts = []
                    for disk_lid in sorted(filled_lids):
                        F_disk = [f for f in bm.faces if f[grid_layer] == disk_lid]
                        if not F_disk:
                            continue
                        E_disk_all = set()
                        for f in F_disk:
                            E_disk_all.update(f.edges)
                        E_disk_boundary = {e for e in E_disk_all if len([f for f in e.link_faces if f[grid_layer] == disk_lid]) == 1}
                        components = self.trace_all_boundary_components(E_disk_boundary)
                        if components:
                            # Use the component with count closest to inner_new_count
                            for comp in components:
                                if abs(len(comp) - inner_new_count) < abs(len(best_inner_verts) - inner_new_count):
                                    best_inner_verts = comp
                    
                    if not best_inner_verts or not resampled_outer:
                        continue
                    
                    inner_ring_verts = best_inner_verts
                    n_inner = len(inner_ring_verts)
                    n_outer = len(resampled_outer)
                    
                    # Resample resampled_outer to match n_inner if needed
                    if n_outer != n_inner:
                        resampled_outer = resample_closed_loop(resampled_outer, n_inner)
                        n_outer = n_inner
                    
                    # Create outer boundary verts
                    outer_new_verts = []
                    for co in resampled_outer:
                        v = bm.verts.new(co)
                        outer_new_verts.append(v)
                    bm.verts.ensure_lookup_table()
                    
                    # Create edges for outer boundary
                    for i in range(n_outer):
                        v1 = outer_new_verts[i]
                        v2 = outer_new_verts[(i + 1) % n_outer]
                        try:
                            bm.edges.new((v1, v2))
                        except Exception:
                            pass
                    bm.edges.ensure_lookup_table()
                    
                    # Find the alignment offset between inner_ring_verts and outer_new_verts
                    # by minimizing the total distance between corresponding verts.
                    min_total_dist = float('inf')
                    best_offset = 0
                    for offset in range(n_inner):
                        total_dist = sum(
                            (inner_ring_verts[(j + offset) % n_inner].co - outer_new_verts[j].co).length
                            for j in range(n_inner)
                        )
                        if total_dist < min_total_dist:
                            min_total_dist = total_dist
                            best_offset = offset
                    
                    # Create quad faces bridging inner boundary to outer boundary
                    new_ring_faces = []
                    for i in range(n_inner):
                        v_inner_a = inner_ring_verts[(i + best_offset) % n_inner]
                        v_inner_b = inner_ring_verts[(i + 1 + best_offset) % n_inner]
                        v_outer_a = outer_new_verts[i]
                        v_outer_b = outer_new_verts[(i + 1) % n_outer]
                        try:
                            # Validate all 4 verts are distinct and valid
                            if len({v_inner_a, v_inner_b, v_outer_a, v_outer_b}) == 4:
                                # Check if face already exists
                                existing = [f for f in v_inner_a.link_faces if set(f.verts) == {v_inner_a, v_inner_b, v_outer_a, v_outer_b}]
                                if not existing:
                                    new_f = bm.faces.new((v_inner_a, v_outer_a, v_outer_b, v_inner_b))
                                    new_ring_faces.append(new_f)
                        except Exception as fe:
                            print(f"Ring face creation error at index {i}: {fe}")
                    
                    bm.faces.ensure_lookup_table()
                    
                    # Assign the ring loop_id to all new ring faces
                    for nf in new_ring_faces:
                        if nf.is_valid:
                            nf[grid_layer] = ring_lid
                    
                    # Weld the outer boundary to any existing geometry
                    try:
                        bmesh.ops.remove_doubles(bm, verts=list(set(outer_new_verts + inner_ring_verts)), dist=0.001)
                    except Exception:
                        pass
                
                bmesh.update_edit_mesh(topo_obj.data)
                self.rebuild_kd_tree()
            except Exception as e:
                print("Error rebuilding ring loops during subdivision:", e)

        # Restore the previously active object
        if prev_active is not None:
            try:
                context.view_layer.objects.active = prev_active
            except Exception:
                pass
                    
        if not grid_fill_success:
            # Restore BMesh from backup.
            # bm_backup.to_mesh() raises ValueError when the mesh is in edit
            # mode, so we route through a temporary mesh that is never linked
            # to edit mode, then rebuild the live edit bmesh from it.
            temp_mesh = bpy.data.meshes.new("_tp_backup_restore_tmp")
            try:
                bm_backup.to_mesh(temp_mesh)
                bm_restore = bmesh.from_edit_mesh(topo_obj.data)
                bm_restore.clear()
                bm_restore.from_mesh(temp_mesh)
                bmesh.update_edit_mesh(topo_obj.data)
            finally:
                bpy.data.meshes.remove(temp_mesh)
                bm_backup.free()
            
            # Revert the multiplier
            if event is not None:
                if event.type == 'WHEELUPMOUSE':
                    self.subdiv_multiplier -= 0.1
                else:
                    self.subdiv_multiplier += 0.1
                
            self.rebuild_kd_tree()
            self.report({'INFO'}, "由于网格填充失败，已自动撤销该细分级别")
            context.area.tag_redraw()
            return True
            
        # Free the backup BMesh since everything succeeded
        bm_backup.free()
                    
        # 8. Select all boundary vertices of all created loops and weld them
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.001)
        bm.verts.ensure_lookup_table()
        
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
            
        bmesh.update_edit_mesh(topo_obj.data)
        self.rebuild_kd_tree()

        
        # Push undo AFTER grid fill and remove_doubles so that Ctrl+Z restores
        # the fully-filled state (not the intermediate wire-loop state).
        try:
            bpy.ops.ed.undo_push(message="TP 全局循环圈细分")
        except Exception as e:
            print("Error pushing undo step during global subdivision:", e)
        
        # 9. Update the edge length parameter in the scene / N-panel
        total_loop_edge_lens = []
        for update in loop_updates:
            new_count = update['new_count']
            P = update['new_coords']
            perimeter = 0.0
            is_open = (update['orig_loop']['type'] == 'open_path')
            
            for i in range(len(P)):
                p1 = P[i]
                if is_open:
                    if i == len(P) - 1:
                        continue
                    p2 = P[i + 1]
                else:
                    p2 = P[(i + 1) % len(P)]
                perimeter += (p2 - p1).length
                
            if new_count > 0:
                edge_count = (new_count - 1) if is_open else new_count
                if edge_count > 0:
                    total_loop_edge_lens.append(perimeter / edge_count)
                
        if total_loop_edge_lens:
            avg_edge_len = sum(total_loop_edge_lens) / len(total_loop_edge_lens)
            context.scene.tp_edge_length = avg_edge_len
            
        action = "参数调整" if event is None else ("细分" if event.type == 'WHEELUPMOUSE' else "反细分")
        self.report({'INFO'}, f"已对所有栅格、线段和圈进行全局{action}，当前密度比例：{self.subdiv_multiplier:.2f}")
        context.area.tag_redraw()
        return True

    def check_line_crosses_ref_mesh(self, ref_obj, pt1_world, pt2_world):
        matrix_inverse = ref_obj.matrix_world.inverted()
        pt1_local = matrix_inverse @ pt1_world
        pt2_local = matrix_inverse @ pt2_world
        
        dir_local = pt2_local - pt1_local
        dist = dir_local.length
        if dist < 0.001:
            return False
        
        dir_norm = dir_local.normalized()
        epsilon = 0.002
        if dist <= 2 * epsilon:
            return False
            
        start_pt = pt1_local + dir_norm * epsilon
        ray_dist = dist - 2 * epsilon
        
        try:
            success, location, normal, face_idx = ref_obj.ray_cast(start_pt, dir_norm, distance=ray_dist)
            if success:
                return True
        except Exception:
            pass
            
        # Reverse check for symmetry and robustness
        start_pt_rev = pt2_local - dir_norm * epsilon
        try:
            success, location, normal, face_idx = ref_obj.ray_cast(start_pt_rev, -dir_norm, distance=ray_dist)
            if success:
                return True
        except Exception:
            pass
            
        return False

    def is_point_occluded(self, context, ref_obj, pt_world, region, rv3d):
        screen_coord = location_3d_to_region_2d(region, rv3d, pt_world)
        if not screen_coord:
            return True
            
        ray_origin = region_2d_to_origin_3d(region, rv3d, screen_coord)
        
        matrix_inverse = ref_obj.matrix_world.inverted()
        ray_origin_local = matrix_inverse @ ray_origin
        pt_local = matrix_inverse @ pt_world
        
        dir_local = pt_local - ray_origin_local
        dist = dir_local.length
        if dist < 0.001:
            return False
            
        dir_norm = dir_local.normalized()
        epsilon = 0.005
        if dist <= epsilon:
            return False
            
        ray_dist = dist - epsilon
        
        depsgraph = context.evaluated_depsgraph_get()
        try:
            success, location, normal, face_idx = ref_obj.ray_cast(
                ray_origin_local,
                dir_norm,
                distance=ray_dist,
                depsgraph=depsgraph
            )
            if success:
                return True
        except Exception:
            try:
                success, location, normal, face_idx = ref_obj.ray_cast(
                    ray_origin_local,
                    dir_norm,
                    distance=ray_dist
                )
                if success:
                    return True
            except Exception:
                pass
        return False

    def handle_grab_modal(self, context, event):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        
        if not topo_obj or not ref_obj or topo_obj.mode != 'EDIT':
            self.is_grabbing = False
            self.hover_snap_pt = None
            return {'PASS_THROUGH'}

        region = context.region
        rv3d = context.space_data.region_3d
        
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}
            
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            if getattr(self, 'grab_backup_bm', None):
                temp_mesh = bpy.data.meshes.new("_tp_grab_restore_tmp")
                try:
                    self.grab_backup_bm.to_mesh(temp_mesh)
                    bm_restore = bmesh.from_edit_mesh(topo_obj.data)
                    bm_restore.clear()
                    bm_restore.from_mesh(temp_mesh)
                    bmesh.update_edit_mesh(topo_obj.data)
                finally:
                    bpy.data.meshes.remove(temp_mesh)
                    self.grab_backup_bm.free()
                    self.grab_backup_bm = None
            else:
                bm = bmesh.from_edit_mesh(topo_obj.data)
                bm.verts.ensure_lookup_table()
                for v_idx, initial_co in self.grab_initial_cos.items():
                    if v_idx < len(bm.verts):
                        bm.verts[v_idx].co = initial_co
                bmesh.update_edit_mesh(topo_obj.data)
                
            self.is_grabbing = False
            self.is_dragging_grab = False
            self.hover_snap_pt = None
            self.grab_snap_target_idx = None
            self.report({'INFO'}, "已取消移动")
            context.workspace.status_text_set("TP拓扑模式 | Ctrl+left-click drag: continuous draw | Ctrl+left-click single: draw segment | Alt+left-click: select loop | right-click/Enter: submit | ESC to exit")
            context.area.tag_redraw()
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and getattr(self, 'is_dragging_grab', False) and not getattr(self, 'grab_dragged', False):
            # Restore original coordinates since no dragging occurred (click to select)
            if getattr(self, 'grab_backup_bm', None):
                temp_mesh = bpy.data.meshes.new("_tp_grab_restore_tmp")
                try:
                    self.grab_backup_bm.to_mesh(temp_mesh)
                    bm_restore = bmesh.from_edit_mesh(topo_obj.data)
                    bm_restore.clear()
                    bm_restore.from_mesh(temp_mesh)
                    bmesh.update_edit_mesh(topo_obj.data)
                finally:
                    bpy.data.meshes.remove(temp_mesh)
                    self.grab_backup_bm.free()
                    self.grab_backup_bm = None
            else:
                bm = bmesh.from_edit_mesh(topo_obj.data)
                bm.verts.ensure_lookup_table()
                for v_idx, initial_co in self.grab_initial_cos.items():
                    if v_idx < len(bm.verts):
                        bm.verts[v_idx].co = initial_co
                bmesh.update_edit_mesh(topo_obj.data)
                
            self.is_grabbing = False
            self.is_dragging_grab = False
            self.hover_snap_pt = None
            self.grab_snap_target_idx = None
            
            # Perform selection instead
            self.perform_loop_selection(context, self.grab_mouse_start, event.shift)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        is_confirm = False
        if getattr(self, 'is_dragging_grab', False):
            if (event.type == 'LEFTMOUSE' and event.value == 'RELEASE') or (event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS'):
                is_confirm = True
        else:
            if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
                is_confirm = True
                
        if is_confirm:
            import os
            debug_path = r"C:\Users\12801\.gemini\antigravity\brain\d401ec69-7e15-445d-83b5-48930a3c0a10\scratch\debug_weld.txt"
            os.makedirs(os.path.dirname(debug_path), exist_ok=True)
            with open(debug_path, "a", encoding="utf-8") as debug_file:
                debug_file.write(f"\n--- Grab Confirm ---\n")
                debug_file.write(f"is_dragging_grab: {getattr(self, 'is_dragging_grab', False)}\n")
                debug_file.write(f"grab_active_vert_idx: {self.grab_active_vert_idx}\n")
                debug_file.write(f"grab_snap_target_idx: {self.grab_snap_target_idx}\n")
            
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            
            if self.grab_snap_target_idx is not None:
                if (self.grab_active_vert_idx is not None and self.grab_active_vert_idx < len(bm.verts) and
                    self.grab_snap_target_idx < len(bm.verts)):
                    active_v = bm.verts[self.grab_active_vert_idx]
                    target_v = bm.verts[self.grab_snap_target_idx]
                    
                    with open(debug_path, "a", encoding="utf-8") as debug_file:
                        debug_file.write(f"active_v.is_valid: {active_v.is_valid}, target_v.is_valid: {target_v.is_valid}\n")
                        debug_file.write(f"active_v != target_v: {active_v != target_v}\n")
                    
                    if active_v.is_valid and target_v.is_valid and active_v != target_v:
                        pin_layer = bm.verts.layers.int.get("tp_is_pinned")
                        co_layer = bm.verts.layers.float_vector.get("tp_pinned_co")
                        is_pinned = False
                        if pin_layer:
                            if active_v[pin_layer] == 1 or target_v[pin_layer] == 1:
                                is_pinned = True
                        
                        active_v.co = target_v.co.copy()
                        try:
                            bmesh.ops.weld_verts(bm, targetmap={active_v: target_v})
                            with open(debug_path, "a", encoding="utf-8") as debug_file:
                                debug_file.write("Weld verts operator completed successfully\n")
                            if is_pinned and pin_layer:
                                target_v[pin_layer] = 1
                                if co_layer:
                                    target_v[co_layer] = target_v.co.copy()
                        except Exception as e:
                            with open(debug_path, "a", encoding="utf-8") as debug_file:
                                debug_file.write(f"Weld verts error: {str(e)}\n")
                            print("Weld verts error:", e)
                        
            try:
                d = context.scene.tp_edge_length
                weld_dist = d * 0.45
                bm.verts.ensure_lookup_table()
                inner_ring_verts = self.get_ring_inner_vertices(bm)
                if context.scene.tp_boundary_mode:
                    # In boundary mode, only the explicitly snapped active vertex should merge (via weld_verts above).
                    # We do not run remove_doubles on all boundary vertices to prevent neighboring vertices (which move due to proportional drag influence) from merging.
                    pass
                else:
                    weld_verts = [v for v in bm.verts if v not in inner_ring_verts]
                    bmesh.ops.remove_doubles(bm, verts=weld_verts, dist=weld_dist)
            except Exception as e:
                print("Remove doubles error:", e)
                
            bmesh.update_edit_mesh(topo_obj.data)
            
            # Rebuild/update the grids that are connected to the moved vertices in boundary mode
            if context.scene.tp_boundary_mode:
                try:
                    from .op_grid_fill import update_grids_for_vertices
                    update_grids_for_vertices(context, list(self.grab_initial_cos.keys()))
                except Exception as e:
                    print("Error updating grids for moved vertices:", e)
                    
            self.rebuild_kd_tree()
            
            self.subdiv_original_loops = None
            self.subdiv_multiplier = 1.0
            
            self.is_grabbing = False
            self.is_dragging_grab = False
            self.hover_snap_pt = None
            self.grab_snap_target_idx = None
            
            if getattr(self, 'grab_backup_bm', None):
                self.grab_backup_bm.free()
                self.grab_backup_bm = None
                
            try:
                bpy.ops.ed.undo_push(message="TP 移动与合并")
            except Exception as e:
                print("Error pushing undo step:", e)
                
            self.report({'INFO'}, "已确认位置并合并")
            context.workspace.status_text_set("TP拓扑模式 | Ctrl+left-click drag: continuous draw | Ctrl+left-click single: draw segment | Alt+left-click: select loop | right-click/Enter: submit | ESC to exit")
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        if event.type == 'MOUSEMOVE':
            mouse_coord = (event.mouse_region_x, event.mouse_region_y)
            
            if getattr(self, 'is_dragging_grab', False) and not getattr(self, 'grab_dragged', False):
                dx = event.mouse_region_x - self.grab_mouse_start[0]
                dy = event.mouse_region_y - self.grab_mouse_start[1]
                if (dx * dx + dy * dy) > 25:  # 5 pixels threshold
                    self.grab_dragged = True
                    if context.scene.tp_boundary_mode:
                        bm_select = bmesh.from_edit_mesh(topo_obj.data)
                        bm_select.verts.ensure_lookup_table()
                        bm_select.edges.ensure_lookup_table()
                        bm_select.faces.ensure_lookup_table()
                        for v in bm_select.verts:
                            v.select = False
                        for e in bm_select.edges:
                            e.select = False
                        for f in bm_select.faces:
                            f.select = False
                        if self.grab_active_vert_idx is not None and 0 <= self.grab_active_vert_idx < len(bm_select.verts):
                            dragged_v = bm_select.verts[self.grab_active_vert_idx]
                            dragged_v.select = True
                            bm_select.select_history.clear()
                            bm_select.select_history.add(dragged_v)
                        bmesh.update_edit_mesh(topo_obj.data)
                else:
                    return {'RUNNING_MODAL'}
            
            surface_pt = self.get_surface_point(context, mouse_coord)
            if surface_pt is not None:
                active_new_world = surface_pt
            else:
                ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
                ray_vector = region_2d_to_vector_3d(region, rv3d, mouse_coord)
                active_new_world = ray_origin + ray_vector * self.grab_initial_depth
                
            inv_topo_matrix = topo_obj.matrix_world.inverted()
            active_new_local = inv_topo_matrix @ active_new_world
            
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            
            if self.grab_active_vert_idx is None or self.grab_active_vert_idx >= len(bm.verts) or self.grab_active_vert_idx < 0:
                print(f"TP Grab Warning: self.grab_active_vert_idx ({self.grab_active_vert_idx}) is out of range. Cancelling grab.")
                self.is_grabbing = False
                self.is_dragging_grab = False
                self.hover_snap_pt = None
                self.grab_snap_target_idx = None
                context.workspace.status_text_set("TP拓扑模式 | Ctrl+left-click drag: continuous draw | Ctrl+left-click single: draw segment | Alt+left-click: select loop | right-click/Enter: submit | ESC to exit")
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            
            active_v = bm.verts[self.grab_active_vert_idx]
            if self.grab_active_vert_idx not in self.grab_initial_cos:
                self.grab_initial_cos[self.grab_active_vert_idx] = active_v.co.copy()
            active_start_local = self.grab_initial_cos[self.grab_active_vert_idx]
            
            delta_local = active_new_local - active_start_local
            
            pin_boundary = context.scene.tp_pin_boundary
            pin_layer = bm.verts.layers.int.get("tp_is_pinned")
            
            topo_world = topo_obj.matrix_world
            for v_idx, init_co in self.grab_initial_cos.items():
                if v_idx < len(bm.verts):
                    v = bm.verts[v_idx]
                    is_v_pinned = pin_layer and (v[pin_layer] == 1)
                    if is_v_pinned:
                        v.co = init_co
                    else:
                        weight = getattr(self, 'grab_weights', {}).get(v_idx, 1.0)
                        proposed_local = init_co + delta_local * weight
                        proposed_world = topo_world @ proposed_local
                        if is_point_in_mask(proposed_world, context):
                            clamped_world = clamp_to_mask_boundary(proposed_world, context)
                            v.co = inv_topo_matrix @ clamped_world
                        else:
                            v.co = proposed_local
            
            ref_matrix = ref_obj.matrix_world
            ref_inverse = ref_matrix.inverted()
            
            for v_idx in self.grab_initial_cos.keys():
                if v_idx < len(bm.verts):
                    v = bm.verts[v_idx]
                    if v_idx != self.grab_active_vert_idx:
                        is_v_pinned = pin_layer and (v[pin_layer] == 1)
                        if is_v_pinned:
                            continue
                        try:
                            world_co = topo_world @ v.co
                            local_target = ref_inverse @ world_co
                            success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                            if success:
                                local_pt = location + normal * 0.0005
                                proj_world = ref_matrix @ local_pt
                                if is_point_in_mask(proj_world, context):
                                    clamped_world = clamp_to_mask_boundary(proj_world, context)
                                    v.co = inv_topo_matrix @ clamped_world
                                else:
                                    v.co = inv_topo_matrix @ proj_world
                        except Exception:
                            pass
            
            self.grab_snap_target_idx = None
            self.hover_snap_pt = None
            
            is_active_pinned = pin_layer and (active_v[pin_layer] == 1)
            if is_active_pinned:
                active_projected_world = topo_world @ active_start_local
                active_v.co = active_start_local
            else:
                try:
                    world_co = topo_world @ active_new_local
                    local_target = ref_inverse @ world_co
                    success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                    if success:
                        local_pt = location + normal * 0.0005
                        candidate_world = ref_matrix @ local_pt
                    else:
                        candidate_world = world_co
                except Exception:
                    candidate_world = world_co
                    
                if not hasattr(self, 'last_valid_active_world'):
                    self.last_valid_active_world = (topo_world @ active_start_local).copy()

                if is_point_in_mask(candidate_world, context):
                    clamped_world = clamp_to_mask_boundary(candidate_world, context)
                    active_projected_world = clamped_world
                    self.last_valid_active_world = clamped_world.copy()
                else:
                    if context.scene.tp_boundary_mode:
                        if self.is_point_occluded(context, ref_obj, candidate_world, region, rv3d):
                            active_projected_world = self.last_valid_active_world
                        else:
                            active_projected_world = candidate_world
                            self.last_valid_active_world = candidate_world.copy()
                    else:
                        active_projected_world = candidate_world
                        self.last_valid_active_world = candidate_world.copy()
                    
                active_v.co = inv_topo_matrix @ active_projected_world
            
            if self.kd_tree:
                try:
                    nearest = self.kd_tree.find_n(candidate_world, 50)
                except Exception:
                    nearest = None
                    
                if nearest:
                    mouse_vec = mathutils.Vector(mouse_coord)
                    min_dist_px = 20.0
                    snap_candidate_idx = None
                    snap_candidate_co = None
                    
                    for co, idx, dist in nearest:
                        if idx == active_v.index:
                            continue
                            
                        # In boundary mode, do not snap to internal grid vertices, only to other white edge vertices
                        if context.scene.tp_boundary_mode:
                            if active_v.index in self.internal_grid_verts or idx in self.internal_grid_verts:
                                continue
                                
                        # Use target vertex's real-time coordinate from BMesh
                        if idx < len(bm.verts):
                            target_co_world = topo_world @ bm.verts[idx].co
                        else:
                            target_co_world = co
                            
                        screen_coord = location_3d_to_region_2d(region, rv3d, target_co_world)
                        if screen_coord:
                            dist_px = (screen_coord - mouse_vec).length
                            if dist_px < min_dist_px:
                                # Prevent snapping to vertices on the opposite side of the reference mesh
                                if context.scene.tp_boundary_mode and self.is_point_occluded(context, ref_obj, target_co_world, region, rv3d):
                                    continue
                                min_dist_px = dist_px
                                snap_candidate_idx = idx
                                snap_candidate_co = target_co_world
                                
                    if snap_candidate_idx is not None:
                        crosses = self.check_line_crosses_ref_mesh(ref_obj, snap_candidate_co, candidate_world)
                        if not crosses:
                            self.grab_snap_target_idx = snap_candidate_idx
                            self.hover_snap_pt = snap_candidate_co
                            active_v.co = inv_topo_matrix @ snap_candidate_co
                            
            bmesh.update_edit_mesh(topo_obj.data)
            
            if context.scene.tp_boundary_mode:
                try:
                    from .op_grid_fill import update_grids_for_vertices
                    update_grids_for_vertices(context, list(self.grab_initial_cos.keys()), is_interactive=True)
                except Exception as e:
                    print("Error updating grids for moved vertices in real-time:", e)
                    
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        return {'RUNNING_MODAL'}


# --- Robust Boundary Pinning Mechanism ---
_pinned_coords = {}          # Dictionary mapping vertex index to Vector
_pinned_vertex_count = 0    # Tracks total vertices to detect topology changes
_in_pin_handler = False      # Re-entrancy guard to prevent recursive updates
_updating_ui = False        # Guard to prevent UI property syncing from altering vertex data

def update_pinned_coordinates(context):
    """
    Finds all boundary vertices in the TP topology mesh and caches their current coordinates.
    """
    global _pinned_coords, _pinned_vertex_count
    _pinned_coords.clear()
    _pinned_vertex_count = 0
    
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not topo_obj:
        return
        
    import bmesh
    
    # Check mode and load geometry
    if topo_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        pin_layer = bm.verts.layers.int.get("tp_is_pinned")
        co_layer = bm.verts.layers.float_vector.get("tp_pinned_co")
        if pin_layer and co_layer:
            for v in bm.verts:
                if v[pin_layer] == 1:
                    _pinned_coords[v.index] = mathutils.Vector(v[co_layer])
        _pinned_vertex_count = len(bm.verts)
    else:
        # In Object, Sculpt, or other modes
        mesh = topo_obj.data
        pin_attr = mesh.attributes.get("tp_is_pinned")
        co_attr = mesh.attributes.get("tp_pinned_co")
        if pin_attr and co_attr:
            for i, v in enumerate(mesh.vertices):
                if pin_attr.data[i].value == 1:
                    _pinned_coords[i] = mathutils.Vector(co_attr.data[i].vector)
        _pinned_vertex_count = len(mesh.vertices)

def get_pin_target_verts(bm):
    """
    Returns the set of vertices that represent the boundaries of all loops
    (including outer boundaries and internal stitched borders of rasterized loops).
    """
    target_verts = set()
    target_edges = get_seam_target_edges(bm)
    for e in target_edges:
        target_verts.update(e.verts)
    return target_verts

def on_pin_boundary_update(self, context):
    """
    Callback triggered whenever context.scene.tp_pin_boundary is toggled.
    Initializes or clears the boundary coordinates cache and sets sculpt masks if appropriate.
    """
    global _pinned_coords, _pinned_vertex_count, _updating_ui
    if _updating_ui:
        return
        
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not topo_obj:
        return
        
    pin_active = context.scene.tp_pin_boundary
    import bmesh
    is_edit = (topo_obj.mode == 'EDIT')
    
    # Get selected vertices
    selected_indices = []
    if is_edit:
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        selected_indices = [v.index for v in bm.verts if v.select]
    else:
        selected_indices = [v.index for v in topo_obj.data.vertices if v.select]
        
    # Modify pin layer/attribute
    if is_edit:
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        pin_layer = bm.verts.layers.int.get("tp_is_pinned") or bm.verts.layers.int.new("tp_is_pinned")
        co_layer = bm.verts.layers.float_vector.get("tp_pinned_co") or bm.verts.layers.float_vector.new("tp_pinned_co")
        
        if not selected_indices:
            # Case A: No points selected - set/clear all boundary vertices of loops
            target_verts = get_pin_target_verts(bm)
            for v in bm.verts:
                if v in target_verts:
                    v[pin_layer] = 1 if pin_active else 0
                    v[co_layer] = v.co.copy()
                else:
                    v[pin_layer] = 0
        else:
            # Case B: Points selected - set/clear selected vertices only
            for idx in selected_indices:
                if idx < len(bm.verts):
                    v = bm.verts[idx]
                    v[pin_layer] = 1 if pin_active else 0
                    v[co_layer] = v.co.copy()
        bmesh.update_edit_mesh(topo_obj.data)
    else:
        mesh = topo_obj.data
        pin_attr = mesh.attributes.get("tp_is_pinned") or mesh.attributes.new(name="tp_is_pinned", type='INT', domain='POINT')
        co_attr = mesh.attributes.get("tp_pinned_co") or mesh.attributes.new(name="tp_pinned_co", type='FLOAT_VECTOR', domain='POINT')
        
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        
        if not selected_indices:
            # Case A: No points selected
            target_verts = get_pin_target_verts(bm)
            target_indices = {v.index for v in target_verts}
            for v in bm.verts:
                idx = v.index
                if idx in target_indices:
                    pin_attr.data[idx].value = 1 if pin_active else 0
                    co_attr.data[idx].vector = v.co.copy()
                else:
                    pin_attr.data[idx].value = 0
        else:
            # Case B: Points selected
            for idx in selected_indices:
                if idx < len(mesh.vertices):
                    pin_attr.data[idx].value = 1 if pin_active else 0
                    co_attr.data[idx].vector = mesh.vertices[idx].co.copy()
        bm.free()
        mesh.update()
        
    # Rebuild coordinates cache and update masks
    update_pinned_coordinates(context)
    
    # If in Sculpt mode, synchronize sculpt masks
    if topo_obj.mode == 'SCULPT':
        mesh = topo_obj.data
        mask_attr = mesh.attributes.get("mask") or mesh.attributes.new(name="mask", type='FLOAT', domain='POINT')
        for item in mask_attr.data:
            item.value = 0.0
        for idx in _pinned_coords.keys():
            if idx < len(mask_attr.data):
                mask_attr.data[idx].value = 1.0
        mesh.update()

def get_seam_target_edges(bm):
    """
    Returns the set of edges that represent the borders of all loops
    (including outer boundaries and internal stitched borders of rasterized loops).
    """
    target_edges = set()
    
    # 1. Wire edges (edges with no faces)
    for e in bm.edges:
        if len(e.link_faces) == 0:
            target_edges.add(e)
            
    # 2. Rasterized loop boundaries
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if grid_layer:
        # Group faces by loop_id
        faces_by_lid = {}
        for f in bm.faces:
            lid = f[grid_layer]
            if lid > 0:
                faces_by_lid.setdefault(lid, []).append(f)
                
        # For each loop, find its boundary edges
        for lid, faces in faces_by_lid.items():
            edges_of_lid = set()
            for f in faces:
                edges_of_lid.update(f.edges)
            for e in edges_of_lid:
                faces_sharing = [f for f in e.link_faces if f[grid_layer] == lid]
                if len(faces_sharing) == 1:
                    target_edges.add(e)
                    
    # 3. Standard boundary edges of the mesh
    for e in bm.edges:
        if e.is_boundary:
            target_edges.add(e)
            
    return target_edges

def on_seam_edge_update(self, context):
    """
    Callback triggered whenever context.scene.tp_seam_edge is toggled.
    Marks/clears edge seams on loop boundary edges (if no edges are selected) or selected edges.
    """
    global _updating_ui
    if _updating_ui:
        return
        
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not topo_obj:
        return
        
    seam_active = context.scene.tp_seam_edge
    import bmesh
    is_edit = (topo_obj.mode == 'EDIT')
    
    # Get selected edges
    selected_indices = []
    if is_edit:
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.edges.ensure_lookup_table()
        selected_indices = [e.index for e in bm.edges if e.select]
    else:
        selected_indices = [e.index for e in topo_obj.data.edges if e.select]
        
    # Modify seam property
    if is_edit:
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.edges.ensure_lookup_table()
        
        if not selected_indices:
            # Case A: No edges selected - set/clear all loop boundaries
            target_edges = get_seam_target_edges(bm)
            for e in bm.edges:
                if e in target_edges:
                    e.seam = seam_active
                else:
                    e.seam = False
        else:
            # Case B: Edges selected - set/clear selected edges only
            for idx in selected_indices:
                if idx < len(bm.edges):
                    bm.edges[idx].seam = seam_active
        bmesh.update_edit_mesh(topo_obj.data)
    else:
        mesh = topo_obj.data
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        
        if not selected_indices:
            # Case A: No edges selected
            target_edges = get_seam_target_edges(bm)
            target_indices = {e.index for e in target_edges}
            for e in bm.edges:
                idx = e.index
                if idx < len(mesh.edges):
                    mesh.edges[idx].use_seam = (idx in target_indices) and seam_active
        else:
            # Case B: Edges selected
            for idx in selected_indices:
                if idx < len(mesh.edges):
                    mesh.edges[idx].use_seam = seam_active
        bm.free()
        mesh.update()
        
    # Trigger viewport redraw
    if context.area:
        context.area.tag_redraw()

def tp_pin_depsgraph_handler(scene, depsgraph=None):
    """
    Global depsgraph update handler to enforce the boundary coordinates locking.
    Locks vertices in both Edit Mode and Sculpt Mode, and ensures proper mask protection in Sculpt Mode.
    """
    global _in_pin_handler, _pinned_coords, _pinned_vertex_count, _updating_ui
    if _in_pin_handler:
        return
        
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not topo_obj:
        return
        
    import bmesh
    is_edit = (topo_obj.mode == 'EDIT')
    
    # 0. New face loop ID tracking for boundary extrusion
    global _active_draw_operator
    active_op = _active_draw_operator
    if is_edit and active_op is not None:
        _in_pin_handler = True
        try:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            # Ensure all vertices have valid indices (assign positive indices to new vertices with index -1)
            # without shifting the indices of existing vertices
            max_idx = -1
            for v in bm.verts:
                if v.index > max_idx:
                    max_idx = v.index
            next_idx = max_idx + 1
            for v in bm.verts:
                if v.index < 0:
                    v.index = next_idx
                    next_idx += 1
            grid_layer = bm.faces.layers.int.get("tp_is_grid")
            if not grid_layer:
                grid_layer = bm.faces.layers.int.new("tp_is_grid")
                
            # Use coordinate-based signatures to make them completely stable across
            # Blender's Edit Mode vertex re-indexing/shifting operations.
            def get_face_sig(f):
                return frozenset(tuple(round(c, 4) for c in v.co) for v in f.verts)
                
            current_keys = set()
            for f in bm.faces:
                current_keys.add(get_face_sig(f))
                
            # If the active draw operator is currently in an interactive modal state (such as dragging,
            # smoothing, or drawing), we are reconstructing or modifying the grids internally.
            # During these states, we want to completely skip new loop ID assignment (extrusion detection)
            # and simply synchronize our known_faces cache to keep it matching the current mesh.
            is_interactive = (
                getattr(active_op, 'is_grabbing', False) or
                getattr(active_op, 'is_smoothing', False) or
                getattr(active_op, 'is_drawing', False) or
                getattr(scene, "tp_square_mode", False) or
                getattr(scene, "tp_circle_mode", False)
            )
            
            if is_interactive:
                active_op.known_faces = current_keys
            else:
                known_faces = getattr(active_op, 'known_faces', None)
                if known_faces is None:
                    active_op.known_faces = current_keys
                else:
                    # Check if any faces were deleted/removed (e.g. during grid reconstruction, undo/redo, or vertex weld/merge)
                    deleted_keys = known_faces - current_keys
                    
                    # We only detect extrusion (new loop ID assignment) if:
                    # 1. New faces were added (new_keys is not empty)
                    # 2. NO faces were deleted/removed (deleted_keys is empty, i.e., pure addition)
                    if not deleted_keys:
                        # Clean up known_faces FIRST to remove signatures of deleted faces.
                        # This prevents index reuse/collision bugs where newly created faces get
                        # the same vertex index signatures as previously deleted faces.
                        active_op.known_faces = active_op.known_faces.intersection(current_keys)
                        known_faces = active_op.known_faces
                        
                        new_keys = current_keys - known_faces
                        if new_keys:
                            new_faces = [f for f in bm.faces if get_face_sig(f) in new_keys]
                            should_assign_new = False
                            for f in new_faces:
                                if f[grid_layer] == 0:
                                    should_assign_new = True
                                    break
                                for e in f.edges:
                                    for adj_f in e.link_faces:
                                        if adj_f != f and get_face_sig(adj_f) in known_faces:
                                            if f[grid_layer] == adj_f[grid_layer]:
                                                should_assign_new = True
                                                break
                                    if should_assign_new:
                                        break
                                        
                            if should_assign_new:
                                existing_lids = set()
                                for f in bm.faces:
                                    if get_face_sig(f) not in new_keys:
                                        existing_lids.add(f[grid_layer])
                                existing_lids.discard(0)
                                new_lid = max(existing_lids) + 1 if existing_lids else 1
                                
                                changed_faces = False
                                for f in new_faces:
                                    f[grid_layer] = new_lid
                                    changed_faces = True
                                    
                                if changed_faces:
                                    bmesh.update_edit_mesh(topo_obj.data)
                            active_op.known_faces.update(new_keys)
                    else:
                        # If faces were deleted, or indices shifted (e.g. undo/redo, delete, reconstruction, weld/merge),
                        # we do NOT assign new loop IDs. We simply reset known_faces to current_keys
                        # to keep it in sync with the new index/geometry state.
                        active_op.known_faces = current_keys
        except Exception as e:
            print("Error in depsgraph handler face loop ID tracking:", e)
        finally:
            _in_pin_handler = False
            
    # 1. UI State Synchronization (runs always to keep button in sync with active selection)
    is_pinned = False
    if is_edit:
        try:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            selected_verts = [v for v in bm.verts if v.select]
            if selected_verts:
                active_v = bm.select_history.active
                if active_v and isinstance(active_v, bmesh.types.BMVert) and active_v.select:
                    target_v = active_v
                else:
                    target_v = selected_verts[0]
                pin_layer = bm.verts.layers.int.get("tp_is_pinned")
                if pin_layer and target_v[pin_layer] == 1:
                    is_pinned = True
            else:
                pin_layer = bm.verts.layers.int.get("tp_is_pinned")
                if pin_layer:
                    target_verts = get_pin_target_verts(bm)
                    if target_verts:
                        is_pinned = all(v[pin_layer] == 1 for v in target_verts)
        except Exception:
            pass
    else:
        mesh = topo_obj.data
        selected_indices = [v.index for v in mesh.vertices if v.select]
        pin_attr = mesh.attributes.get("tp_is_pinned")
        if pin_attr:
            if selected_indices:
                idx = selected_indices[0]
                if pin_attr.data[idx].value == 1:
                    is_pinned = True
            else:
                bm = bmesh.new()
                bm.from_mesh(mesh)
                bm.verts.ensure_lookup_table()
                target_verts = get_pin_target_verts(bm)
                target_indices = {v.index for v in target_verts}
                bm.free()
                if target_indices:
                    is_pinned = all(pin_attr.data[idx].value == 1 for idx in target_indices)
                    
    if scene.tp_pin_boundary != is_pinned:
        _updating_ui = True
        try:
            scene.tp_pin_boundary = is_pinned
        finally:
            _updating_ui = False

    # --- Seam Edge UI State Synchronization ---
    is_seam = False
    if is_edit:
        try:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            selected_edges = [e for e in bm.edges if e.select]
            if selected_edges:
                active_e = bm.select_history.active
                if active_e and isinstance(active_e, bmesh.types.BMEdge) and active_e.select:
                    target_e = active_e
                else:
                    target_e = selected_edges[0]
                if target_e.seam:
                    is_seam = True
            else:
                target_edges = get_seam_target_edges(bm)
                if target_edges:
                    is_seam = all(e.seam for e in target_edges)
        except Exception:
            pass
    else:
        try:
            mesh = topo_obj.data
            selected_edges = [e for e in mesh.edges if e.select]
            if selected_edges:
                target_e = selected_edges[0]
                if target_e.use_seam:
                    is_seam = True
            else:
                bm = bmesh.new()
                bm.from_mesh(mesh)
                bm.edges.ensure_lookup_table()
                target_edges = get_seam_target_edges(bm)
                target_indices = {e.index for e in target_edges}
                bm.free()
                if target_indices:
                    is_seam = all(mesh.edges[idx].use_seam for idx in target_indices)
        except Exception:
            pass
            
    if scene.tp_seam_edge != is_seam:
        _updating_ui = True
        try:
            scene.tp_seam_edge = is_seam
        finally:
            _updating_ui = False
            
    # 2. Coordinates Enforcement
    # Get the current vertex count to check for topology changes
    current_count = 0
    if is_edit:
        try:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            current_count = len(bm.verts)
        except Exception:
            return
    else:
        current_count = len(topo_obj.data.vertices)
        
    # If topology changed, rebuild the pinning coordinates at their current position
    if current_count != _pinned_vertex_count:
        _in_pin_handler = True
        try:
            update_pinned_coordinates(bpy.context)
        finally:
            _in_pin_handler = False
        return
        
    # Enforce boundary coordinates
    _in_pin_handler = True
    try:
        changed = False
        
        if is_edit:
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            pin_layer = bm.verts.layers.int.get("tp_is_pinned")
            
            # Clean up _pinned_coords if any vertex is no longer pinned in BMesh
            to_remove = []
            for idx in list(_pinned_coords.keys()):
                if idx < len(bm.verts):
                    v = bm.verts[idx]
                    if not pin_layer or v[pin_layer] != 1:
                        to_remove.append(idx)
                else:
                    to_remove.append(idx)
            for idx in to_remove:
                _pinned_coords.pop(idx, None)
                
            if _pinned_coords:
                for idx, co in _pinned_coords.items():
                    if idx < len(bm.verts):
                        v = bm.verts[idx]
                        if (v.co - co).length > 1e-5:
                            v.co = co.copy()
                            changed = True
                if changed:
                    bmesh.update_edit_mesh(topo_obj.data)
        else:
            mesh = topo_obj.data
            pin_attr = mesh.attributes.get("tp_is_pinned")
            
            # Clean up _pinned_coords if any vertex is no longer pinned in mesh attributes
            to_remove = []
            for idx in list(_pinned_coords.keys()):
                if idx < len(mesh.vertices):
                    if not pin_attr or pin_attr.data[idx].value != 1:
                        to_remove.append(idx)
                else:
                    to_remove.append(idx)
            for idx in to_remove:
                _pinned_coords.pop(idx, None)
                
            if _pinned_coords:
                for idx, co in _pinned_coords.items():
                    if idx < len(mesh.vertices):
                        v = mesh.vertices[idx]
                        if (v.co - co).length > 1e-5:
                            v.co = co.copy()
                            changed = True
                
                # Enforce sculpt mode masking
                if topo_obj.mode == 'SCULPT':
                    mask_attr = mesh.attributes.get("mask")
                    if not mask_attr:
                        mask_attr = mesh.attributes.new(name="mask", type='FLOAT', domain='POINT')
                    for idx in _pinned_coords.keys():
                        if idx < len(mask_attr.data):
                            item = mask_attr.data[idx]
                            if item.value != 1.0:
                                item.value = 1.0
                                changed = True
                if changed:
                    mesh.update()
                
    except Exception as e:
        print("Error enforcing boundary pin:", e)
    finally:
        _in_pin_handler = False


_in_edge_length_update = False

def on_edge_length_update(self, context):
    global _in_edge_length_update
    if _in_edge_length_update:
        return
    _in_edge_length_update = True
    try:
        global _active_draw_operator
        active_op = _active_draw_operator
        if active_op is not None:
            if getattr(active_op, 'ui_is_dragging', False):
                context.scene.tp_edge_length = getattr(active_op, 'ui_click_edge_length', 0.1)
                return
            target_edge_length = context.scene.tp_edge_length
            if target_edge_length > 0.001:
                topo_obj_name = "TP_Topology_Mesh"
                topo_obj = bpy.data.objects.get(topo_obj_name)
                if topo_obj and topo_obj.mode == 'EDIT':
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    
                    # Initialize the original loops cache if not already done
                    if getattr(active_op, 'subdiv_original_loops', None) is None:
                        loops = active_op.find_all_loops(bm)
                        if loops:
                            vert_loop_count = {}
                            for loop in loops:
                                for v in loop['verts']:
                                    vert_loop_count[v] = vert_loop_count.get(v, 0) + 1
                            
                            active_op.subdiv_original_loops = []
                            for loop in loops:
                                splicing_indices = set()
                                for idx, v in enumerate(loop['verts']):
                                    if vert_loop_count[v] > 1:
                                        splicing_indices.add(idx)
                                
                                entry = {
                                    'type': loop['type'],
                                    'coords': [v.co.copy() for v in loop['verts']],
                                    'splicing_indices': splicing_indices,
                                    'original_count': len(loop['verts']),
                                    'loop_id': loop.get('loop_id', None)
                                }
                                # For ring loops, also save the inner boundary coords
                                if loop['type'] == 'ring' and 'inner_verts' in loop:
                                    entry['inner_coords'] = [v.co.copy() for v in loop['inner_verts']]
                                    entry['inner_count'] = len(loop['inner_verts'])
                                active_op.subdiv_original_loops.append(entry)
                    
                    # Calculate multiplier from target_edge_length
                    # Ring loops are excluded from this calculation (their density follows inner disk)
                    if getattr(active_op, 'subdiv_original_loops', None):
                        total_len = 0.0
                        total_edges = 0
                        for orig_loop in active_op.subdiv_original_loops:
                            if orig_loop['type'] == 'ring':
                                continue  # ring loops follow the inner disk density
                            P = orig_loop['coords']
                            is_open = (orig_loop['type'] == 'open_path')
                            orig_count = orig_loop['original_count']
                            orig_edges = (orig_count - 1) if is_open else orig_count
                            
                            perimeter = 0.0
                            for i in range(len(P)):
                                p1 = P[i]
                                if is_open:
                                    if i == len(P) - 1:
                                        continue
                                    p2 = P[i + 1]
                                else:
                                    p2 = P[(i + 1) % len(P)]
                                perimeter += (p2 - p1).length
                            total_len += perimeter
                            total_edges += orig_edges
                            
                        if total_edges > 0:
                            new_multiplier = (total_len / target_edge_length) / total_edges
                            active_op.subdiv_multiplier = max(0.01, new_multiplier)
                            active_op.handle_loop_subdivision(context, event=None)
    except Exception as e:
        print("Error in on_edge_length_update:", e)
    finally:
        _in_edge_length_update = False



def on_boundary_mode_update(self, context):
    """
    当 tp_boundary_mode 开启时，默认进入编辑模式下的边模式。
    """
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not topo_obj:
        return
    
    # 只有当拓扑在运行中且拓扑对象处于编辑模式时才更改模式
    if context.window_manager.tp_topology_running and topo_obj.mode == 'EDIT':
        try:
            if context.scene.tp_boundary_mode:
                context.scene.tool_settings.mesh_select_mode = (False, True, False)
            else:
                context.scene.tool_settings.mesh_select_mode = (True, False, False)
        except Exception as e:
            print("Error in on_boundary_mode_update:", e)


_in_mode_update = False

def on_square_mode_update(self, context):
    global _in_mode_update
    if _in_mode_update:
        return
    _in_mode_update = True
    try:
        if context.scene.tp_square_mode:
            context.scene.tp_circle_mode = False
    finally:
        _in_mode_update = False

def on_circle_mode_update(self, context):
    global _in_mode_update
    if _in_mode_update:
        return
    _in_mode_update = True
    try:
        if context.scene.tp_circle_mode:
            context.scene.tp_square_mode = False
    finally:
        _in_mode_update = False


class OBJECT_OT_tp_apply_symmetry(bpy.types.Operator):
    bl_idname = "object.tp_apply_symmetry"
    bl_label = "确认对称"
    bl_description = "应用镜像内容并同时退出对称状态"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        return topo_obj is not None and getattr(context.scene, "tp_symmetry_mode", False)

    def execute(self, context):
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if not topo_obj:
            self.report({'WARNING'}, "未找到拓扑网格对象")
            return {'CANCELLED'}

        # 确认前取消所有固定内容
        context.scene.tp_pin_boundary = False
        import bmesh
        if topo_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            pin_layer = bm.verts.layers.int.get("tp_is_pinned")
            if pin_layer:
                for v in bm.verts:
                    v[pin_layer] = 0
            bmesh.update_edit_mesh(topo_obj.data)
        else:
            pin_attr = topo_obj.data.attributes.get("tp_is_pinned")
            if pin_attr:
                for val in pin_attr.data:
                    val.value = 0
            topo_obj.data.update()
        update_pinned_coordinates(context)

        scene = context.scene
        mod_name = "TP_Mirror"
        mod = topo_obj.modifiers.get(mod_name)

        if mod:
            orig_mode = topo_obj.mode
            orig_active = context.view_layer.objects.active
            
            context.view_layer.objects.active = topo_obj
            if orig_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except Exception as e:
                    self.report({'ERROR'}, f"切换至物体模式失败: {e}")
                    context.view_layer.objects.active = orig_active
                    return {'CANCELLED'}

            try:
                bpy.ops.object.modifier_apply(modifier=mod_name)
            except Exception as e:
                self.report({'ERROR'}, f"应用镜像失败: {e}")
                if orig_mode != 'OBJECT':
                    try:
                        bpy.ops.object.mode_set(mode=orig_mode)
                    except Exception:
                        pass
                context.view_layer.objects.active = orig_active
                return {'CANCELLED'}

            if orig_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode=orig_mode)
                except Exception:
                    pass
            context.view_layer.objects.active = orig_active
            self.report({'INFO'}, "已应用镜像内容")
        else:
            self.report({'INFO'}, "未检测到活动的镜像修改器")

        # Exit symmetry state
        scene.tp_symmetry_mode = False

        # Rebuild KDTree in active drawing operator
        global _active_draw_operator
        if _active_draw_operator:
            try:
                _active_draw_operator.rebuild_kd_tree()
            except Exception as e:
                print("Error rebuilding KD-Tree after applying symmetry:", e)

        if context.area:
            context.area.tag_redraw()

        return {'FINISHED'}


def update_mirror_modifier(context):
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not topo_obj:
        return
        
    scene = context.scene
    mod_name = "TP_Mirror"
    
    if scene.tp_symmetry_mode:
        mod = topo_obj.modifiers.get(mod_name)
        if not mod:
            mod = topo_obj.modifiers.new(name=mod_name, type='MIRROR')
            
        if len(topo_obj.modifiers) > 1 and topo_obj.modifiers[0].name != mod_name:
            try:
                act_obj = context.view_layer.objects.active
                context.view_layer.objects.active = topo_obj
                bpy.ops.object.modifier_move_to_index(modifier=mod_name, index=0)
                context.view_layer.objects.active = act_obj
            except Exception as e:
                print("Error moving mirror modifier:", e)
                
        mod.use_axis[0] = scene.tp_symmetry_x
        mod.use_axis[1] = scene.tp_symmetry_y
        mod.use_axis[2] = scene.tp_symmetry_z
        
        ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        if ref_obj:
            mod.mirror_object = ref_obj
            
        mod.use_mirror_merge = True
        mod.merge_threshold = 0.001
        mod.show_in_editmode = True
        mod.show_on_cage = True
    else:
        mod = topo_obj.modifiers.get(mod_name)
        if mod:
            try:
                topo_obj.modifiers.remove(mod)
            except Exception as e:
                print("Error removing mirror modifier:", e)

def on_symmetry_mode_update(self, context):
    update_mirror_modifier(context)
    if context.area:
        context.area.tag_redraw()

def on_symmetry_axis_update(self, context):
    update_mirror_modifier(context)
    if context.area:
        context.area.tag_redraw()


