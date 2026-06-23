import bpy
import bmesh
import mathutils
import math

class OBJECT_OT_tp_topology_grid_fill(bpy.types.Operator):
    bl_idname = "object.tp_topology_grid_fill"
    bl_label = "TP拓扑栅格填充"
    bl_description = "将选中的圈进行高质量的栅格化填充，并优化为均匀的正方形面"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'
        
    def execute(self, context):
        topo_obj = context.active_object
        if not topo_obj or topo_obj.type != 'MESH':
            self.report({'WARNING'}, "没有活动的网格对象")
            return {'CANCELLED'}
            
        ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        
        # 辅助函数：计算面法线和面积，并支持虚拟移动单个顶点后预测其计算结果，用于防止折叠
        def get_face_normal_and_area(face, move_vert=None, new_co=None):
            coords = []
            for loop in face.loops:
                vert = loop.vert
                if vert == move_vert:
                    coords.append(new_co)
                else:
                    coords.append(vert.co)
            
            # Newell方法计算法线与面积
            n = mathutils.Vector((0.0, 0.0, 0.0))
            for i in range(len(coords)):
                curr = coords[i]
                nxt = coords[(i + 1) % len(coords)]
                n.x += (curr.y - nxt.y) * (curr.z + nxt.z)
                n.y += (curr.z - nxt.z) * (curr.x + nxt.x)
                n.z += (curr.x - nxt.x) * (curr.y + nxt.y)
            length = n.length
            if length > 1e-7:
                return n.normalized(), length
            else:
                return mathutils.Vector((0.0, 0.0, 0.0)), 0.0
        
        if topo_obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
            
        bm = bmesh.from_edit_mesh(topo_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        
        # 1. 过滤选中的边：只保留边界边 (链接面数小于等于1的面)
        selected_edges = set(e for e in bm.edges if e.select and len(e.link_faces) <= 1)
        
        # 2. 合并重合顶点：防止由于顶点重合导致无法连成回路
        selected_verts = list(set(v for e in selected_edges for v in e.verts))
        if selected_verts:
            bmesh.ops.remove_doubles(bm, verts=selected_verts, dist=0.0001)
            bmesh.update_edit_mesh(topo_obj.data)
            # 重新获取同步后的 edit mesh
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            selected_edges = set(e for e in bm.edges if e.select and len(e.link_faces) <= 1)
            
        # 3. 剪枝算法：移除悬挂的单条边 (degree == 1) 等非闭合回路的分支
        while True:
            vert_degrees = {}
            for e in selected_edges:
                vert_degrees[e.verts[0]] = vert_degrees.get(e.verts[0], 0) + 1
                vert_degrees[e.verts[1]] = vert_degrees.get(e.verts[1], 0) + 1
                
            deg1_verts = {v for v, deg in vert_degrees.items() if deg == 1}
            if not deg1_verts:
                break
                
            to_remove = set()
            for e in selected_edges:
                if e.verts[0] in deg1_verts or e.verts[1] in deg1_verts:
                    to_remove.add(e)
            
            selected_edges -= to_remove
            
        loops = []
        visited_edges = set()
        for edge in selected_edges:
            if edge in visited_edges:
                continue
                
            loop_edges = [edge]
            visited_edges.add(edge)
            
            curr_v = edge.verts[0]
            prev_e = edge
            while True:
                next_e = None
                for e in curr_v.link_edges:
                    if e in selected_edges and e not in visited_edges:
                        next_e = e
                        break
                if not next_e:
                    break
                visited_edges.add(next_e)
                loop_edges.append(next_e)
                curr_v = next_e.other_vert(curr_v)
                prev_e = next_e
                
            curr_v = edge.verts[1]
            prev_e = edge
            while True:
                next_e = None
                for e in curr_v.link_edges:
                    if e in selected_edges and e not in visited_edges:
                        next_e = e
                        break
                if not next_e:
                    break
                visited_edges.add(next_e)
                loop_edges.insert(0, next_e)
                curr_v = next_e.other_vert(curr_v)
                prev_e = next_e
                
            vert_counts = {}
            for e in loop_edges:
                vert_counts[e.verts[0]] = vert_counts.get(e.verts[0], 0) + 1
                vert_counts[e.verts[1]] = vert_counts.get(e.verts[1], 0) + 1
                
            if len(loop_edges) >= 4 and all(count == 2 for count in vert_counts.values()):
                loops.append(([e.index for e in loop_edges], [v.index for v in vert_counts.keys()]))
                
        if not loops:
            self.report({'WARNING'}, "未选中任何有效的闭合圈（选中的边必须构成闭合回路）")
            return {'CANCELLED'}
            
        self.report({'INFO'}, f"找到 {len(loops)} 个待填充 of 圈, 开始高质量填充...")
        
        succeeded_count = 0
        failed_count = 0
        
        for loop_idx, (loop_edge_indices, loop_vert_indices) in enumerate(loops):
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            # 重新解析当前 BMesh 中的闭合圈对应的边 and 点
            edge_idx_set = set(loop_edge_indices)
            bm_loop_edges = [e for e in bm.edges if e.index in edge_idx_set]
            bm_loop_verts = set()
            for e in bm_loop_edges:
                bm_loop_verts.update(e.verts)
                
            vert_idx_set = set(v.index for v in bm_loop_verts)
            
            # 4. 奇数点处理：如果圈上的顶点数为奇数，栅格填充必定失败，在此自动细分最长的一条边，使其变成偶数点
            if len(bm_loop_verts) % 2 != 0:
                longest_edge = max(bm_loop_edges, key=lambda e: (e.verts[0].co - e.verts[1].co).length)
                new_edge, new_vert = bmesh.utils.edge_split(longest_edge, longest_edge.verts[0], 0.5)
                edge_idx_set.add(new_edge.index)
                vert_idx_set.add(new_vert.index)
            
            # 清除其他选择，仅选中当前圈的边 and 点
            for v in bm.verts:
                v.select = False
            for e in bm.edges:
                e.select = False
            bm.select_history.clear()
            
            for e in bm.edges:
                if e.index in edge_idx_set:
                    e.select = True
            for v in bm.verts:
                if v.index in vert_idx_set:
                    v.select = True
                    
            # 记录此时的顶点集合，用于之后识别哪些是新生成的网格顶点（进行松弛投影优化）
            # 使用坐标哈希以防 fill_grid 重新排序索引导致识别边界错误
            def co_key(co):
                return (round(co.x, 6), round(co.y, 6), round(co.z, 6))
            existing_cos = {co_key(v.co) for v in bm.verts}
            
            bmesh.update_edit_mesh(topo_obj.data)
            
            num_verts = len(vert_idx_set)
            optimal_span = num_verts // 4
            
            success = False
            
            # 5. 栅格填充搜索算法：首先尝试正常的插值模式 (use_interp_simple=False)
            # 如果失败，再尝试简单融合模式 (use_interp_simple=True)，极大增强对扭曲或高曲率孔洞的适应度
            faces_before = len(bm.faces)
            for interp_simple in [False, True]:
                # 尝试最优跨度
                if optimal_span >= 1:
                    for offset in range(num_verts):
                        try:
                            res = bpy.ops.mesh.fill_grid(span=optimal_span, offset=offset, use_interp_simple=interp_simple)
                            temp_bm = bmesh.from_edit_mesh(topo_obj.data)
                            if len(temp_bm.faces) > faces_before:
                                success = True
                                break
                        except Exception:
                            pass
                    if success:
                        break
                        
                # 尝试其他可能的跨度 (从 1 到 num_verts // 2)
                candidate_spans = []
                for s in range(1, num_verts // 2):
                    if s != optimal_span:
                        candidate_spans.append(s)
                candidate_spans.sort(key=lambda s: abs(s - optimal_span))
                
                for span in candidate_spans:
                    for offset in range(num_verts):
                        try:
                            res = bpy.ops.mesh.fill_grid(span=span, offset=offset, use_interp_simple=interp_simple)
                            temp_bm = bmesh.from_edit_mesh(topo_obj.data)
                            if len(temp_bm.faces) > faces_before:
                                success = True
                                break
                        except Exception:
                            pass
                    if success:
                        break
                if success:
                    break
                    
                # 尝试默认参数作为最后的妥协
                try:
                    res = bpy.ops.mesh.fill_grid(use_interp_simple=interp_simple)
                    temp_bm = bmesh.from_edit_mesh(topo_obj.data)
                    if len(temp_bm.faces) > faces_before:
                        success = True
                        break
                except Exception:
                    pass
                if success:
                    break
                    
            if not success:
                # 最后的兜底方案：使用普通填充 (Beauty Fill) 并转换为四边形，确保百分百能填充成功
                try:
                    res = bpy.ops.mesh.fill(use_beauty=True)
                    temp_bm = bmesh.from_edit_mesh(topo_obj.data)
                    if len(temp_bm.faces) > faces_before:
                        bpy.ops.mesh.tris_convert_to_quads()
                        success = True
                except Exception as e:
                    print("Beauty fill fallback error:", e)
                    
            if not success:
                failed_count += 1
                self.report({'WARNING'}, f"第 {loop_idx + 1} 个圈栅格化填充失败：无法找到合适的跨度(span)或进行普通填充")
                continue
                
            succeeded_count += 1
                
            bm = bmesh.from_edit_mesh(topo_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            
            interior_verts = [v for v in bm.verts if co_key(v.co) not in existing_cos]
            
            if interior_verts:
                if ref_obj:
                    matrix_world = ref_obj.matrix_world
                    matrix_inverse = matrix_world.inverted()
                    topo_inverse = topo_obj.matrix_world.inverted()
                    topo_world = topo_obj.matrix_world
                    
                # 1. 计算每个顶点的拓扑深度（BFS，从边界层层向内）
                depths = {}
                for v in bm.verts:
                    if co_key(v.co) in existing_cos:
                        depths[v] = 0
                
                queue = [v for v in bm.verts if co_key(v.co) in existing_cos]
                head = 0
                while head < len(queue):
                    curr = queue[head]
                    head += 1
                    curr_depth = depths[curr]
                    for edge in curr.link_edges:
                        neighbor = edge.other_vert(curr)
                        if neighbor not in depths:
                            depths[neighbor] = curr_depth + 1
                            queue.append(neighbor)
                            
                max_depth = max(depths.values()) if depths else 1
                if max_depth == 0:
                    max_depth = 1
                    
                decay = context.scene.tp_grid_decay
                
                # 2. 迭代松弛并投影
                for iteration in range(50):
                    new_cos = {}
                    for v in interior_verts:
                        total_w = 0.0
                        weighted_sum = mathutils.Vector()
                        
                        v_depth = depths.get(v, 0)
                        
                        for edge in v.link_edges:
                            neighbor = edge.other_vert(v)
                            n_depth = depths.get(neighbor, 0)
                            
                            # 边的深度取两端点深度之平均值
                            edge_depth = (v_depth + n_depth) / 2.0
                            norm_depth = edge_depth / max_depth
                            
                            # 边长与权重成反比：采用指数权重函数，使渐变效果明显
                            # 当 norm_depth 趋于 0 时，权重接近 1.0 (边界)
                            # 当 norm_depth 趋于 1 时，权重接近 exp(decay) (中心)
                            w = math.exp(decay * norm_depth)
                            
                            weighted_sum += neighbor.co * w
                            total_w += w
                            
                        if total_w > 0.0:
                            avg_co = weighted_sum / total_w
                            
                            # 计算切线平滑向量以应对复杂的高曲率表面，防止网格收缩和自交变形
                            smoothing_vec = avg_co - v.co
                            normal = v.normal.copy()
                            if ref_obj:
                                try:
                                    world_co = topo_world @ v.co
                                    local_target = matrix_inverse @ world_co
                                    proj_success, location, normal_local, index = ref_obj.closest_point_on_mesh(local_target)
                                    if proj_success:
                                        trans_matrix = (topo_inverse @ matrix_world).to_3x3()
                                        normal = (trans_matrix @ normal_local).normalized()
                                except Exception:
                                    pass
                                    
                            tangent_vec = smoothing_vec - smoothing_vec.dot(normal) * normal
                            
                            # 试探步长以绝对防止重叠面和折叠面 (Backtracking Line Search)
                            step_scale = 1.0
                            is_safe = False
                            co_proposed = v.co
                            
                            for _ in range(4):
                                co_temp = v.co + tangent_vec * (0.6 * step_scale)
                                
                                step_safe = True
                                for f in v.link_faces:
                                    n_before, area_before = get_face_normal_and_area(f)
                                    n_after, area_after = get_face_normal_and_area(f, move_vert=v, new_co=co_temp)
                                    
                                    if area_before > 1e-7:
                                        # 如果面积过于收缩，或者法线发生反转/严重倾斜，则判定为折叠不安全
                                        if area_after < 0.1 * area_before or n_before.dot(n_after) < 0.2:
                                            step_safe = False
                                            break
                                            
                                if step_safe:
                                    co_proposed = co_temp
                                    is_safe = True
                                    break
                                else:
                                    step_scale *= 0.5
                                    
                            if is_safe:
                                new_cos[v] = co_proposed
                            else:
                                new_cos[v] = v.co
                            
                    for v, co in new_cos.items():
                        if ref_obj:
                            try:
                                world_co = topo_world @ co
                                local_target = matrix_inverse @ world_co
                                proj_success, location, normal_local, index = ref_obj.closest_point_on_mesh(local_target)
                                if proj_success:
                                    local_pt = location + normal_local * 0.003
                                    co = topo_inverse @ (matrix_world @ local_pt)
                            except Exception:
                                pass
                        v.co = co
                        
                bmesh.update_edit_mesh(topo_obj.data)
                
        # 清空选择状态，防止下次误操作或误识别已经填充好的边界圈
        bm = bmesh.from_edit_mesh(topo_obj.data)
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
        bmesh.update_edit_mesh(topo_obj.data)
        
        # 最终汇总报告
        if succeeded_count == 0:
            self.report({'WARNING'}, "所有选中的圈均填充失败。")
            return {'CANCELLED'}
        elif failed_count > 0:
            self.report({'INFO'}, f"部分填充完成：成功 {succeeded_count} 个，失败 {failed_count} 个。")
            return {'FINISHED'}
        else:
            self.report({'INFO'}, f"栅格填充及正方形优化全部完成（成功填充 {succeeded_count} 个圈）！")
            return {'FINISHED'}
