# -*- coding: utf-8 -*-

bl_info = {
    "name": "TP拓扑",
    "author": "Antigravity",
    "version": (1, 0),
    "blender": (2, 80, 0),
    "location": "View3D > Sidebar > TP拓扑",
    "description": "在网格对象表面通过点击和拖动进行手动拓扑的插件",
    "category": "Mesh",
}

import bpy
import bmesh
import blf
import gpu
import mathutils
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils

# ==========================================
# 绘图辅助函数 (兼容 Blender 3.x / 4.x)
# ==========================================

def draw_rect(x1, y1, x2, y2, color):
    """在 2D 视口中绘制填充矩形"""
    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    except ValueError:
        shader = gpu.shader.from_builtin('2D_UNIFORM_COLOR')
        
    vertices = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    indices = ((0, 1, 2), (0, 2, 3))
    
    batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)
    
    shader.bind()
    shader.uniform_float("color", color)
    
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def draw_text(font_id, text, x, y, size=13, color=(1, 1, 1, 1)):
    """在 2D 视口中绘制文本"""
    try:
        blf.size(font_id, size)
    except TypeError:
        blf.size(font_id, size, 72)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def draw_circle(cx, cy, radius, color, segments=16, line_width=2):
    """在 2D 视口中绘制中空圆圈"""
    import math
    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    except ValueError:
        shader = gpu.shader.from_builtin('2D_UNIFORM_COLOR')
        
    vertices = []
    for i in range(segments):
        theta = 2.0 * math.pi * i / segments
        x = cx + radius * math.cos(theta)
        y = cy + radius * math.sin(theta)
        vertices.append((x, y))
        
    indices = []
    for i in range(segments):
        indices.append((i, (i + 1) % segments))
        
    batch = batch_for_shader(shader, 'LINES', {"pos": vertices}, indices=indices)
    
    try:
        gpu.state.line_width_set(line_width)
    except Exception:
        pass
        
    shader.bind()
    shader.uniform_float("color", color)
    
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def draw_line_2d(x1, y1, x2, y2, color, line_width=2):
    """在 2D 视口中绘制直线"""
    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    except ValueError:
        shader = gpu.shader.from_builtin('2D_UNIFORM_COLOR')
        
    vertices = ((x1, y1), (x2, y2))
    indices = ((0, 1),)
    
    batch = batch_for_shader(shader, 'LINES', {"pos": vertices}, indices=indices)
    
    try:
        gpu.state.line_width_set(line_width)
    except Exception:
        pass
        
    shader.bind()
    shader.uniform_float("color", color)
    
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def draw_callback_px(self, context):
    """模态运行时的视口 2D HUD 绘图回调"""
    if not getattr(self, 'drawing_active', False):
        return
        
    x1, y1 = 20, 20
    width = 330
    height = 167
    x2, y2 = x1 + width, y1 + height
    
    draw_rect(x1, y1, x2, y2, (0.05, 0.05, 0.05, 0.85))
    draw_rect(x1, y1, x1 + 5, y2, (0.2, 0.6, 1.0, 1.0))
    
    font_id = 0
    if getattr(self, 'grab_mode', False):
        draw_text(font_id, "✨ 移动模式 (运行中)", x1 + 18, y2 - 28, size=15, color=(0.2, 1.0, 0.7, 1.0))
        draw_rect(x1 + 18, y2 - 34, x2 - 15, y2 - 35, (0.2, 0.2, 0.2, 0.8))
        
        draw_text(font_id, "🖱️ 移动鼠标 : 将选中点平移并贴合在表面", x1 + 18, y2 - 60, size=12, color=(0.95, 0.95, 0.95, 1.0))
        draw_text(font_id, "✔️ 左键 / 回车 / 空格 : 确认位置", x1 + 18, y2 - 82, size=12, color=(0.95, 0.95, 0.95, 1.0))
        draw_text(font_id, "❌ 右键 / ESC : 取消并恢复原位置", x1 + 18, y2 - 104, size=12, color=(1.0, 0.4, 0.4, 1.0))
    else:
        draw_text(font_id, "TP拓扑模式 (运行中)", x1 + 18, y2 - 28, size=15, color=(0.2, 0.7, 1.0, 1.0))
        draw_rect(x1 + 18, y2 - 34, x2 - 15, y2 - 35, (0.2, 0.2, 0.2, 0.8))
        
        draw_text(font_id, "🖱️ Ctrl + 左键点击 : 在参考表面生成单个顶点", x1 + 18, y2 - 60, size=12, color=(0.95, 0.95, 0.95, 1.0))
        draw_text(font_id, "✍️ Ctrl + 左键滑动 : 绘制连续的点线 (符合空间间距)", x1 + 18, y2 - 82, size=12, color=(0.95, 0.95, 0.95, 1.0))
        draw_text(font_id, "🖱️ Ctrl + 鼠标滚轮 : 调节选中循环边的点数", x1 + 18, y2 - 104, size=12, color=(0.95, 0.95, 0.95, 1.0))
        draw_text(font_id, "🧭 中键/滚轮/导航 Gizmo : 自由旋转平移缩放", x1 + 18, y2 - 126, size=12, color=(0.7, 0.7, 0.7, 1.0))
        draw_text(font_id, "❌ ESC 键 : 退出拓扑模式并保存结果", x1 + 18, y2 - 148, size=12, color=(1.0, 0.4, 0.4, 1.0))
        
    # 实时的所有选中的连线点数视口内悬浮显示 (绿色文字)
    if hasattr(self, 'topo_obj') and self.topo_obj:
        try:
            me = self.topo_obj.data
            if self.topo_obj.mode == 'EDIT':
                bm = bmesh.from_edit_mesh(me)
                loops = self.get_selected_loops(bm)
                selected_verts = [v for v in bm.verts if v.select]
                selected_count = len(selected_verts)
                
                if selected_count > 0:
                    if len(loops) == 1:
                        # 只有一个选中环/链，在环/链的中心显示其点数
                        path, is_closed = loops[0]
                        matrix_world = self.topo_obj.matrix_world
                        sum_cos = mathutils.Vector((0.0, 0.0, 0.0))
                        for idx in path:
                            sum_cos += bm.verts[idx].co
                        center_world = matrix_world @ (sum_cos / len(path))
                        
                        region = context.region
                        rv3d = context.region_data
                        co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, center_world)
                        if co_2d:
                            text = f"点数: {len(path)}"
                            draw_text(font_id, text, co_2d[0] + 17, co_2d[1] - 2, size=30, color=(0.0, 0.0, 0.0, 0.85))
                            draw_text(font_id, text, co_2d[0] + 15, co_2d[1], size=30, color=(0.15, 0.95, 0.35, 1.0))
                    else:
                        # 选中了多个环/链，或者是个网格，或者有散点，只在所有选中点的几何中心显示总点数
                        matrix_world = self.topo_obj.matrix_world
                        sum_cos = mathutils.Vector((0.0, 0.0, 0.0))
                        for v in selected_verts:
                            sum_cos += v.co
                        
                        center_world = matrix_world @ (sum_cos / selected_count)
                        region = context.region
                        rv3d = context.region_data
                        co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, center_world)
                        if co_2d:
                            text = f"点数: {selected_count}"
                            draw_text(font_id, text, co_2d[0] + 17, co_2d[1] - 2, size=30, color=(0.0, 0.0, 0.0, 0.85))
                            draw_text(font_id, text, co_2d[0] + 15, co_2d[1], size=30, color=(0.15, 0.95, 0.35, 1.0))
                        
            # 当按住 Ctrl 或处于 grab_mode 且存在鼠标位置时，绘制吸附圆圈
            show_snap = False
            if getattr(self, 'grab_mode', False):
                show_snap = True
            elif getattr(self, 'ctrl_held', False):
                show_snap = True
                
            if show_snap and hasattr(self, 'mouse_pos'):
                if getattr(self, 'hover_snapped_co', None):
                    sc = self.hover_snapped_co
                    # 绘制吸附提示大圆圈 (橙色，2.5线宽)
                    draw_circle(sc[0], sc[1], 10, (1.0, 0.5, 0.0, 0.95), line_width=2.5)
                    # 绘制中心点
                    draw_circle(sc[0], sc[1], 2, (1.0, 0.5, 0.0, 0.95), line_width=2)
                elif not getattr(self, 'grab_mode', False):
                    m = self.mouse_pos
                    # 绘制跟随小圆圈 (青蓝色，1.5线宽)
                    draw_circle(m[0], m[1], 6, (0.2, 0.7, 1.0, 0.8), line_width=1.5)
                    
            # 如果处于切线/环绕模式，绘制 2D 切线 (亮青蓝色)
            if getattr(self, 'slicing', False) and getattr(self, 'slice_start_pos', None) and getattr(self, 'slice_end_pos', None):
                sp = self.slice_start_pos
                ep = self.slice_end_pos
                draw_line_2d(sp[0], sp[1], ep[0], ep[1], (0.0, 0.9, 1.0, 0.95), line_width=3)
                draw_circle(sp[0], sp[1], 4, (0.0, 0.9, 1.0, 0.95), line_width=2)
                draw_circle(ep[0], ep[1], 4, (0.0, 0.9, 1.0, 0.95), line_width=2)
        except Exception:
            pass


# ==========================================
# 模态操作符 (Modal Operator)
# ==========================================

class VIEW3D_OT_tp_topology_modal(bpy.types.Operator):
    bl_idname = "view3d.tp_topology_modal"
    bl_label = "开启拓扑"
    bl_description = "在所选网格物体表面以点击或拖拽方式绘制点线拓扑"
    bl_options = {'REGISTER', 'UNDO'}
    
    stop: bpy.props.BoolProperty(
        name="停止拓扑",
        default=False,
        options={'SKIP_SAVE'}
    )
    
    def modal(self, context, event):
        if context.area:
            context.area.tag_redraw()
            
        self.ctrl_held = event.ctrl
        self.mouse_pos = (event.mouse_region_x, event.mouse_region_y)
        
        # 释放 Ctrl 键时，清除吸附状态
        if not self.ctrl_held:
            self.snapped_vert_idx = None
            self.hover_snapped_co = None
        else:
            # 按住 Ctrl 时，根据是否已经吸附来进行更新和计算
            self.hover_snapped_co = None
            if not getattr(self, 'grab_mode', False) and context.object and context.object.mode == 'EDIT':
                try:
                    region = None
                    if context.area:
                        for r in context.area.regions:
                            if r.type == 'WINDOW':
                                region = r
                                break
                    if not region:
                        region = context.region
                    
                    me = self.topo_obj.data
                    bm = bmesh.from_edit_mesh(me)
                    rv3d = context.region_data
                    
                    # 1. 如果当前已经处于吸附状态，检测是否仍满足吸附条件 (磁性保持)
                    if getattr(self, 'snapped_vert_idx', None) is not None:
                        try:
                            bm.verts.ensure_lookup_table()
                            snapped_v = None
                            for v in bm.verts:
                                if v.index == self.snapped_vert_idx:
                                    snapped_v = v
                                    break
                            
                            if snapped_v:
                                world_pos = self.topo_obj.matrix_world @ snapped_v.co
                                co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, world_pos)
                                if co_2d:
                                    vx, vy = co_2d[0], co_2d[1]
                                    rx, ry = self.mouse_pos
                                    dist = ((rx - vx)**2 + (ry - vy)**2)**0.5
                                    if dist < 10.0:
                                        # 保持吸附，并将悬停圆圈绘制在顶点屏幕位置
                                        self.hover_snapped_co = (vx, vy)
                                    else:
                                        # 距离超过 10 像素，脱离吸附
                                        self.snapped_vert_idx = None
                                        self.hover_snapped_co = None
                                else:
                                    self.snapped_vert_idx = None
                                    self.hover_snapped_co = None
                            else:
                                self.snapped_vert_idx = None
                                self.hover_snapped_co = None
                        except Exception:
                            self.snapped_vert_idx = None
                            self.hover_snapped_co = None
                            
                    # 2. 如果未吸附，在 10 像素范围内寻找可吸附的目标顶点
                    if getattr(self, 'snapped_vert_idx', None) is None:
                        # 排除当前绘制中的前一顶点和邻接顶点，防止在绘制过程中反向吸附
                        exclude_verts = set()
                        if getattr(self, 'drawing', False) and getattr(self, 'prev_vert_idx', -1) != -1:
                            bm.verts.ensure_lookup_table()
                            if self.prev_vert_idx < len(bm.verts):
                                p_v = bm.verts[self.prev_vert_idx]
                                exclude_verts.add(p_v)
                                for edge in p_v.link_edges:
                                    exclude_verts.update(edge.verts)
                                    
                        nearest_v = self.find_nearest_topo_vert(context, self.mouse_pos, bm, exclude_verts=exclude_verts, max_dist=10.0)
                        if nearest_v:
                            world_pos = self.topo_obj.matrix_world @ nearest_v.co
                            co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, world_pos)
                            if co_2d:
                                vx, vy = co_2d[0], co_2d[1]
                                self.snapped_vert_idx = nearest_v.index
                                self.hover_snapped_co = (vx, vy)
                                # 物理吸附：单次将真实鼠标 Warp 到顶点位置
                                rx, ry = self.mouse_pos
                                if abs(rx - vx) > 0.5 or abs(ry - vy) > 0.5:
                                    try:
                                        context.window.cursor_warp(int(region.x + vx), int(region.y + vy))
                                    except Exception:
                                        pass
                except Exception:
                    pass
            
        # 安全性检测：检查拓扑对象和参考对象是否被意外删除 (防止 ReferenceError)
        topo_exists = False
        ref_exists = False
        if hasattr(self, 'topo_obj') and self.topo_obj:
            try:
                topo_exists = self.topo_obj.name in bpy.data.objects
            except ReferenceError:
                topo_exists = False
        if hasattr(self, 'ref_obj') and self.ref_obj:
            try:
                ref_exists = self.ref_obj.name in bpy.data.objects
            except ReferenceError:
                ref_exists = False
                
        if not topo_exists or not ref_exists:
            self.exit_modal(context)
            self.report({'WARNING'}, "拓扑模型或参考模型已被删除，模态已退出")
            return {'CANCELLED'}
            
        if not context.object or context.object.mode != 'EDIT':
            self.exit_modal(context)
            self.report({'WARNING'}, "已退出编辑模式，拓扑模态中止")
            return {'CANCELLED'}
            
        # 如果不是通过 UI 按钮停止了拓扑
        if not context.window_manager.tp_topology_running:
            self.exit_modal(context)
            return {'FINISHED'}
            
        # 当释放 Ctrl 键，或者点击鼠标，或者进入移动模式时，重置循环边滚动调节会话以防畸变并提交单次历史状态
        if not event.ctrl or (event.type in {'LEFTMOUSE', 'RIGHTMOUSE'} and event.value == 'PRESS') or getattr(self, 'grab_mode', False):
            if getattr(self, 'scroll_active', False):
                print("DEBUG: Scroll session reset!")
                self.scroll_active = False
                bpy.ops.ed.undo_push(message="TP拓扑调整点数")
            
        # 0. 确认抓取后的左键释放事件过滤
        if getattr(self, 'eat_left_release', False):
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self.eat_left_release = False
                return {'RUNNING_MODAL'}

        # 1. 抓取/移动模式逻辑
        if getattr(self, 'grab_mode', False):
            if event.type == 'MOUSEMOVE':
                dx = event.mouse_region_x - self.grab_initial_mouse[0]
                dy = event.mouse_region_y - self.grab_initial_mouse[1]
                
                region = None
                if context.area:
                    for r in context.area.regions:
                        if r.type == 'WINDOW':
                            region = r
                            break
                if not region:
                    region = context.region
                rv3d = context.region_data
                
                me = self.topo_obj.data
                bm = bmesh.from_edit_mesh(me)
                bm.verts.ensure_lookup_table()
                
                matrix_world = self.topo_obj.matrix_world
                ref_matrix_world = self.ref_obj.matrix_world
                ref_matrix_world_inv = ref_matrix_world.inverted()
                topo_matrix_world_inv = matrix_world.inverted()
                
                # 计算中心的新屏幕坐标
                new_center_x = self.grab_initial_center_screen[0] + dx
                new_center_y = self.grab_initial_center_screen[1] + dy
                
                # 从新屏幕坐标投射射线，寻找新的中心点和表面法线
                ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (new_center_x, new_center_y))
                ray_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, (new_center_x, new_center_y))
                
                ray_origin_local = ref_matrix_world_inv @ ray_origin
                ray_vector_local = ref_matrix_world_inv.to_3x3() @ ray_vector
                
                success, location_local, normal_local, index = self.ref_obj.ray_cast(ray_origin_local, ray_vector_local)
                
                if success:
                    C_new = ref_matrix_world @ location_local
                    N_new = (ref_matrix_world.to_3x3() @ normal_local).normalized()
                else:
                    # 射线未击中，退化到使用上一帧中心点附近的最近表面点
                    v_local_ref = ref_matrix_world_inv @ self.last_center_world
                    succ, loc_local, norm_local, idx = self.ref_obj.closest_point_on_mesh(v_local_ref)
                    if succ:
                        C_new = ref_matrix_world @ loc_local
                        N_new = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                    else:
                        C_new = self.last_center_world
                        N_new = self.last_normal_world
                        
                # 保存这一帧的结果作为缓存
                self.last_center_world = C_new.copy()
                self.last_normal_world = N_new.copy()
                
                # 计算法线旋转差以保持相对于新法线的方向
                rotation = self.grab_initial_normal.rotation_difference(N_new)
                
                # 临时存储每个顶点的初始投射世界坐标
                snapped_positions = {}
                self.grab_last_rigids = {} # 记录这一帧的未投影刚性位置，用于确认时更新平整参考坐标
                
                # 对所有选中顶点应用刚性空间变换，并沿视线投影至参考表面，确保点始终贴合表面且无折角畸变
                for v_idx in self.grab_vert_indices:
                    if v_idx < len(bm.verts):
                        # 从平整参考坐标计算偏移，避免累积折边弯曲畸变
                        flat_co = self.vert_flat_co.get(v_idx, bm.verts[v_idx].co)
                        offset = (matrix_world @ flat_co) - self.grab_initial_center
                        
                        if offset:
                            # 1. 计算刚性平移和旋转后的世界位置
                            pos_world_rigid = C_new + rotation @ offset
                            self.grab_last_rigids[v_idx] = pos_world_rigid.copy()
                            
                            # 2. 将此位置投影到当前视口屏幕空间，以进行视线投影，防止边界折角聚拢畸变
                            co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, pos_world_rigid)
                            
                            pos_world_snapped = None
                            if co_2d:
                                # 从屏幕位置朝模型发射投射射线
                                ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (co_2d[0], co_2d[1]))
                                ray_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, (co_2d[0], co_2d[1]))
                                
                                ray_origin_local = ref_matrix_world_inv @ ray_origin
                                ray_vector_local = ref_matrix_world_inv.to_3x3() @ ray_vector
                                
                                success, location_local, normal_local, index = self.ref_obj.ray_cast(ray_origin_local, ray_vector_local)
                                if success:
                                    location = ref_matrix_world @ location_local
                                    normal = (ref_matrix_world.to_3x3() @ normal_local).normalized()
                                    pos_world_snapped = location + normal * 0.001
                                    
                            # 3. 如果视向投射未击中（例如拖出模型轮廓外），则降级使用最近点投影
                            if pos_world_snapped is None:
                                local_pos_ref = ref_matrix_world_inv @ pos_world_rigid
                                succ, loc_local, norm_local, idx = self.ref_obj.closest_point_on_mesh(local_pos_ref)
                                if succ:
                                    location = ref_matrix_world @ loc_local
                                    normal = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                                    pos_world_snapped = location + normal * 0.001
                                else:
                                    pos_world_snapped = pos_world_rigid + N_new * 0.001
                                    
                            snapped_positions[v_idx] = pos_world_snapped.copy()
                            
                # 运行基于物理弹簧长度约束的松弛算法，使顶点能自然越过并包覆折角
                if hasattr(self, 'grab_edge_lengths') and self.grab_edge_lengths:
                    iterations = 10
                    for _ in range(iterations):
                        # 计算物理弹簧位移量
                        displacements = {v_idx: mathutils.Vector((0.0, 0.0, 0.0)) for v_idx in self.grab_vert_indices}
                        counts = {v_idx: 0 for v_idx in self.grab_vert_indices}
                        
                        for (v1_idx, v2_idx), target_len in self.grab_edge_lengths.items():
                            p1 = snapped_positions.get(v1_idx)
                            p2 = snapped_positions.get(v2_idx)
                            if p1 and p2:
                                diff = p2 - p1
                                curr_len = diff.length
                                if curr_len > 1e-6:
                                    error = curr_len - target_len
                                    disp = 0.5 * error * (diff / curr_len)
                                    displacements[v1_idx] += disp
                                    displacements[v2_idx] -= disp
                                    counts[v1_idx] += 1
                                    counts[v2_idx] += 1
                                    
                        # 应用位移并重新投影贴合到最邻近表面
                        for v_idx in self.grab_vert_indices:
                            if counts[v_idx] > 0:
                                avg_disp = displacements[v_idx] / counts[v_idx]
                                new_pos = snapped_positions[v_idx] + avg_disp * 0.8
                                
                                local_pos_ref = ref_matrix_world_inv @ new_pos
                                succ, loc_local, norm_local, idx = self.ref_obj.closest_point_on_mesh(local_pos_ref)
                                if succ:
                                    location = ref_matrix_world @ loc_local
                                    normal = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                                    snapped_positions[v_idx] = location + normal * 0.001
                                else:
                                    snapped_positions[v_idx] = new_pos
                                    
                # 4. 转换回拓扑物体的局部坐标并更新顶点位置
                for v_idx in self.grab_vert_indices:
                    if v_idx < len(bm.verts):
                        v = bm.verts[v_idx]
                        pos_world_snapped = snapped_positions.get(v_idx)
                        if pos_world_snapped:
                            v.co = topo_matrix_world_inv @ pos_world_snapped
                                    
                # 5. 如果是移动单个点，处理吸附与对齐逻辑 (不 Warp 鼠标，避免破坏 dx/dy 位移矩阵)
                self.grab_snapped_to_vert_idx = None
                self.hover_snapped_co = None
                
                if getattr(self, 'grab_single_vert_idx', None) is not None:
                    v_idx = self.grab_single_vert_idx
                    if v_idx < len(bm.verts):
                        moving_v = bm.verts[v_idx]
                        rx, ry = self.mouse_pos
                        
                        # 寻找最邻近顶点，排除自身
                        nearest_v = self.find_nearest_topo_vert(context, (rx, ry), bm, exclude_verts={moving_v}, max_dist=10.0)
                        if nearest_v:
                            world_pos = self.topo_obj.matrix_world @ nearest_v.co
                            co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, world_pos)
                            if co_2d:
                                vx, vy = co_2d[0], co_2d[1]
                                self.grab_snapped_to_vert_idx = nearest_v.index
                                self.hover_snapped_co = (vx, vy)
                                
                                # 坐标直接对齐吸附目标点
                                moving_v.co = nearest_v.co.copy()
                                    
                bmesh.update_edit_mesh(self.topo_obj.data)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            elif (event.type == 'LEFTMOUSE' and event.value == 'PRESS') or event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
                me = self.topo_obj.data
                bm = bmesh.from_edit_mesh(me)
                bm.verts.ensure_lookup_table()
                
                # 如果处于吸附合并状态，执行焊接合并
                weld_done = False
                if getattr(self, 'grab_snapped_to_vert_idx', None) is not None:
                    moving_idx = self.grab_single_vert_idx
                    target_idx = self.grab_snapped_to_vert_idx
                    if moving_idx < len(bm.verts) and target_idx < len(bm.verts):
                        v_moving = bm.verts[moving_idx]
                        v_target = bm.verts[target_idx]
                        
                        # 执行焊接
                        bmesh.ops.weld_verts(bm, targetmap={v_moving: v_target})
                        bm.verts.index_update()
                        bm.edges.index_update()
                        
                        # 确保选中合并后的顶点并清除被删点的坐标缓存
                        v_target.select = True
                        bm.select_history.add(v_target)
                        self.vert_flat_co[v_target.index] = v_target.co.copy()
                        self.vert_flat_co.pop(moving_idx, None)
                        
                        # 更新活跃点记录
                        self.prev_vert_idx = v_target.index
                        self.last_world_pos = (self.topo_obj.matrix_world @ v_target.co).copy()
                        weld_done = True
                
                if not weld_done:
                    # 正常确认，更新物理平整参考坐标
                    matrix_world = self.topo_obj.matrix_world
                    topo_matrix_world_inv = matrix_world.inverted()
                    for v_idx, rigid_pos in getattr(self, 'grab_last_rigids', {}).items():
                        if v_idx in self.vert_flat_co:
                            self.vert_flat_co[v_idx] = topo_matrix_world_inv @ rigid_pos
                
                self.grab_mode = False
                self.grab_vert_indices = []
                self.grab_initial_cos = {}
                self.grab_initial_screencos = {}
                self.grab_last_rigids = {}
                self.grab_single_vert_idx = None
                self.grab_snapped_to_vert_idx = None
                
                if event.type == 'LEFTMOUSE':
                    self.eat_left_release = True
                    
                if weld_done:
                    # 强行刷新编辑模式下顶点选择状态
                    bm.select_flush(True)
                    bmesh.update_edit_mesh(me)
                    bpy.ops.ed.undo_push(message="TP拓扑移动并合并")
                else:
                    bmesh.update_edit_mesh(me)
                    bpy.ops.ed.undo_push(message="TP拓扑移动")
                    
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            elif event.type in {'ESC', 'RIGHTMOUSE'}:
                me = self.topo_obj.data
                bm = bmesh.from_edit_mesh(me)
                bm.verts.ensure_lookup_table()
                for v_idx, initial_co in self.grab_initial_cos.items():
                    if v_idx < len(bm.verts):
                        bm.verts[v_idx].co = initial_co
                bmesh.update_edit_mesh(me)
                self.grab_mode = False
                self.grab_vert_indices = []
                self.grab_initial_cos = {}
                self.grab_initial_screencos = {}
                self.grab_last_rigids = {}
                self.grab_single_vert_idx = None
                self.grab_snapped_to_vert_idx = None
                self.hover_snapped_co = None
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            return {'RUNNING_MODAL'}

        # 退出条件检测：仅 ESC 键
        if event.type == 'ESC' and event.value == 'PRESS':
            self.exit_modal(context)
            return {'FINISHED'}

        # 拦截撤销操作以重置上一个顶点索引，并放行给 Blender 处理撤销
        if event.type == 'Z' and event.ctrl and event.value == 'PRESS':
            self.prev_vert_idx = -1
            self.last_world_pos = None
            self.leftover_dist = 0.0
            self.changed = False
            return {'PASS_THROUGH'}

        # 拦截删除和取消全选操作以重置端点索引，防止拓扑链条错乱
        if event.value == 'PRESS':
            if event.type in {'X', 'DEL', 'BACKSPACE'}:
                self.prev_vert_idx = -1
                self.last_world_pos = None
                self.leftover_dist = 0.0
            elif event.type == 'A' and event.alt:
                self.prev_vert_idx = -1
                self.last_world_pos = None
                self.leftover_dist = 0.0

        # 激活 Grab 模式 (按 G 键)
        if event.type == 'G' and event.value == 'PRESS' and not getattr(self, 'grab_mode', False):
            me = self.topo_obj.data
            bm = bmesh.from_edit_mesh(me)
            bm.verts.ensure_lookup_table()
            selected_verts = [v for v in bm.verts if v.select]
            if selected_verts:
                self.grab_mode = True
                # 在进入抓取前，从当前最新的 BMesh 数据库为所有顶点重建平整参考坐标缓存
                # 这能完美消除任何由于外部网格修改（如一键“栅格化”建面、内置细分、撤销等）导致的索引不一致和跳变问题
                self.vert_flat_co = {v.index: v.co.copy() for v in bm.verts}
                
                self.grab_vert_indices = [v.index for v in selected_verts]
                self.grab_initial_cos = {v.index: v.co.copy() for v in selected_verts}
                self.grab_initial_mouse = (event.mouse_region_x, event.mouse_region_y)
                
                # 记录是否为单个点移动，并初始化吸附与合并状态
                if len(self.grab_vert_indices) == 1:
                    self.grab_single_vert_idx = self.grab_vert_indices[0]
                else:
                    self.grab_single_vert_idx = None
                self.grab_snapped_to_vert_idx = None
                
                region = None
                if context.area:
                    for r in context.area.regions:
                        if r.type == 'WINDOW':
                            region = r
                            break
                if not region:
                    region = context.region
                rv3d = context.region_data
                
                # 确保所有选中顶点都在 flat_co 中，并计算选择中心的初始 3D 位置 (基于平整参考坐标以避免畸变)
                matrix_world = self.topo_obj.matrix_world
                sum_cos = mathutils.Vector((0.0, 0.0, 0.0))
                for v in selected_verts:
                    flat_co = self.vert_flat_co.setdefault(v.index, v.co.copy())
                    sum_cos += matrix_world @ flat_co
                self.grab_initial_center = sum_cos / len(selected_verts)
                self.last_center_world = self.grab_initial_center.copy()
                
                # 计算初始法线
                ref_matrix_world = self.ref_obj.matrix_world
                ref_matrix_world_inv = ref_matrix_world.inverted()
                local_pos = ref_matrix_world_inv @ self.grab_initial_center
                success, loc_local, norm_local, idx = self.ref_obj.closest_point_on_mesh(local_pos)
                if success:
                    self.grab_initial_normal = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                else:
                    self.grab_initial_normal = mathutils.Vector((0.0, 0.0, 1.0))
                self.last_normal_world = self.grab_initial_normal.copy()
                
                # 计算选择中心屏幕坐标
                co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, self.grab_initial_center)
                if co_2d:
                    self.grab_initial_center_screen = (co_2d[0], co_2d[1])
                else:
                    self.grab_initial_center_screen = (event.mouse_region_x, event.mouse_region_y)
                
                # 计算每个顶点相对于中心的初始偏移 (世界坐标系下，基于平整参考坐标)
                self.grab_initial_offsets = {}
                for v in selected_verts:
                    flat_co = self.vert_flat_co[v.index]
                    self.grab_initial_offsets[v.index] = (matrix_world @ flat_co) - self.grab_initial_center
                
                # 记录选中顶点之间的初始边长度，这里应该基于平整参考坐标计算，确保边长不包含先前的弯折缩水
                selected_edges = [e for e in bm.edges if e.verts[0].select and e.verts[1].select]
                self.grab_edge_lengths = {}
                for e in selected_edges:
                    v1_idx = e.verts[0].index
                    v2_idx = e.verts[1].index
                    v1_flat = self.vert_flat_co.setdefault(v1_idx, e.verts[0].co.copy())
                    v2_flat = self.vert_flat_co.setdefault(v2_idx, e.verts[1].co.copy())
                    v1_world = matrix_world @ v1_flat
                    v2_world = matrix_world @ v2_flat
                    self.grab_edge_lengths[(v1_idx, v2_idx)] = (v1_world - v2_world).length
                
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

        # UI 区域操作状态追踪：如果在 UI 上按下了左键，在释放前持续放行所有事件（防止拖拽滑块移出面板时画线）
        if getattr(self, 'ui_active', False):
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self.ui_active = False
                self.drawing = False
                self.prev_vert_idx = -1
                self.last_world_pos = None
                self.leftover_dist = 0.0
            return {'PASS_THROUGH'}

        # Gizmo 导航状态追踪：如果在右上角导航 Gizmo 区域按下了左键，在释放前持续放行所有事件
        if getattr(self, 'navigation_active', False):
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self.navigation_active = False
                self.drawing = False
                self.prev_vert_idx = -1
                self.last_world_pos = None
                self.leftover_dist = 0.0
            return {'PASS_THROUGH'}

        # 如果鼠标在侧边栏 (UI 区域)、工具栏 (TOOLS 区域)、顶部栏 (HEADER 区域)、工具设置栏 (TOOL_HEADER 区域) 或 HUD 区域上，放行所有事件，让用户可以正常交互界面
        if (self.is_mouse_in_region(context, event, 'UI') or 
            self.is_mouse_in_region(context, event, 'TOOLS') or 
            self.is_mouse_in_region(context, event, 'HEADER') or
            self.is_mouse_in_region(context, event, 'TOOL_HEADER') or
            self.is_mouse_in_region(context, event, 'HUD')):
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self.ui_active = True
            return {'PASS_THROUGH'}

        # 检查鼠标是否处于视口右上角的导航视口 Gizmo 区域内
        # 获取 UI 缩放比例以实现自适应
        ui_scale = 1.0
        try:
            ui_scale = context.preferences.system.ui_scale
        except AttributeError:
            try:
                ui_scale = context.preferences.view.ui_scale
            except AttributeError:
                pass

        region = context.region
        if region and not getattr(self, 'drawing', False):
            gizmo_w = 85 * ui_scale
            gizmo_h = 290 * ui_scale
            if (event.mouse_region_x > region.width - gizmo_w and 
                event.mouse_region_y > region.height - gizmo_h):
                if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                    self.navigation_active = True
                return {'PASS_THROUGH'}

        # Ctrl + 滚轮调节选中连线路径的点数
        if event.ctrl and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            if event.value == 'PRESS':
                me = self.topo_obj.data
                bm = bmesh.from_edit_mesh(me)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                
                matrix_world = self.topo_obj.matrix_world
                topo_matrix_world_inv = matrix_world.inverted()
                
                # 如果当前没有激活的滚动调节会话，则初始化它并缓存所有选中路径的原始坐标和外部拓扑连接
                if not getattr(self, 'scroll_active', False):
                    loops = self.get_selected_loops(bm)
                    if not loops:
                        return {'RUNNING_MODAL'}
                        
                    self.scroll_active = True
                    print(f"DEBUG: Scroll session started. Selected loops: {len(loops)}")
                    self.scroll_initial_loops = []
                    
                    for path, is_closed in loops:
                        loop_coords = [matrix_world @ bm.verts[idx].co for idx in path]
                        
                        # 自动记录连向外部未选中几何体的边（保护拓扑，后面会自动重新连线）
                        external_connections = []
                        for idx in path:
                            v = bm.verts[idx]
                            for e in v.link_edges:
                                if not e.select:
                                    other_v = e.other_vert(v)
                                    if not other_v.select:
                                        external_connections.append((idx, other_v.co.copy()))
                                        
                        self.scroll_initial_loops.append({
                            'initial_coords': loop_coords,
                            'is_closed': is_closed,
                            'initial_path': path.copy(),
                            'current_path_indices': path.copy(),
                            'initial_N': len(path),
                            'current_N': len(path),
                            'external_connections': external_connections,
                        })
                        
                # 对所有匹配的连线路径同时调整点数
                changed_any = False
                for loop in self.scroll_initial_loops:
                    N = loop['current_N']
                    if event.type == 'WHEELUPMOUSE':
                        new_N = N + 1
                    else:
                        new_N = max(3 if loop['is_closed'] else 2, N - 1)
                        
                    if new_N != N:
                        loop['current_N'] = new_N
                        changed_any = True
                        
                if changed_any:
                    # 清理平整坐标字典中的旧顶点
                    for loop in self.scroll_initial_loops:
                        for idx in loop['current_path_indices']:
                            self.vert_flat_co.pop(idx, None)
                            
                    # 删除当前所有选中的顶点（即上一滚动步生成的顶点）
                    verts_to_delete = [v for v in bm.verts if v.select]
                    print(f"DEBUG: verts_to_delete count: {len(verts_to_delete)}, total verts: {len(bm.verts)}")
                    bmesh.ops.delete(bm, geom=verts_to_delete, context='VERTS')
                    
                    # 重新生成每一个调整后的连线路径并重新建立外部边连接
                    ref_matrix_world = self.ref_obj.matrix_world
                    ref_matrix_world_inv = ref_matrix_world.inverted()
                    
                    created_verts = {}
                    loop_to_new_verts = {}
                    
                    for loop in self.scroll_initial_loops:
                        coords = loop['initial_coords'].copy()
                        is_closed = loop['is_closed']
                        new_N = loop['current_N']
                        initial_path = loop['initial_path']
                        
                        # 线性插值重采样 (保留硬边折角，防止网格收缩变形)
                        new_world_cos, orig_idx_map = self.interpolate_linear(coords, is_closed, new_N)
                        
                        # 投影寻点得到参考表面上的点，然后对其段进行等间距平均化
                        snapped_world_cos = []
                        for i, pos_world in enumerate(new_world_cos):
                            local_pos_ref = ref_matrix_world_inv @ pos_world
                            succ, loc_local, norm_local, idx = self.ref_obj.closest_point_on_mesh(local_pos_ref)
                            if succ:
                                location = ref_matrix_world @ loc_local
                                snapped_world_cos.append(location)
                            else:
                                snapped_world_cos.append(pos_world.copy())
                                
                        if getattr(context.scene, "tp_auto_average_stroke", True):
                            # 动态检测原始顶点中的折角特征点
                            detected_corners = self.detect_corners(coords, is_closed)
                            # 将原始角点索引映射为新顶点列表中的索引
                            new_corner_indices = []
                            for new_i, orig_i in orig_idx_map.items():
                                if orig_i in detected_corners:
                                    new_corner_indices.append(new_i)
                            new_corner_indices = sorted(list(set(new_corner_indices)))
                            
                            # 如果是闭合环且没有角点，默认包含 0 索引以进行全局平均
                            if is_closed and not new_corner_indices:
                                new_corner_indices = [0]
                                
                            averaged_world_cos = self.average_path_segments(snapped_world_cos, new_corner_indices, is_closed)
                        else:
                            averaged_world_cos = snapped_world_cos
                            
                        new_verts = []
                        for i, pos_world in enumerate(averaged_world_cos):
                            orig_idx = None
                            if i in orig_idx_map:
                                orig_idx = initial_path[orig_idx_map[i]]
                                    
                            if orig_idx is not None and orig_idx in created_verts:
                                v = created_verts[orig_idx]
                            else:
                                local_pos_ref = ref_matrix_world_inv @ pos_world
                                succ, loc_local, norm_local, idx = self.ref_obj.closest_point_on_mesh(local_pos_ref)
                                if succ:
                                    location = ref_matrix_world @ loc_local
                                    normal = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                                    pos_world = location + normal * 0.001
                                local_pos = topo_matrix_world_inv @ pos_world
                                
                                v = bm.verts.new(local_pos)
                                v.select = True
                                if orig_idx is not None:
                                    created_verts[orig_idx] = v
                                    
                            new_verts.append(v)
                            
                        # 连接新顶点
                        for i in range(len(new_verts) - 1):
                            v1, v2 = new_verts[i], new_verts[i+1]
                            if not bm.edges.get((v1, v2)) and not bm.edges.get((v2, v1)):
                                bm.edges.new((v1, v2))
                        if is_closed:
                            v1, v2 = new_verts[-1], new_verts[0]
                            if not bm.edges.get((v1, v2)) and not bm.edges.get((v2, v1)):
                                bm.edges.new((v1, v2))
                                
                        loop_to_new_verts[id(loop)] = new_verts
                        
                        # 重新建立外部边连接：找到最邻近的新顶点并连线
                        for old_v_idx, other_v_co in loop['external_connections']:
                            unselected_verts = [v for v in bm.verts if not v.select]
                            if unselected_verts:
                                target_unselected_v = min(unselected_verts, key=lambda v: (v.co - other_v_co).length_squared)
                                if (target_unselected_v.co - other_v_co).length_squared < 1e-6:
                                    old_pos = loop['initial_coords'][loop['initial_path'].index(old_v_idx)]
                                    closest_new_v = min(new_verts, key=lambda nv: (nv.co - topo_matrix_world_inv @ old_pos).length_squared)
                                    if not bm.edges.get((closest_new_v, target_unselected_v)) and not bm.edges.get((target_unselected_v, closest_new_v)):
                                        bm.edges.new((closest_new_v, target_unselected_v))
                                    
                    # 重新计算索引，选中所有的连线边并更新网格
                    bm.verts.index_update()
                    bm.edges.index_update()
                    print(f"DEBUG: After regeneration, total verts: {len(bm.verts)}")
                    
                    for loop in self.scroll_initial_loops:
                        new_verts = loop_to_new_verts[id(loop)]
                        loop['current_path_indices'] = [v.index for v in new_verts]
                        for v in new_verts:
                            self.vert_flat_co[v.index] = v.co.copy()
                            
                    for e in bm.edges:
                        if e.verts[0].select and e.verts[1].select:
                            e.select = True
                            
                    bm.select_flush(True)
                    bmesh.update_edit_mesh(me)
                    context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # 视口导航按键及手势穿透：中键旋转平移、滚轮缩放、小键盘视图切换
        if event.type in {
            'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 
            'NUMPAD_2', 'NUMPAD_4', 'NUMPAD_6', 'NUMPAD_8', 
            'NUMPAD_1', 'NUMPAD_3', 'NUMPAD_7', 'NUMPAD_5', 
            'NUMPAD_9', 'NUMPAD_0', 'NUMPAD_PERIOD'
        }:
            return {'PASS_THROUGH'}
            
        # Alt + 左键点击：在拓扑编辑模式下选择循环边 (Shift+Alt 支持多选/加选)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and event.alt:
            try:
                me = self.topo_obj.data
                bm = bmesh.from_edit_mesh(me)
                bm.verts.ensure_lookup_table()
                
                # 寻找鼠标点击位置附近的最近顶点作为 Loop Select 的种子点
                nearest_v = self.find_nearest_topo_vert(context, self.mouse_pos, bm, max_dist=10.0)
                
                if nearest_v:
                    # 如果没有按住 Shift，先清空其他所有选择
                    if not event.shift:
                        self.deselect_all(bm)
                    
                    # 选中该顶点并将其设为活动对象（Loop Select 的必要条件）
                    nearest_v.select = True
                    bm.select_history.add(nearest_v)
                    bm.select_flush(True)
                    bmesh.update_edit_mesh(me)
                    
                    # 调用 Blender 内置的循环边选择
                    bpy.ops.mesh.loop_select('INVOKE_DEFAULT', extend=event.shift, toggle=True)
                    
                    # 重新获取 BMesh 并同步更新活跃点，保证后续 Ctrl + 滚轮 或 绘制 逻辑正常连接
                    bm = bmesh.from_edit_mesh(me)
                    bm.verts.ensure_lookup_table()
                    
                    active_vert = None
                    if bm.select_history and isinstance(bm.select_history[-1], bmesh.types.BMVert) and bm.select_history[-1].select:
                        active_vert = bm.select_history[-1]
                    else:
                        for v in bm.verts:
                            if v.select:
                                active_vert = v
                                break
                                
                    if active_vert:
                        self.prev_vert_idx = active_vert.index
                        self.last_world_pos = (self.topo_obj.matrix_world @ active_vert.co).copy()
                    else:
                        self.prev_vert_idx = -1
                        self.last_world_pos = None
                        
                    context.area.tag_redraw()
            except Exception as e:
                print("Loop select failed:", e)
            return {'RUNNING_MODAL'}

        # 拦截并处理鼠标左键绘制逻辑 (必须按住 Ctrl 键)
        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                if event.ctrl:
                    # 射线检测鼠标是否在参考网格内
                    region = None
                    if context.area:
                        for r in context.area.regions:
                            if r.type == 'WINDOW':
                                region = r
                                break
                    if not region:
                        region = context.region
                    rv3d = context.region_data
                    mouse_pos = (event.mouse_region_x, event.mouse_region_y)
                    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_pos)
                    ray_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_pos)
                    
                    matrix_world = self.ref_obj.matrix_world
                    matrix_world_inv = matrix_world.inverted()
                    ray_origin_local = matrix_world_inv @ ray_origin
                    ray_vector_local = matrix_world_inv.to_3x3() @ ray_vector
                    
                    success, location_local, normal_local, index = self.ref_obj.ray_cast(ray_origin_local, ray_vector_local)
                    if not success:
                        # 鼠标在物体外：启动环绕切片模式
                        self.slicing = True
                        self.slice_start_pos = mouse_pos
                        self.slice_end_pos = mouse_pos
                        return {'RUNNING_MODAL'}
                    else:
                        # 鼠标在物体内：正常连续点线绘制
                        self.drawing = True
                        self.handle_click_or_drag(context, event, is_drag=False)
                        return {'RUNNING_MODAL'}
                else:
                    # 如果是不按住 Ctrl 的普通左键点击，表示用户在手动选择/取消选择，重置绘制链条端点
                    self.prev_vert_idx = -1
                    self.last_world_pos = None
                    self.leftover_dist = 0.0
                
            elif event.value == 'RELEASE':
                if getattr(self, 'slicing', False):
                    self.slice_end_pos = (event.mouse_region_x, event.mouse_region_y)
                    self.handle_slice(context)
                    self.slicing = False
                    self.slice_start_pos = None
                    self.slice_end_pos = None
                    return {'RUNNING_MODAL'}
                elif getattr(self, 'drawing', False):
                    self.drawing = False
                    self.last_world_pos = None
                    self.leftover_dist = 0.0
                    self.average_current_stroke(context)
                    if getattr(self, 'changed', False):
                        bpy.ops.ed.undo_push(message="TP拓扑绘制")
                        self.changed = False
                    return {'RUNNING_MODAL'}
                
        elif event.type == 'MOUSEMOVE':
            if getattr(self, 'slicing', False):
                self.slice_end_pos = (event.mouse_region_x, event.mouse_region_y)
                return {'RUNNING_MODAL'}
            elif getattr(self, 'drawing', False):
                # 鼠标滑动绘制连续线条
                self.handle_click_or_drag(context, event, is_drag=True)
                return {'RUNNING_MODAL'}
            
        return {'PASS_THROUGH'}
        
    def invoke(self, context, event):
        self._handle = None
        self.drawing_active = False
        
        # 如果传入了 stop=True，则直接停止运行中的模态
        if self.stop:
            context.window_manager.tp_topology_running = False
            return {'FINISHED'}
            
        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "此工具只能在 3D 视图（3D Viewport）中使用！")
            return {'CANCELLED'}
            
        ref_obj = context.active_object
        if not ref_obj or ref_obj.type != 'MESH':
            self.report({'WARNING'}, "请先选中一个网格物体作为拓扑参考！")
            return {'CANCELLED'}
            
        # 切换到物体模式以创建或锁定低模拓扑对象
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
            
        topo_obj_name = "TP_Topology"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        
        if not topo_obj:
            mesh = bpy.data.meshes.new(name=topo_obj_name)
            topo_obj = bpy.data.objects.new(topo_obj_name, mesh)
            context.collection.objects.link(topo_obj)
        
        # 强制同步“显示在最前”状态到当前场景属性的值
        topo_obj.show_in_front = context.scene.tp_show_in_front
        
        for o in context.selected_objects:
            o.select_set(False)
        topo_obj.select_set(True)
        context.view_layer.objects.active = topo_obj
        
        bpy.ops.object.mode_set(mode='EDIT')
        
        # 初始化状态参数
        self.ref_obj = ref_obj
        self.topo_obj = topo_obj
        self.drawing = False
        self.slicing = False
        self.slice_start_pos = None
        self.slice_end_pos = None
        self.drawing_active = True
        self.changed = False
        self.ui_active = False
        self.navigation_active = False
        self.grab_mode = False
        self.grab_vert_indices = []
        self.eat_left_release = False
        self.current_stroke_verts = []
        
        # 吸附状态参数：记录吸附顶点的索引和屏幕坐标，单次 warp 磁性吸附防抖动
        self.snapped_vert_idx = None
        self.hover_snapped_co = None
        
        # 移动点时的吸附与合并状态参数
        self.grab_single_vert_idx = None
        self.grab_snapped_to_vert_idx = None
        
        # 寻找已有选中点作为活跃点，避免重复从头绘制
        me = topo_obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        
        # 初始化所有已有顶点的平整参考坐标以防累积畸变
        self.vert_flat_co = {v.index: v.co.copy() for v in bm.verts}
        
        # 初始化循环边滚动调节会话状态，防止反复来来回回滚动导致的收缩与变形
        self.scroll_active = False
        self.scroll_initial_coords = []
        self.scroll_initial_is_closed = False
        self.scroll_initial_N = 0
        self.scroll_current_N = 0
        self.scroll_path_indices = []
        
        active_vert = None
        if bm.select_history and isinstance(bm.select_history[-1], bmesh.types.BMVert) and bm.select_history[-1].select:
            active_vert = bm.select_history[-1]
        else:
            for v in bm.verts:
                if v.select:
                    active_vert = v
                    break
                    
        if active_vert:
            self.prev_vert_idx = active_vert.index
            self.last_world_pos = (topo_obj.matrix_world @ active_vert.co).copy()
            self.leftover_dist = 0.0
        else:
            bpy.ops.mesh.select_all(action='DESELECT')
            self.prev_vert_idx = -1
            self.last_world_pos = None
            self.leftover_dist = 0.0
        
        context.window_manager.tp_topology_running = True
        context.workspace.status_text_set("TP拓扑: Ctrl+左键点击(画点) | Ctrl+左键拖拽(画线) | 中键导航 | ESC退出")
        
        args = (self, context)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_callback_px, args, 'WINDOW', 'POST_PIXEL'
        )
        
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}
        
    def exit_modal(self, context):
        self.drawing_active = False
        self.ui_active = False
        self.navigation_active = False
        self.grab_mode = False
        self.grab_vert_indices = []
        self.grab_single_vert_idx = None
        self.grab_snapped_to_vert_idx = None
        self.slicing = False
        self.slice_start_pos = None
        self.slice_end_pos = None
        context.window_manager.tp_topology_running = False
        context.workspace.status_text_set(None)
        
        # 磁性吸附退出时无需做额外的强制位置重置，因为用户始终能完全控制真实的鼠标指针
        pass
                
        if self._handle:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
            
        self.report({'INFO'}, "TP拓扑模式已成功退出")
        if context.area:
            context.area.tag_redraw()
            
    def is_mouse_in_region(self, context, event, region_type):
        area = context.area
        if not area or area.type != 'VIEW_3D':
            return False
        for region in area.regions:
            if region.type == region_type:
                if (region.x <= event.mouse_x < region.x + region.width and
                    region.y <= event.mouse_y < region.y + region.height):
                    return True
        return False
        
    def handle_click_or_drag(self, context, event, is_drag=False):
        mouse_pos = (event.mouse_region_x, event.mouse_region_y)
        
        # 计算 3D 空间投射射线 (世界空间)
        region = None
        if context.area:
            for r in context.area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break
        if not region:
            region = context.region
            
        rv3d = context.region_data
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_pos)
        ray_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_pos)
        
        # 将世界坐标系下的射线转换到参考物体的局部坐标系下进行精准射线测试
        # 这样可以 100% 避免射线击中拓扑物体自身或其他无关背景物体
        matrix_world = self.ref_obj.matrix_world
        matrix_world_inv = matrix_world.inverted()
        
        ray_origin_local = matrix_world_inv @ ray_origin
        ray_vector_local = matrix_world_inv.to_3x3() @ ray_vector
        
        success, location_local, normal_local, index = self.ref_obj.ray_cast(ray_origin_local, ray_vector_local)
        
        if success:
            location = matrix_world @ location_local
            normal = (matrix_world.to_3x3() @ normal_local).normalized()
            
            # 打开 bmesh 进行批量点线编辑 (提高性能)
            me = self.topo_obj.data
            bm = bmesh.from_edit_mesh(me)
            bm.verts.ensure_lookup_table()
            
            # 同步当前最新的选择状态作为绘制起点
            active_vert = None
            if bm.select_history and isinstance(bm.select_history[-1], bmesh.types.BMVert) and bm.select_history[-1].select:
                active_vert = bm.select_history[-1]
            else:
                selected_verts = [v for v in bm.verts if v.select]
                if selected_verts:
                    active_vert = selected_verts[-1]
                    
            if active_vert:
                self.prev_vert_idx = active_vert.index
            # 注意：若当前无任何选中点，不要盲目将 prev_vert_idx 重置为 -1，
            # 只有当 prev_vert_idx 确实超界或在 modal 中被显式重置时，才为 -1，
            # 从而防止由于 Blender 撤销或内部重构导致的选择状态短暂丢失。
                
            prev_vert = None
            if getattr(self, 'prev_vert_idx', -1) != -1:
                if self.prev_vert_idx < len(bm.verts):
                    prev_vert = bm.verts[self.prev_vert_idx]
                else:
                    self.prev_vert_idx = -1
                
            # 内部辅助函数：在指定的世界空间坐标处生成拓扑点，并与前一点相连
            def add_vertex_local(pos):
                nonlocal prev_vert
                # 将生成的点沿法线方向向外微偏移 0.001 (1毫米) 避免视口闪烁
                offset_loc = pos + normal * 0.001
                # 将世界坐标转换为拓扑物体的局部坐标
                local_pos = self.topo_obj.matrix_world.inverted() @ offset_loc
                
                target_vert = bm.verts.new(local_pos)
                target_vert.select = True
                bm.select_history.add(target_vert)
                self.changed = True
                
                # 记录初始平整参考坐标
                self.vert_flat_co[target_vert.index] = local_pos.copy()
                

                
                if prev_vert:
                    bm.edges.new((prev_vert, target_vert))
                prev_vert = target_vert
            
            # 定义排除集合，避免吸附到当前活跃点及其直接邻接点
            exclude_verts = set()
            if prev_vert:
                exclude_verts.add(prev_vert)
                for edge in prev_vert.link_edges:
                    exclude_verts.update(edge.verts)
            
            # 检测鼠标当前位置是否靠近已有的顶点
            snapped_vert = self.find_nearest_topo_vert(context, mouse_pos, bm, exclude_verts=exclude_verts)
            
            if snapped_vert:
                # 无论点击还是拖拽，只要靠近已有顶点就直接吸附并闭合/连接
                if prev_vert:
                    # 如果尚未连接，则创建新边
                    edge_exists = False
                    for edge in snapped_vert.link_edges:
                        if prev_vert in edge.verts:
                            edge_exists = True
                            break
                    if not edge_exists:
                        bm.edges.new((prev_vert, snapped_vert))
                        self.changed = True
                else:
                    if not is_drag:
                        self.deselect_all(bm)
                
                snapped_vert.select = True
                bm.select_history.add(snapped_vert)
                prev_vert = snapped_vert
                self.last_world_pos = (self.topo_obj.matrix_world @ snapped_vert.co).copy()
                self.leftover_dist = 0.0
                

            else:
                # 没有检测到吸附，执行常规的绘制或插值逻辑
                if not is_drag or getattr(self, 'last_world_pos', None) is None:
                    # 笔画起点或单点单击
                    if not is_drag:
                        self.deselect_all(bm)
                        if prev_vert:
                            prev_vert.select = True
                            bm.select_history.add(prev_vert)
                    add_vertex_local(location)
                    self.last_world_pos = location.copy()
                    self.leftover_dist = 0.0
                else:
                    # 拖拽绘制过程：进行精确的 3D 空间插值，解决因鼠标采样率不足导致的间距过大问题
                    segment_vec = location - self.last_world_pos
                    segment_len = segment_vec.length
                    
                    # 读取场景设置中的间距参数 (米)
                    spacing = getattr(context.scene, "tp_fixed_spacing_dist", 0.1)
                    
                    needed_dist = spacing - getattr(self, 'leftover_dist', 0.0)
                    if segment_len >= needed_dist:
                        direction = segment_vec.normalized()
                        curr_pos = self.last_world_pos + direction * needed_dist
                        
                        # 生成第一个插值点
                        add_vertex_local(curr_pos)
                        
                        # 根据视线与表面法线的夹角，限制单帧最大生成点数，防止在极斜面上因为深度剧烈变化而生成过多重叠点
                        view_dot = abs(ray_vector.normalized().dot(normal))
                        max_points = 1 if view_dot < 0.25 else 5
                        
                        # 生成后续所有符合固定间距的插值点
                        remaining_len = segment_len - needed_dist
                        count = 0
                        while remaining_len >= spacing and count < max_points:
                            curr_pos = curr_pos + direction * spacing
                            add_vertex_local(curr_pos)
                            remaining_len -= spacing
                            count += 1
                            
                        self.leftover_dist = remaining_len
                        self.last_world_pos = location.copy()
                    else:
                        # 距离不足，仅累加线段长度
                        self.leftover_dist = getattr(self, 'leftover_dist', 0.0) + segment_len
                        self.last_world_pos = location.copy()
            
            bm.select_flush(True)
            bm.verts.index_update()
            if prev_vert:
                self.prev_vert_idx = prev_vert.index
            bmesh.update_edit_mesh(me)
        else:
            # 当拖拽绘制时，如果射线未击中表面，则不重置绘制状态，只略过此帧，以防边缘抖动产生断线和孤立点
            pass
            
    def handle_slice(self, context):
        if not getattr(self, 'slice_start_pos', None) or not getattr(self, 'slice_end_pos', None):
            return
            
        sp = self.slice_start_pos
        ep = self.slice_end_pos
        
        dist_px = ((sp[0] - ep[0])**2 + (sp[1] - ep[1])**2)**0.5
        if dist_px < 10.0:
            return
            
        region = None
        if context.area:
            for r in context.area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break
        if not region:
            region = context.region
            
        rv3d = context.region_data
        
        O1 = view3d_utils.region_2d_to_origin_3d(region, rv3d, sp)
        D1 = view3d_utils.region_2d_to_vector_3d(region, rv3d, sp)
        
        O2 = view3d_utils.region_2d_to_origin_3d(region, rv3d, ep)
        D2 = view3d_utils.region_2d_to_vector_3d(region, rv3d, ep)
        
        V1 = D1.normalized()
        V2 = ((O2 - O1) + D2).normalized()
        
        plane_no_world = V1.cross(V2)
        if plane_no_world.length_squared < 1e-8:
            plane_no_world = V1.orthogonal()
        else:
            plane_no_world.normalize()
            
        plane_co_world = O1
        
        matrix_world = self.ref_obj.matrix_world
        matrix_world_inv = matrix_world.inverted()
        
        plane_co_local = matrix_world_inv @ plane_co_world
        plane_no_local = (matrix_world_inv.to_3x3() @ plane_no_world).normalized()
        
        ref_me = self.ref_obj.data
        ref_bm = bmesh.new()
        ref_bm.from_mesh(ref_me)
        ref_bm.verts.ensure_lookup_table()
        ref_bm.edges.ensure_lookup_table()
        ref_bm.faces.ensure_lookup_table()
        
        geom = ref_bm.verts[:] + ref_bm.edges[:] + ref_bm.faces[:]
        try:
            res = bmesh.ops.bisect_plane(
                ref_bm,
                geom=geom,
                plane_co=plane_co_local,
                plane_no=plane_no_local,
                clear_outer=False,
                clear_inner=False
            )
        except Exception as e:
            print("Bisect operation failed:", e)
            ref_bm.free()
            return
            
        cut_edges = [x for x in res['geom_cut'] if isinstance(x, bmesh.types.BMEdge)]
        if not cut_edges:
            ref_bm.free()
            self.report({'INFO'}, "切片未穿过参考物体表面，未建立环绕圈")
            return
            
        from collections import defaultdict
        adj = defaultdict(list)
        for edge in cut_edges:
            v1, v2 = edge.verts
            adj[v1].append(v2)
            adj[v2].append(v1)
            
        visited = set()
        paths = []
        
        for v in list(adj.keys()):
            if len(adj[v]) == 1 and v not in visited:
                path = [v]
                visited.add(v)
                curr = v
                while True:
                    next_v = None
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            next_v = neighbor
                            break
                    if next_v is None:
                        break
                    path.append(next_v)
                    visited.add(next_v)
                    curr = next_v
                paths.append((path, False))
                
        for v in list(adj.keys()):
            if v not in visited:
                path = [v]
                visited.add(v)
                curr = v
                is_closed = False
                while True:
                    next_v = None
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            next_v = neighbor
                            break
                    if next_v is None:
                        if path[0] in adj[curr] and len(path) >= 3:
                            is_closed = True
                        break
                    path.append(next_v)
                    visited.add(next_v)
                    curr = next_v
                paths.append((path, is_closed))
                
        paths_coords = []
        for path, is_closed in paths:
            coords = [v.co.copy() for v in path]
            paths_coords.append((coords, is_closed))
            
        ref_bm.free()
        
        me = self.topo_obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        
        self.deselect_all(bm)
        
        topo_matrix_world = self.topo_obj.matrix_world
        topo_matrix_world_inv = topo_matrix_world.inverted()
        
        ref_matrix_world = self.ref_obj.matrix_world
        ref_matrix_world_inv = ref_matrix_world.inverted()
        
        # 使用 evaluated_depsgraph_get 评估参考物体以防止 background/headless 模式下 closest_point_on_mesh 报错
        ref_obj_eval = self.ref_obj
        try:
            dg = context.evaluated_depsgraph_get()
            ref_obj_eval = self.ref_obj.evaluated_get(dg)
        except Exception:
            pass
        
        created_loops_count = 0
        created_verts = []
        
        for path_coords, is_closed in paths_coords:
            if len(path_coords) < 2:
                continue
                
            new_verts = []
            for co_local_ref in path_coords:
                co_world = ref_matrix_world @ co_local_ref
                
                co_local_ref_query = ref_matrix_world_inv @ co_world
                succ, loc_local, norm_local, idx = ref_obj_eval.closest_point_on_mesh(co_local_ref_query)
                if succ:
                    co_world = ref_matrix_world @ loc_local
                    normal = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                    co_world += normal * 0.001
                    
                co_local_topo = topo_matrix_world_inv @ co_world
                
                v_new = bm.verts.new(co_local_topo)
                v_new.select = True
                new_verts.append(v_new)
                created_verts.append(v_new)
                
                self.vert_flat_co[v_new.index] = co_local_topo.copy()
                
            for i in range(len(new_verts) - 1):
                v1, v2 = new_verts[i], new_verts[i+1]
                bm.edges.new((v1, v2))
                
            if is_closed and len(new_verts) >= 3:
                v1, v2 = new_verts[-1], new_verts[0]
                bm.edges.new((v1, v2))
                
            created_loops_count += 1
            
        if created_loops_count > 0:
            bm.verts.index_update()
            bm.edges.index_update()
            
            for v in created_verts:
                bm.select_history.add(v)
                
            if created_verts:
                self.prev_vert_idx = created_verts[-1].index
                
            bm.select_flush(True)
            bmesh.update_edit_mesh(me)
            
            bpy.ops.ed.undo_push(message="TP拓扑环绕切割")
            self.report({'INFO'}, f"成功生成了 {created_loops_count} 个环绕拓扑圈！")
            context.area.tag_redraw()
        else:
            bmesh.update_edit_mesh(me)
                
    def find_nearest_topo_vert(self, context, mouse_pos, bm, exclude_verts=None, max_dist=10.0):
        region = None
        if context.area:
            for r in context.area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break
        if not region:
            region = context.region
            
        rv3d = context.region_data
        nearest_v = None
        min_dist = max_dist
        
        matrix_world = self.topo_obj.matrix_world
        ref_world = self.ref_obj.matrix_world if self.ref_obj else None
        ref_world_inv = ref_world.inverted() if ref_world else None
        
        for v in bm.verts:
            if exclude_verts and v in exclude_verts:
                continue
            world_pos = matrix_world @ v.co
            co_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, world_pos)
            if co_2d:
                dx = mouse_pos[0] - co_2d[0]
                dy = mouse_pos[1] - co_2d[1]
                dist = (dx**2 + dy**2) ** 0.5
                if dist < min_dist:
                    # 进行遮挡性（检测是否被高模遮挡）过滤，防止吸附到背面或视线外的顶点
                    is_occluded = False
                    if self.ref_obj and ref_world_inv:
                        ray_orig = view3d_utils.region_2d_to_origin_3d(region, rv3d, co_2d)
                        ray_dir = view3d_utils.region_2d_to_vector_3d(region, rv3d, co_2d)
                        
                        # 转换到参考物体局部空间做 ray_cast
                        ray_orig_local = ref_world_inv @ ray_orig
                        ray_dir_local = ref_world_inv.to_3x3() @ ray_dir
                        
                        success, hit_loc_local, _, _ = self.ref_obj.ray_cast(ray_orig_local, ray_dir_local)
                        if success:
                            hit_loc_world = ref_world @ hit_loc_local
                            dist_to_hit = (hit_loc_world - ray_orig).length
                            dist_to_vert = (world_pos - ray_orig).length
                            
                            # 留出 1 毫米容差以避免贴合在表面上的顶点由于浮点数误差被过滤掉
                            if dist_to_hit < dist_to_vert - 0.001:
                                is_occluded = True
                                
                    if is_occluded:
                        continue
                        
                    min_dist = dist
                    nearest_v = v
        return nearest_v
        
    def deselect_all(self, bm):
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
        bm.select_history.clear()

    def get_selected_loops(self, bm):
        """寻找当前选中几何体中的所有简单单连通循环边或连线路径（支持多条、开路与闭合）"""
        selected_verts = [v for v in bm.verts if v.select]
        if not selected_verts:
            return []
            
        adj = {v.index: [] for v in selected_verts}
        selected_edges = [e for e in bm.edges if e.select]
        
        for e in selected_edges:
            v1, v2 = e.verts[0], e.verts[1]
            if v1.select and v2.select:
                adj[v1.index].append(v2.index)
                adj[v2.index].append(v1.index)
                
        visited = set()
        components = []
        
        for v_idx in adj:
            if v_idx not in visited:
                comp = []
                queue = [v_idx]
                visited.add(v_idx)
                while queue:
                    curr = queue.pop(0)
                    comp.append(curr)
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append(neighbor)
                components.append(comp)
                
        valid_loops = []
        for comp in components:
            comp_set = set(comp)
            degrees = {v_idx: len([n for n in adj[v_idx] if n in comp_set]) for v_idx in comp}
            
            # 找到度数不为2的所有点（端点或交叉分叉点）
            J = {v_idx for v_idx, d in degrees.items() if d != 2}
            
            if not J:
                # 这是一个单纯的闭合环（所有点度数都为2）
                if len(comp) >= 3:
                    start_idx = comp[0]
                    comp_visited = set()
                    path = []
                    curr = start_idx
                    while curr is not None and curr not in comp_visited:
                        comp_visited.add(curr)
                        path.append(curr)
                        next_nodes = [n for n in adj[curr] if n in comp_set and n not in comp_visited]
                        if next_nodes:
                            curr = next_nodes[0]
                        else:
                            break
                    if len(path) == len(comp):
                        valid_loops.append((path, True))
            else:
                # 包含端点或交叉点，追踪J点之间的路径
                traced_paths = []
                for u in J:
                    if degrees[u] > 0:
                        for v in adj[u]:
                            if v not in comp_set:
                                continue
                            # 从 u -> v 开始追踪路径
                            path = [u, v]
                            if v in J:
                                # 直连的两个J点
                                traced_paths.append((path, False))
                            else:
                                prev = u
                                curr = v
                                while curr not in J:
                                    next_nodes = [n for n in adj[curr] if n in comp_set and n != prev]
                                    if next_nodes:
                                        prev = curr
                                        curr = next_nodes[0]
                                        path.append(curr)
                                    else:
                                        break
                                traced_paths.append((path, False))
                                
                # 去重
                unique_paths = []
                seen = set()
                for path, _ in traced_paths:
                    if len(path) < 2:
                        continue
                    if path[0] == path[-1]:
                        # 这是一个起止于同一个分叉点的环
                        if len(path) >= 4:
                            if path[1] < path[-2]:
                                canonical = path[:-1]
                                canonical_tuple = tuple(canonical)
                                if canonical_tuple not in seen:
                                    seen.add(canonical_tuple)
                                    unique_paths.append((canonical, True))
                    else:
                        # 开路路径，通过排序其反向路径来进行唯一化
                        rev_path = list(reversed(path))
                        canonical = path if path < rev_path else rev_path
                        canonical_tuple = tuple(canonical)
                        if canonical_tuple not in seen:
                            seen.add(canonical_tuple)
                            unique_paths.append((canonical, False))
                            
                valid_loops.extend(unique_paths)
                
        return valid_loops

    def interpolate_linear(self, points, is_closed, num_samples):
        """对 3D 顶点列表使用线性插值进行等间距重采样，保留硬表面折角特征，防止收缩变形"""
        if len(points) < 2:
            return [p.copy() for p in points], {i: i for i in range(len(points))}
            
        M = len(points)
        # 1. 如果采样点数大于或等于原始点数，采用按段分配细分，保留全部原始特征角点
        if num_samples >= M:
            # 段数：闭合环为 M 段，开路为 M - 1 段
            K_seg = M if is_closed else M - 1
            # 目标细分后的总段数
            S_target = num_samples if is_closed else num_samples - 1
            
            # 计算每段的原始长度
            lengths = []
            for i in range(M - 1):
                lengths.append((points[i+1] - points[i]).length)
            if is_closed:
                lengths.append((points[0] - points[-1]).length)
                
            # 初始分配：每段至少细分为 1 个子段
            k = [1] * K_seg
            remaining = S_target - K_seg
            
            # 贪心分配剩下的细分额度，使细分后的最大边长最小
            while remaining > 0:
                max_val = -1.0
                max_idx = -1
                for idx in range(K_seg):
                    val = lengths[idx] / k[idx]
                    if val > max_val:
                        max_val = val
                        max_idx = idx
                if max_idx != -1:
                    k[max_idx] += 1
                else:
                    k[0] += 1
                remaining -= 1
                
            new_points = []
            orig_idx_map = {}
            
            # 组合各段：元组格式为 (起点, 终点, 细分段数, 起点在原数组中的索引)
            segments = []
            for i in range(M - 1):
                segments.append((points[i], points[i+1], k[i], i))
            if is_closed:
                segments.append((points[-1], points[0], k[-1], M - 1))
                
            current_index = 0
            for p_start, p_end, k_count, orig_start_idx in segments:
                orig_idx_map[current_index] = orig_start_idx
                for j in range(k_count):
                    t = j / k_count
                    pos = p_start * (1.0 - t) + p_end * t
                    new_points.append(pos)
                    current_index += 1
                    
            if not is_closed:
                # 开路时需要把最后一个点追加进去
                orig_idx_map[current_index] = M - 1
                new_points.append(points[-1].copy())
                
            return new_points, orig_idx_map
            
        else:
            # 2. 如果点数减少，回退到全局重采样
            dists = [0.0]
            for i in range(len(points) - 1):
                dists.append(dists[-1] + (points[i+1] - points[i]).length)
                
            total_len = dists[-1]
            if is_closed:
                total_len += (points[0] - points[-1]).length
                dists.append(total_len)
                
            if total_len < 1e-5:
                res = [p.copy() for p in points[:num_samples]]
                return res, {i: min(i, len(points)-1) for i in range(len(res))}
                
            if is_closed:
                target_dists = [i * total_len / num_samples for i in range(num_samples)]
            else:
                target_dists = [i * total_len / (num_samples - 1) for i in range(num_samples)]
                
            sampled_pts = []
            pts_list = points + [points[0]] if is_closed else points
            
            for td in target_dists:
                pos = pts_list[-1].copy()
                for i in range(len(dists) - 1):
                    if dists[i] <= td <= dists[i+1]:
                        seg_len = dists[i+1] - dists[i]
                        t = 0.0 if seg_len < 1e-5 else (td - dists[i]) / seg_len
                        pos = pts_list[i] * (1.0 - t) + pts_list[i+1] * t
                        break
                sampled_pts.append(pos)
                
            # 回退映射：只映射头尾
            orig_idx_map = {}
            if num_samples > 0:
                orig_idx_map[0] = 0
                if not is_closed and num_samples > 1:
                    orig_idx_map[num_samples - 1] = len(points) - 1
                    
            return sampled_pts, orig_idx_map

    def resample_path_uniform(self, points, is_closed, num_samples):
        """对 3D 顶点列表使用线性插值进行完全等距离重采样"""
        if len(points) < 2 or num_samples < 2:
            return [p.copy() for p in points]
            
        dists = [0.0]
        for i in range(len(points) - 1):
            dists.append(dists[-1] + (points[i+1] - points[i]).length)
            
        total_len = dists[-1]
        if is_closed:
            total_len += (points[0] - points[-1]).length
            dists.append(total_len)
            
        if total_len < 1e-5:
            return [p.copy() for p in points[:num_samples]]
            
        if is_closed:
            target_dists = [i * total_len / num_samples for i in range(num_samples)]
        else:
            target_dists = [i * total_len / (num_samples - 1) for i in range(num_samples)]
            
        sampled_pts = []
        pts_list = points + [points[0]] if is_closed else points
        
        for td in target_dists:
            pos = pts_list[-1].copy()
            for i in range(len(dists) - 1):
                if dists[i] <= td <= dists[i+1]:
                    seg_len = dists[i+1] - dists[i]
                    t = 0.0 if seg_len < 1e-5 else (td - dists[i]) / seg_len
                    pos = pts_list[i] * (1.0 - t) + pts_list[i+1] * t
                    break
            sampled_pts.append(pos)
            
        return sampled_pts

    def average_path_segments(self, points, corner_indices, is_closed):
        """对 3D 顶点列表的每一段进行等距离重采样平均，同时固定特征拐角点"""
        if len(points) < 3 or not corner_indices:
            return [p.copy() for p in points]
            
        new_points = [p.copy() for p in points]
        
        segments = []
        num_corners = len(corner_indices)
        for i in range(num_corners - 1):
            segments.append((corner_indices[i], corner_indices[i+1]))
        if is_closed:
            segments.append((corner_indices[-1], corner_indices[0]))
            
        for start_idx, end_idx in segments:
            if start_idx < end_idx:
                sub_indices = list(range(start_idx, end_idx + 1))
            else:
                sub_indices = list(range(start_idx, len(points))) + list(range(0, end_idx + 1))
                
            if len(sub_indices) < 3:
                continue
                
            sub_points = [points[idx] for idx in sub_indices]
            resampled_sub = self.resample_path_uniform(sub_points, is_closed=False, num_samples=len(sub_indices))
            
            for idx_in_sub, idx_in_points in enumerate(sub_indices):
                new_points[idx_in_points] = resampled_sub[idx_in_sub]
                
        return new_points

    def detect_corners(self, points, is_closed):
        """通过分析顶点拐角角度，动态识别硬表面边缘折角特征点"""
        if len(points) < 3:
            return list(range(len(points)))
            
        import math
        M = len(points)
        corners = []
        
        if not is_closed:
            corners.append(0)
            
        start_check = 0 if is_closed else 1
        end_check = M if is_closed else M - 1
        
        for i in range(start_check, end_check):
            p_prev = points[(i - 1) % M]
            p_curr = points[i]
            p_next = points[(i + 1) % M]
            
            v1 = p_curr - p_prev
            v2 = p_next - p_curr
            
            if v1.length_squared > 1e-8 and v2.length_squared > 1e-8:
                dot = v1.normalized().dot(v2.normalized())
                dot = max(-1.0, min(1.0, dot))
                angle = math.acos(dot)
                # 角度阀值：30度 = 0.5236弧度。如果转弯角大于30度（即夹角小于150度），判定为折角角点
                if angle > 0.5236:
                    corners.append(i)
                    
        if not is_closed:
            corners.append(M - 1)
            
        if is_closed and not corners:
            corners.append(0)
            
        return sorted(list(set(corners)))

    def trace_selected_path(self, bm):
        # Find all selected vertices
        selected_verts = [v for v in bm.verts if v.select]
        if len(selected_verts) < 2:
            return [], False
            
        # Get all selected edges between selected vertices
        selected_edges = [e for e in bm.edges if e.select and e.verts[0].select and e.verts[1].select]
        
        # Build adjacency list
        from collections import defaultdict
        adj = defaultdict(list)
        for e in selected_edges:
            v1, v2 = e.verts
            adj[v1].append(v2)
            adj[v2].append(v1)
            
        # Find start vertices (degree 1 in selected adjacency)
        start_verts = [v for v in selected_verts if len(adj[v]) == 1]
        
        if len(start_verts) == 2:
            # Open path!
            path = []
            curr = start_verts[0]
            visited = {curr}
            path.append(curr)
            while True:
                next_v = None
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        next_v = neighbor
                        break
                if next_v is None:
                    break
                path.append(next_v)
                visited.add(next_v)
                curr = next_v
            return path, False
            
        elif len(start_verts) == 0:
            # Closed loop!
            degree_2_verts = [v for v in selected_verts if len(adj[v]) == 2]
            if not degree_2_verts:
                return [], False
                
            path = []
            curr = degree_2_verts[0]
            visited = {curr}
            path.append(curr)
            
            neighbors = adj[curr]
            if len(neighbors) < 2:
                return [], False
            curr = neighbors[0]
            path.append(curr)
            visited.add(curr)
            
            while True:
                next_v = None
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        next_v = neighbor
                        break
                if next_v is None:
                    if path[0] in adj[curr]:
                        return path, True
                    break
                path.append(next_v)
                visited.add(next_v)
                curr = next_v
                
            return path, False
            
        return [], False

    def average_current_stroke(self, context):
        if not getattr(context.scene, "tp_auto_average_stroke", True):
            return
            
        me = self.topo_obj.data
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        
        path, is_closed = self.trace_selected_path(bm)
        if len(path) < 3:
            return
            
        # Get coordinates in world space (in edit mode)
        matrix_world = self.topo_obj.matrix_world
        points = [matrix_world @ v.co for v in path]
        path_vert_indices = [v.index for v in path]
        
        # Resample uniformly
        num_samples = len(points)
        new_world_cos = self.resample_path_uniform(points, is_closed, num_samples)
        
        # Snapping setup
        ref_matrix_world = self.ref_obj.matrix_world
        ref_matrix_world_inv = ref_matrix_world.inverted()
        topo_matrix_world_inv = self.topo_obj.matrix_world.inverted()
        
        # Workaround for background/headless mode:
        # Toggle out of EDIT mode temporarily to run closest_point_on_mesh, then toggle back
        mode_switched = False
        active_obj = bpy.context.active_object
        if active_obj and active_obj.mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')
            mode_switched = True
            
        ref_obj_eval = self.ref_obj
        try:
            dg = bpy.context.evaluated_depsgraph_get()
            ref_obj_eval = self.ref_obj.evaluated_get(dg)
        except Exception:
            pass
            
        projected_local_cos = []
        for i, pos_world in enumerate(new_world_cos):
            # Snap to high-poly reference surface (now safe in OBJECT mode)
            local_pos_ref = ref_matrix_world_inv @ pos_world
            succ, loc_local, norm_local, idx_mesh = ref_obj_eval.closest_point_on_mesh(local_pos_ref)
            if succ:
                location = ref_matrix_world @ loc_local
                normal = (ref_matrix_world.to_3x3() @ norm_local).normalized()
                pos_world = location + normal * 0.001
                
            local_pos = topo_matrix_world_inv @ pos_world
            projected_local_cos.append(local_pos)
            
        # Switch back to EDIT mode
        if mode_switched:
            bpy.ops.object.mode_set(mode='EDIT')
            
        # Re-get BMesh in EDIT mode and write back the coordinates
        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        
        for i, idx in enumerate(path_vert_indices):
            if idx < len(bm.verts):
                v = bm.verts[idx]
                local_pos = projected_local_cos[i]
                v.co = local_pos
                self.vert_flat_co[v.index] = local_pos.copy()
                
        bmesh.update_edit_mesh(me)
        context.area.tag_redraw()
        print(f"DEBUG: Averaged stroke of {len(path_vert_indices)} vertices (closed={is_closed})")





# ==========================================
# 侧边栏 UI 面板 (N Panel)
# ==========================================

class VIEW3D_PT_tp_topology_panel(bpy.types.Panel):
    bl_label = "TP拓扑"
    bl_idname = "VIEW3D_PT_tp_topology_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TP拓扑'
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        wm = context.window_manager
        active_obj = context.active_object
        
        # 1. 拓扑控制主按钮
        col = layout.column(align=True)
        col.scale_y = 1.4
        
        is_mesh = active_obj and active_obj.type == 'MESH'
        
        if wm.tp_topology_running:
            col.operator("view3d.tp_topology_modal", text="当前为拓扑状态", icon='CANCEL').stop = True
            
            # 当拓扑状态运行中，显示“栅格化”建面按钮
            col.separator()
            row_grid = col.row()
            row_grid.scale_y = 1.2
            row_grid.operator("view3d.tp_grid_fill", text="栅格化", icon='GRID')
        else:
            row = col.row()
            if not is_mesh:
                row.enabled = False
            row.operator("view3d.tp_topology_modal", text="当前未进入拓扑状态", icon='PLAY')
            
        # 2. 绘制参数设置
        layout.separator()
        box = layout.box()
        box.label(text="绘制设置", icon='TOOL_SETTINGS')
        
        row_dist = box.row()
        row_dist.prop(scene, "tp_fixed_spacing_dist", text="间距(米)")
        
        # 绘制 "显示在最前" 属性复选框，直接绑定场景级属性以实现更好的状态同步
        row_front = box.row()
        row_front.prop(scene, "tp_show_in_front", text="显示在最前")

        row_avg = box.row()
        row_avg.prop(scene, "tp_auto_average_stroke", text="自动平均点距")
        



# ==========================================
# 插件注册与卸载
# ==========================================

def update_show_in_front(self, context):
    """当用户更改面板中的“显示在最前”时，同步更新拓扑对象的属性"""
    topo_obj = bpy.data.objects.get("TP_Topology")
    if topo_obj:
        topo_obj.show_in_front = self.tp_show_in_front

class VIEW3D_OT_tp_grid_fill(bpy.types.Operator):
    bl_idname = "view3d.tp_grid_fill"
    bl_label = "栅格化"
    bl_description = "将所选的拓扑点进行建面 (智能选择网格填充或常规建面)"
    bl_options = {'UNDO'}
    
    def execute(self, context):
        active_obj = context.active_object
        if not active_obj or active_obj.name != "TP_Topology" or active_obj.mode != 'EDIT':
            self.report({'WARNING'}, "未处于拓扑编辑状态！")
            return {'CANCELLED'}
            
        # 获取 bmesh 并确保查找表是最新的
        bm = bmesh.from_edit_mesh(active_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        
        # 收集选中的点
        selected_verts = {v for v in bm.verts if v.select}
        if not selected_verts:
            self.report({'WARNING'}, "请先选择需要建面的拓扑点！")
            return {'CANCELLED'}
            
        # 1. 自动将选中的顶点状态同步选择到边
        bm.select_flush(True)
        
        # 2. 通过广度优先搜索 (BFS) 寻找所有选中的连通分支 (Components)
        visited = set()
        components = []
        
        for v in selected_verts:
            if v not in visited:
                comp = []
                queue = [v]
                visited.add(v)
                while queue:
                    curr = queue.pop(0)
                    comp.append(curr)
                    for edge in curr.link_edges:
                        other = edge.other_vert(curr)
                        if other in selected_verts and other not in visited:
                            visited.add(other)
                            queue.append(other)
                components.append(comp)
                
        # 将连通分支保存为顶点索引 (int)，防止后续由于 Blender 算子重建网格导致 BMVert 内存指针失效抛出 ReferenceError
        components = [[v.index for v in comp] for comp in components]
                
        # 寻找场景中的参考高模对象
        ref_obj = None
        for obj in context.scene.objects:
            if obj.type == 'MESH' and obj.name != "TP_Topology":
                if not obj.hide_get():
                    ref_obj = obj
                    break
        if not ref_obj:
            for obj in context.scene.objects:
                if obj.type == 'MESH' and obj.name != "TP_Topology":
                    ref_obj = obj
                    break
                    
        # 保存原始选择模式
        tool_settings = context.tool_settings
        old_select_mode = tool_settings.mesh_select_mode[:]
        
        # 定义局部辅助函数，将新生成的顶点投射回参考高模面
        def snap_verts_to_surface(verts_list):
            if not ref_obj or not verts_list:
                return
            ref_world = ref_obj.matrix_world
            ref_world_inv = ref_world.inverted()
            topo_world = active_obj.matrix_world
            topo_world_inv = topo_world.inverted()
            for v in verts_list:
                world_pos = topo_world @ v.co
                local_pos = ref_world_inv @ world_pos
                success, hit_loc, _, _ = ref_obj.closest_point_on_mesh(local_pos)
                if success:
                    world_hit = ref_world @ hit_loc
                    v.co = topo_world_inv @ world_hit

        success_count = 0
        all_processed_verts = []
        
        # 3. 逐个对每个连通分支进行“栅格化”或“普通建面”
        for comp_indices in components:
            # 重新获取 fresh 的 bmesh 实例，防止前一个分支的建面算子导致当前 BMesh 数据库失效
            bm = bmesh.from_edit_mesh(active_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            # 清空当前选择以孤立处理此分支
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
            for f in bm.faces:
                f.select = False
                
            # 根据索引选中当前连通分支的全部顶点
            for idx in comp_indices:
                if idx < len(bm.verts):
                    bm.verts[idx].select = True
            bm.select_flush(True)
            
            # 智能细分单数顶点（多于4点）：细分最长边，并贴合高模以支持 fill_grid
            comp_selected_verts = [v for v in bm.verts if v.select]
            if len(comp_selected_verts) > 4 and len(comp_selected_verts) % 2 != 0:
                comp_selected_edges = [e for e in bm.edges if e.select]
                if comp_selected_edges:
                    longest_edge = max(comp_selected_edges, key=lambda e: (e.verts[0].co - e.verts[1].co).length)
                    res = bmesh.ops.subdivide_edges(bm, edges=[longest_edge], cuts=1)
                    
                    new_verts = [item for item in res['geom_split'] if isinstance(item, bmesh.types.BMVert)]
                    for v in new_verts:
                        v.select = True
                        
                    # 投射细分点到表面
                    snap_verts_to_surface(new_verts)
                    bm.select_flush(True)
                    
            bmesh.update_edit_mesh(active_obj.data)
            
            # 刷新网格后，必须再次重新获取 fresh 的 bmesh 实例，防止 bpy.ops 调用失效
            bm = bmesh.from_edit_mesh(active_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            # 再次获取细分后或当前的顶点
            comp_selected_verts = [v for v in bm.verts if v.select]
            num_faces_before = len(bm.faces)
            grid_fill_success = False
            
            # 尝试 Grid Fill (网格填充)
            if len(comp_selected_verts) > 4:
                try:
                    tool_settings.mesh_select_mode = (False, True, False)
                    # 记录已有的顶点索引，用于投射新生成点
                    existing_vert_indices = {v.index for v in bm.verts}
                    bpy.ops.mesh.fill_grid()
                    tool_settings.mesh_select_mode = old_select_mode
                    
                    # 重新获取 fresh 的 BMesh，检查是否成功生成面
                    bm_fresh = bmesh.from_edit_mesh(active_obj.data)
                    bm_fresh.faces.ensure_lookup_table()
                    bm_fresh.verts.ensure_lookup_table()
                    if len(bm_fresh.faces) > num_faces_before:
                        grid_fill_success = True
                        success_count += 1
                        
                        # 投射新生成的顶点到高模表面
                        new_verts = [v for v in bm_fresh.verts if v.index not in existing_vert_indices]
                        if new_verts:
                            snap_verts_to_surface(new_verts)
                            bmesh.update_edit_mesh(active_obj.data)
                            # 重新获取以保持选择状态一致
                            bm_fresh = bmesh.from_edit_mesh(active_obj.data)
                            bm_fresh.verts.ensure_lookup_table()
                            
                        # 收集所有的点（包含新生成的内点）
                        all_processed_verts.extend([v.index for v in bm_fresh.verts if v.select])
                except Exception:
                    tool_settings.mesh_select_mode = old_select_mode
            
            if grid_fill_success:
                continue
                
            # 普通建面逻辑 (类似于 F 键)
            # 重新获取 fresh 的 BMesh，因为之前的操作可能导致 bm 句柄失效
            bm = bmesh.from_edit_mesh(active_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            # 获取当前分支选中的点 and 边，并记录已有的面索引以识别新生成的面
            current_selected_verts = [v for v in bm.verts if v.select]
            current_selected_edges = [e for e in bm.edges if e.select]
            existing_face_indices = {f.index for f in bm.faces}
            
            built_faces_success = False
            try:
                geom = current_selected_verts + current_selected_edges
                bmesh.ops.contextual_create(bm, geom=geom)
                bmesh.update_edit_mesh(active_obj.data)
                built_faces_success = True
            except Exception:
                try:
                    bpy.ops.mesh.edge_face_add()
                    built_faces_success = True
                except Exception:
                    pass
            
            if built_faces_success:
                success_count += 1
                
                # 重新获取 BMesh 以查找新生成的面
                bm = bmesh.from_edit_mesh(active_obj.data)
                bm.faces.ensure_lookup_table()
                new_faces = [f for f in bm.faces if f.index not in existing_face_indices]
                new_face_indices = [f.index for f in new_faces]
                
                # 对每一个新生成的大面进行边界局部栅格化填充
                for f_idx in new_face_indices:
                    # 重新从 edit mesh 获取以保持同步
                    bm = bmesh.from_edit_mesh(active_obj.data)
                    bm.faces.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()
                    
                    face = None
                    for f in bm.faces:
                        if f.index == f_idx:
                            face = f
                            break
                    if not face:
                        continue
                        
                    # 如果该面只有3或4个顶点，已经是三角/四边形，无需进一步栅格化
                    if len(face.verts) <= 4:
                        all_processed_verts.extend([v.index for v in face.verts])
                        continue
                        
                    # 记录该面原本的边界边索引
                    boundary_edge_indices = {e.index for e in face.edges}
                    
                    # 清空选择状态
                    for v in bm.verts:
                        v.select = False
                    for e in bm.edges:
                        e.select = False
                    for f_other in bm.faces:
                        f_other.select = False
                        
                    # 从网格中只删除这个大面本身 (保留它的边界边和顶点)
                    bmesh.ops.delete(bm, geom=[face], context='FACES_ONLY')
                    bmesh.update_edit_mesh(active_obj.data)
                    
                    # 重新获取 BMesh 并选中边界边
                    bm = bmesh.from_edit_mesh(active_obj.data)
                    bm.edges.ensure_lookup_table()
                    bm.verts.ensure_lookup_table()
                    for e in bm.edges:
                        if e.index in boundary_edge_indices:
                            e.select = True
                            
                    # 记录投射前的顶点索引
                    existing_vert_indices = {v.index for v in bm.verts}
                    
                    # 切换到边选择模式进行 fill_grid
                    tool_settings.mesh_select_mode = (False, True, False)
                    bmesh.update_edit_mesh(active_obj.data)
                    
                    try:
                        bpy.ops.mesh.fill_grid()
                        
                        # 重新获取 BMesh 并将新生成的点投射回高模表面
                        bm_temp = bmesh.from_edit_mesh(active_obj.data)
                        bm_temp.verts.ensure_lookup_table()
                        
                        new_verts = [v for v in bm_temp.verts if v.index not in existing_vert_indices]
                        if new_verts:
                            snap_verts_to_surface(new_verts)
                            bmesh.update_edit_mesh(active_obj.data)
                            bm_temp = bmesh.from_edit_mesh(active_obj.data)
                            bm_temp.verts.ensure_lookup_table()
                            
                        # 收集所有的点（包含新生成的内点）
                        all_processed_verts.extend([v.index for v in bm_temp.verts if v.select])
                    except Exception:
                        # 如果单个面的 grid fill 失败，尝试用原始大面兜底重建它
                        bm_temp = bmesh.from_edit_mesh(active_obj.data)
                        bm_temp.verts.ensure_lookup_table()
                        bm_temp.edges.ensure_lookup_table()
                        sel_verts = [v for v in bm_temp.verts if v.select]
                        sel_edges = [e for e in bm_temp.edges if e.select]
                        try:
                            bmesh.ops.contextual_create(bm_temp, geom=sel_verts + sel_edges)
                            bmesh.update_edit_mesh(active_obj.data)
                            all_processed_verts.extend([v.index for v in sel_verts])
                        except Exception:
                            pass
                            
                # 恢复选择模式
                tool_settings.mesh_select_mode = old_select_mode
                    
        # 4. 恢复选择状态：重新获取全新的 BMesh，清空选择，选中所有参与建面和新生成的顶点
        bm = bmesh.from_edit_mesh(active_obj.data)
        bm.verts.ensure_lookup_table()
        
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
            
        bm.verts.ensure_lookup_table()
        for idx in all_processed_verts:
            if idx < len(bm.verts):
                bm.verts[idx].select = True
        bm.select_flush(True)
        bmesh.update_edit_mesh(active_obj.data)
        
        # 5. 刷新视口并汇报
        if context.area:
            context.area.tag_redraw()
            
        if success_count > 0:
            self.report({'INFO'}, f"成功对 {success_count} 个分支进行了建面/网格填充！")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "建面失败，请检查选点拓扑结构。")
            return {'CANCELLED'}

classes = (
    VIEW3D_PT_tp_topology_panel,
    VIEW3D_OT_tp_topology_modal,
    VIEW3D_OT_tp_grid_fill,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.tp_topology_running = bpy.props.BoolProperty(
        name="TP拓扑运行中",
        default=False
    )

    bpy.types.Scene.tp_fixed_spacing_dist = bpy.props.FloatProperty(
        name="空间距离",
        description="绘制连线顶点时的 3D 空间实际距离(单位:米)",
        default=0.1,
        min=0.001,
        max=5.0,
        precision=3,
        step=1.0
    )

    bpy.types.Scene.tp_show_in_front = bpy.props.BoolProperty(
        name="显示在最前",
        description="是否让拓扑网格显示在所有模型最前方",
        default=False,
        update=update_show_in_front
    )

    bpy.types.Scene.tp_auto_average_stroke = bpy.props.BoolProperty(
        name="自动平均点距",
        description="绘制完成后自动等距平均所有的点",
        default=True
    )

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.tp_topology_running

    del bpy.types.Scene.tp_fixed_spacing_dist
    del bpy.types.Scene.tp_show_in_front
    del bpy.types.Scene.tp_auto_average_stroke

if __name__ == "__main__":
    register()
