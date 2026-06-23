import bpy
import bmesh
import mathutils
from mathutils import kdtree
from bpy_extras.view3d_utils import (
    region_2d_to_origin_3d,
    region_2d_to_vector_3d,
    location_3d_to_region_2d
)

from .draw_utils import draw_callback, draw_text_callback

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
            
        self.ensure_shrinkwrap_modifier(context, topo_obj)
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
            
        mod.offset = 0.003
        mod.show_in_editmode = True
        
        # 3. 关键选项：开启笼子显示，强制 Blender 编辑手柄(Gizmo)物理投射到高模表面，解决操作分离感
        mod.show_on_cage = True

    def enforce_topology_state(self, context):
        """强固拓扑模式所需的状态：实时保障 C++ 级底层物理吸附在调整时正常工作"""
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
            
            self.ensure_shrinkwrap_modifier(context, topo_obj)
                    
        # 强制自动合并
        if not context.scene.tool_settings.use_mesh_automerge:
            context.scene.tool_settings.use_mesh_automerge = True
            
        # 强固面部吸附参数设置：在拖拽微调、滑动或G键移动时，直接由 C++ 执行 60FPS 实时投影包裹
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
            
        # 核心选项：开启投影单一元素到高模面，使得 C++ 原生模态在调整时始终吸附
        if hasattr(context.scene.tool_settings, "use_snap_project"):
            if not context.scene.tool_settings.use_snap_project:
                context.scene.tool_settings.use_snap_project = True

        # 禁用仅吸附到可选择对象和吸附到自身，防止产生只能在边上吸附移动的bug
        if hasattr(context.scene.tool_settings, "use_snap_selectable"):
            if context.scene.tool_settings.use_snap_selectable:
                context.scene.tool_settings.use_snap_selectable = False
        if hasattr(context.scene.tool_settings, "use_snap_self"):
            if context.scene.tool_settings.use_snap_self:
                context.scene.tool_settings.use_snap_self = False

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
        self.is_grabbing = False
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
        self.stroke_history = []
        self.last_clicked_vert_idx = None
        self.last_clicked_cycles = []
        self.last_clicked_cycle_idx = -1
        self.last_ctrl_state = False
        self.start_from_selected_v_co = None
        self.start_from_selected_v_idx = None
        
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
            context.scene.tool_settings.mesh_select_mode = (True, False, False)
        except Exception as e:
            print("Failed to enter Edit Mode:", e)
            
        self.rebuild_kd_tree()

        context.window_manager.tp_topology_running = True

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

    def modal(self, context, event):
        if not context.window_manager.tp_topology_running:
            self.cleanup(context)
            self.report({'INFO'}, "已退出TP拓扑模式")
            return {'FINISHED'}

        self.enforce_topology_state(context)

        if getattr(self, 'is_grabbing', False):
            return self.handle_grab_modal(context, event)

        ctrl_pressed = event.ctrl
        if ctrl_pressed and not getattr(self, 'last_ctrl_state', False):
            topo_obj_name = "TP_Topology_Mesh"
            topo_obj = bpy.data.objects.get(topo_obj_name)
            if topo_obj and topo_obj.mode == 'EDIT':
                try:
                    bm_weld = bmesh.from_edit_mesh(topo_obj.data)
                    bmesh.ops.remove_doubles(bm_weld, verts=bm_weld.verts, dist=0.001)
                    bmesh.update_edit_mesh(topo_obj.data)
                except Exception as e:
                    print("Error welding vertices on Ctrl press:", e)
        self.last_ctrl_state = ctrl_pressed

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
            if event.type == 'LEFTMOUSE' and event.alt:
                topo_obj_name = "TP_Topology_Mesh"
                topo_obj = bpy.data.objects.get(topo_obj_name)
                if topo_obj and topo_obj.mode == 'EDIT':
                    try:
                        bm_weld = bmesh.from_edit_mesh(topo_obj.data)
                        bmesh.ops.remove_doubles(bm_weld, verts=bm_weld.verts, dist=0.001)
                        bmesh.update_edit_mesh(topo_obj.data)
                    except Exception as e:
                        print("Error welding vertices on Alt+Click:", e)
                        
                self.rebuild_kd_tree()
                mouse_coord = (event.mouse_region_x, event.mouse_region_y)
                nearest_world_pt, nearest_v_idx = self.find_nearest_vertex(context, mouse_coord, threshold_pixels=20)
                
                topo_obj_name = "TP_Topology_Mesh"
                topo_obj = bpy.data.objects.get(topo_obj_name)
                
                if topo_obj and topo_obj.mode == 'EDIT':
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    
                    if nearest_v_idx is not None and nearest_v_idx < len(bm.verts):
                        v_target = bm.verts[nearest_v_idx]
                        cycles = self.find_cycles_through_vertex(v_target)
                        
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
                            
                            if event.shift:
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
                            self.report({'INFO'}, "该点不属于任何闭合圈")
                            
                    else:
                        nearest_edge = self.find_nearest_edge(context, mouse_coord, threshold_pixels=20)
                        if nearest_edge:
                            success_native = False
                            if len(nearest_edge.link_faces) >= 1:
                                try:
                                    if not event.shift:
                                        for v in bm.verts:
                                            v.select = False
                                        for e in bm.edges:
                                            e.select = False
                                    bm.select_history.clear()
                                    bm.select_history.add(nearest_edge)
                                    bmesh.update_edit_mesh(topo_obj.data)
                                    
                                    bpy.ops.mesh.loop_select(extend=event.shift)
                                    
                                    bm = bmesh.from_edit_mesh(topo_obj.data)
                                    bm.verts.ensure_lookup_table()
                                    bm.edges.ensure_lookup_table()
                                    success_native = True
                                except Exception as e:
                                    print("Native loop select failed, falling back:", e)
                                    success_native = False
                                    
                            if not success_native:
                                chain_edges = self.find_edge_chain(nearest_edge)
                                
                                if not event.shift:
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
                            
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}

            if event.type == 'Z' and event.ctrl:
                if event.shift:
                    try:
                        bpy.ops.ed.redo()
                    except Exception:
                        pass
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
                        self.grab_initial_cos = {v.index: v.co.copy() for v in selected_verts}
                        
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
                        ray_origin_start = region_2d_to_origin_3d(region, rv3d, self.grab_mouse_start)
                        ray_vector_start = region_2d_to_vector_3d(region, rv3d, self.grab_mouse_start)
                        self.grab_initial_depth = (active_start_world - ray_origin_start).dot(ray_vector_start)
                        self.grab_snap_target_idx = None
                        self.hover_snap_pt = None
                        
                        context.workspace.status_text_set("TP拓扑移动 | 移动鼠标: 调整位置 | 左键/回车: 确定并吸附合并 | 右键/ESC: 取消")
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}

            if context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
                
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.ctrl:
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
                    self.is_drawing = True
                    self.is_dragging = True
                    self.is_polyline = False
                    self.stroke_points = [pt]
                    self.stroke_snap_indices = [snap_v]
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
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
                        if (pt - self.stroke_points[-1]).length > 0.001:
                            self.stroke_points.append(pt)
                            self.stroke_snap_indices.append(snap_v)
                            context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                    
                return {'RUNNING_MODAL'}
            else:
                if event.type == 'MOUSEMOVE':
                    if self.is_dragging:
                        coord = (event.mouse_region_x, event.mouse_region_y)
                        dx = coord[0] - self.last_mouse_coord_prev[0]
                        dy = coord[1] - self.last_mouse_coord_prev[1]
                        if (dx*dx + dy*dy) ** 0.5 > 12:
                            snap_pt, snap_v = self.find_nearest_vertex(context, coord, threshold_pixels=20)
                            pt = snap_pt if snap_pt else self.get_surface_point(context, coord)
                            if pt:
                                if (pt - self.stroke_points[-1]).length > 0.001:
                                    self.stroke_points.append(pt)
                                    self.stroke_snap_indices.append(snap_v)
                                    self.last_mouse_coord_prev = coord
                                    context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                        
                elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    coord = (event.mouse_region_x, event.mouse_region_y)
                    dx = coord[0] - self.drag_start_coord[0]
                    dy = coord[1] - self.drag_start_coord[1]
                    click_dist = (dx*dx + dy*dy) ** 0.5
                    
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
            local_pt = location + normal * 0.003
            world_pt = matrix_world @ local_pt
            return world_pt
            
        return None

    def rebuild_kd_tree(self):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj:
            self.kd_tree = None
            return
            
        matrix_world = topo_obj.matrix_world
        
        if topo_obj.mode == 'EDIT':
            try:
                bm = bmesh.from_edit_mesh(topo_obj.data)
                if not bm.verts:
                    self.kd_tree = None
                    return
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
            self.kd_tree = kdtree.KDTree(len(topo_obj.data.vertices))
            for i, v in enumerate(topo_obj.data.vertices):
                self.kd_tree.insert(matrix_world @ v.co, i)
            self.kd_tree.balance()

    def find_nearest_vertex(self, context, mouse_coord, threshold_pixels=20):
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
        
        for co, index, dist in nearest:
            screen_coord = location_3d_to_region_2d(region, rv3d, co)
            if screen_coord:
                dist_px = (screen_coord - mouse_vec).length
                if dist_px < min_dist_px:
                    if not self.check_line_crosses_ref_mesh(ref_obj, co, target_pt):
                        min_dist_px = dist_px
                        nearest_v_idx = index
                        nearest_world_pt = co
                    
        if nearest_v_idx is not None:
            return nearest_world_pt, nearest_v_idx
        return None, None

    def find_cycles_through_vertex(self, v_start, max_cycles=15, max_len=150):
        from collections import deque
        cycles = []
        seen_cycles = set()
        
        queue = deque([(v_start, [v_start], {v_start.index})])
        steps = 0
        max_steps = 10000
        
        while queue and len(cycles) < max_cycles and steps < max_steps:
            steps += 1
            curr, path, visited = queue.popleft()
            
            valid_edges = [e for e in curr.link_edges if len(e.link_faces) <= 1]
            for edge in valid_edges:
                nbr = edge.other_vert(curr)
                if nbr.index == v_start.index:
                    if len(path) >= 3:
                        canonical = tuple(sorted(v.index for v in path))
                        if canonical not in seen_cycles:
                            seen_cycles.add(canonical)
                            cycles.append([v.index for v in path])
                elif nbr.index not in visited:
                    if len(path) < max_len:
                        new_visited = visited.copy()
                        new_visited.add(nbr.index)
                        queue.append((nbr, path + [nbr], new_visited))
                        
        cycles.sort(key=lambda c: len(c))
        return cycles

    def find_nearest_edge(self, context, mouse_coord, threshold_pixels=20):
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        if not topo_obj or topo_obj.mode != 'EDIT':
            return None
            
        bm = bmesh.from_edit_mesh(topo_obj.data)
        matrix_world = topo_obj.matrix_world
        region = context.region
        rv3d = context.space_data.region_3d
        p_mouse = mathutils.Vector(mouse_coord)
        
        nearest_edge = None
        min_dist = threshold_pixels
        
        for e in bm.edges:
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

    def find_edge_chain(self, edge):
        chain_edges = {edge}
        
        curr_v = edge.verts[0]
        prev_e = edge
        while True:
            valid_edges = [e for e in curr_v.link_edges if len(e.link_faces) <= 1]
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
            valid_edges = [e for e in curr_v.link_edges if len(e.link_faces) <= 1]
            if len(valid_edges) != 2:
                break
            next_e = valid_edges[0] if valid_edges[0] != prev_e else valid_edges[1]
            if next_e in chain_edges:
                break
            chain_edges.add(next_e)
            curr_v = next_e.other_vert(curr_v)
            prev_e = next_e
            
        return chain_edges

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
        
        for v in bm.verts:
            if active_indices is not None and v.index not in active_indices:
                continue
            try:
                world_co = topo_world @ v.co
                local_target = matrix_inverse @ world_co
                success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                if success:
                    local_pt = location + normal * 0.003
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
            
        edge_len = context.scene.tp_edge_length
        if edge_len < 0.001:
            edge_len = 0.001
            
        dists = [0.0]
        for i in range(n - 1):
            dists.append(dists[-1] + (points[i+1] - points[i]).length)
            
        total_len = dists[-1]
        if total_len < 0.001:
            return [points[0]] * 4
            
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
                target_d = (j / (M - 1)) * total_len
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
            
        if is_closed:
            points = list(points)
            points[-1] = points[0]
            snap_indices = list(snap_indices)
            snap_indices[-1] = snap_indices[0]
            
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
            
        boundaries = []
        boundaries.append((0, snap_indices[0]))
        for i in range(1, n - 1):
            if snap_indices[i] is not None:
                if snap_indices[i] != boundaries[-1][1]:
                    boundaries.append((i, snap_indices[i]))
        if n - 1 > boundaries[-1][0]:
            boundaries.append((n - 1, snap_indices[-1]))
            
        num_segs = len(boundaries) - 1
        seg_lengths = []
        seg_points = []
        seg_snap_indices = []
        
        for j in range(num_segs):
            start_idx = boundaries[j][0]
            end_idx = boundaries[j+1][0]
            
            pts = points[start_idx : end_idx + 1]
            snaps = snap_indices[start_idx : end_idx + 1]
            
            l_seg = 0.0
            for k in range(len(pts) - 1):
                l_seg += (pts[k+1] - pts[k]).length
                
            seg_lengths.append(l_seg)
            seg_points.append(pts)
            seg_snap_indices.append(snaps)
            
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
                            local_pt = location + normal * 0.003
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
            
        return resampled_points, resampled_snap_indices

    def create_geometry(self, context):
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
                        
        self.stroke_points, self.stroke_snap_indices = self.resample_stroke_segments(
            context, self.stroke_points, self.stroke_snap_indices, is_closed
        )

        curr_indices = []

        if topo_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(topo_obj.data)
            inv_matrix = topo_obj.matrix_world.inverted()
            
            bm.verts.ensure_lookup_table()
            
            bm_verts = []
            for idx_pt, pt in enumerate(self.stroke_points):
                local_pt = inv_matrix @ pt
                v = None
                
                snap_idx = self.stroke_snap_indices[idx_pt]
                if snap_idx is not None:
                    bm.verts.ensure_lookup_table()
                    if snap_idx < len(bm.verts):
                        v = bm.verts[snap_idx]
                        
                if v is None:
                    v = self.get_or_create_vertex(bm, local_pt)
                bm_verts.append(v)
                
            bm.verts.ensure_lookup_table()
            
            for i in range(len(bm_verts) - 1):
                v1, v2 = bm_verts[i], bm_verts[i+1]
                if v1 != v2 and not bm.edges.get((v1, v2)):
                    bm.edges.new((v1, v2))
                
            curr_indices = [v.index for v in bm_verts if v is not None]
            bmesh.update_edit_mesh(topo_obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(topo_obj.data)
            inv_matrix = topo_obj.matrix_world.inverted()
            
            bm.verts.ensure_lookup_table()
            
            bm_verts = []
            for idx_pt, pt in enumerate(self.stroke_points):
                local_pt = inv_matrix @ pt
                v = None
                
                snap_idx = self.stroke_snap_indices[idx_pt]
                if snap_idx is not None:
                    bm.verts.ensure_lookup_table()
                    if snap_idx < len(bm.verts):
                        v = bm.verts[snap_idx]
                        
                if v is None:
                    v = self.get_or_create_vertex(bm, local_pt)
                bm_verts.append(v)
                
            bm.verts.ensure_lookup_table()
            
            for i in range(len(bm_verts) - 1):
                v1, v2 = bm_verts[i], bm_verts[i+1]
                if v1 != v2 and not bm.edges.get((v1, v2)):
                    bm.edges.new((v1, v2))
                
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

        if not self.is_polyline and expanded_active_indices:
            self.resample_loops(context, topo_obj, active_indices=expanded_active_indices)
            
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

    def resample_loops(self, context, topo_obj, active_indices=None):
        is_edit_mode = (topo_obj.mode == 'EDIT')
        
        if is_edit_mode:
            bm = bmesh.from_edit_mesh(topo_obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(topo_obj.data)
            
        bm.verts.ensure_lookup_table()
        
        if active_indices is not None:
            val2_verts = [v for v in bm.verts if len(v.link_edges) == 2 and v.index in active_indices]
        else:
            val2_verts = [v for v in bm.verts if len(v.link_edges) == 2]
        
        if not val2_verts:
            if not is_edit_mode:
                bm.free()
            return
            
        factor = getattr(context.scene, "tp_smooth_factor", 0.05)
        if factor < 0.001:
            if not is_edit_mode:
                bm.free()
            return
            
        ref_obj = bpy.data.objects.get(self.ref_object_name)
        if ref_obj:
            matrix_world = ref_obj.matrix_world
            matrix_inverse = matrix_world.inverted()
            topo_inverse = topo_obj.matrix_world.inverted()
            topo_world = topo_obj.matrix_world
            
            iterations = 2
            for iteration in range(iterations):
                new_cos = {}
                for v in val2_verts:
                    neighbors = [e.other_vert(v) for e in v.link_edges]
                    avg_co = (neighbors[0].co + neighbors[1].co) / 2.0
                    new_cos[v] = v.co * (1.0 - factor) + avg_co * factor
                    
                for v, co in new_cos.items():
                    try:
                        world_co = topo_world @ co
                        local_target = matrix_inverse @ world_co
                        success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                        if success:
                            local_pt = location + normal * 0.003
                            co = topo_inverse @ (matrix_world @ local_pt)
                    except Exception:
                        pass
                    v.co = co
                
        if is_edit_mode:
            bmesh.update_edit_mesh(topo_obj.data)
        else:
            bm.to_mesh(topo_obj.data)
            bm.free()
            topo_obj.data.update()

    def cleanup(self, context):
        try:
            context.window_manager.tp_topology_running = False
        except Exception:
            pass

        # 还原用户的吸附设置
        try:
            context.scene.tool_settings.use_snap = self.orig_use_snap
            context.scene.tool_settings.snap_elements = self.orig_snap_elements
            context.scene.tool_settings.snap_target = self.orig_snap_target
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
            
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

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
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            for v_idx, initial_co in self.grab_initial_cos.items():
                if v_idx < len(bm.verts):
                    bm.verts[v_idx].co = initial_co
            bmesh.update_edit_mesh(topo_obj.data)
            
            self.is_grabbing = False
            self.hover_snap_pt = None
            self.grab_snap_target_idx = None
            self.report({'INFO'}, "已取消移动")
            context.workspace.status_text_set("TP拓扑模式 | Ctrl+左键拖拽: 连续绘制 | Ctrl+左键单击: 绘制多段线 | Alt+左键: 选中圈/循环边 | 右键/回车: 提交 | ESC退出")
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            
            if self.grab_snap_target_idx is not None:
                active_v = bm.verts[self.grab_active_vert_idx]
                target_v = bm.verts[self.grab_snap_target_idx]
                
                if active_v.is_valid and target_v.is_valid and active_v != target_v:
                    active_v.co = target_v.co.copy()
                    try:
                        bmesh.ops.weld_verts(bm, targetmap={active_v: target_v})
                    except Exception as e:
                        print("Weld verts error:", e)
                        
            try:
                bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.001)
            except Exception as e:
                print("Remove doubles error:", e)
                
            bmesh.update_edit_mesh(topo_obj.data)
            self.rebuild_kd_tree()
            
            self.is_grabbing = False
            self.hover_snap_pt = None
            self.grab_snap_target_idx = None
            
            try:
                bpy.ops.ed.undo_push(message="TP 移动与合并")
            except Exception as e:
                print("Error pushing undo step:", e)
                
            self.report({'INFO'}, "已确认位置并合并")
            context.workspace.status_text_set("TP拓扑模式 | Ctrl+左键拖拽: 连续绘制 | Ctrl+左键单击: 绘制多段线 | Alt+左键: 选中圈/循环边 | 右键/回车: 提交 | ESC退出")
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        if event.type == 'MOUSEMOVE':
            mouse_coord = (event.mouse_region_x, event.mouse_region_y)
            
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
            
            active_v = bm.verts[self.grab_active_vert_idx]
            active_start_local = self.grab_initial_cos[self.grab_active_vert_idx]
            
            delta_local = active_new_local - active_start_local
            
            for v_idx, init_co in self.grab_initial_cos.items():
                if v_idx < len(bm.verts):
                    bm.verts[v_idx].co = init_co + delta_local
            
            topo_world = topo_obj.matrix_world
            ref_matrix = ref_obj.matrix_world
            ref_inverse = ref_matrix.inverted()
            
            for v_idx in self.grab_initial_cos.keys():
                if v_idx < len(bm.verts):
                    v = bm.verts[v_idx]
                    if v_idx != self.grab_active_vert_idx:
                        try:
                            world_co = topo_world @ v.co
                            local_target = ref_inverse @ world_co
                            success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                            if success:
                                local_pt = location + normal * 0.003
                                v.co = inv_topo_matrix @ (ref_matrix @ local_pt)
                        except Exception:
                            pass
            
            self.grab_snap_target_idx = None
            self.hover_snap_pt = None
            
            try:
                world_co = topo_world @ active_new_local
                local_target = ref_inverse @ world_co
                success, location, normal, index = ref_obj.closest_point_on_mesh(local_target)
                if success:
                    local_pt = location + normal * 0.003
                    active_projected_world = ref_matrix @ local_pt
                else:
                    active_projected_world = world_co
            except Exception:
                active_projected_world = world_co
                
            active_v.co = inv_topo_matrix @ active_projected_world
            
            if self.kd_tree:
                try:
                    nearest = self.kd_tree.find_n(active_projected_world, 50)
                except Exception:
                    nearest = None
                    
                if nearest:
                    mouse_vec = mathutils.Vector(mouse_coord)
                    min_dist_px = 20.0
                    snap_candidate_idx = None
                    snap_candidate_co = None
                    
                    for co, idx, dist in nearest:
                        if idx in self.grab_initial_cos:
                            continue
                            
                        screen_coord = location_3d_to_region_2d(region, rv3d, co)
                        if screen_coord:
                            dist_px = (screen_coord - mouse_vec).length
                            if dist_px < min_dist_px:
                                min_dist_px = dist_px
                                snap_candidate_idx = idx
                                snap_candidate_co = co
                                
                    if snap_candidate_idx is not None:
                        crosses = self.check_line_crosses_ref_mesh(ref_obj, snap_candidate_co, active_projected_world)
                        if not crosses:
                            self.grab_snap_target_idx = snap_candidate_idx
                            self.hover_snap_pt = snap_candidate_co
                            active_v.co = inv_topo_matrix @ snap_candidate_co
                            
            bmesh.update_edit_mesh(topo_obj.data)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
            
        return {'RUNNING_MODAL'}
