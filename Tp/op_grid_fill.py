import bpy
import bmesh
import mathutils
from mathutils import Vector
import math

def check_is_grid_filled(bm, loop_verts, cycle_edges):
    """
    检查一个圈（顶点和边）是否已经完成了栅格填充。
    如果圈中超过 80% 的边连接了已栅格化（f[grid_layer] > 0）的面，则判定为已填充。
    """
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if not grid_layer:
        return False
        
    cycle_edges_set = set(cycle_edges)
    if not cycle_edges_set:
        return False
        
    grid_edge_count = 0
    for e in cycle_edges_set:
        # 检查这条边是否连接了任何已栅格化的面
        if any(f[grid_layer] > 0 for f in e.link_faces):
            grid_edge_count += 1
            
    ratio = grid_edge_count / len(cycle_edges_set)
    return ratio > 0.8


def analyze_selection(bm, is_auto=False):
    """
    分析选中的或所有的外圈（边界连通分支）。
    返回：
      components: list of dict, 每个 dict 包含：
        'type': 'loop' 或 'non_linear_loops' 或 'invalid'
        'verts': 相关的顶点列表
        'edges': 相关的边列表
        # 如果是 'loop'，还包含：
        'loop_verts': 按顺序排列的循环边顶点列表
      err_msg: 如果有致命错误则返回错误信息
    """
    selected_verts = {v for v in bm.verts if v.select}
    selected_edges = {e for e in bm.edges if e.select}
    selected_faces = {f for f in bm.faces if f.select}
    
    has_selection = bool(selected_verts or selected_edges or selected_faces)
    
    # 找出整个网格中所有的外圈/边界边和边界顶点
    # 外圈边：只连接了一个面或者没有连接任何面的边（即 boundary 边或 wire 边）
    # 如果已经存在栅格化区域，边界边应包含未栅格化与已栅格化的边界交界
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if grid_layer:
        all_boundary_edges = [
            e for e in bm.edges
            if len(e.link_faces) <= 1 or (any(f[grid_layer] > 0 for f in e.link_faces) and not all(f[grid_layer] > 0 for f in e.link_faces))
        ]
    else:
        all_boundary_edges = [e for e in bm.edges if len(e.link_faces) <= 1]
        
    all_boundary_verts = list({v for e in all_boundary_edges for v in e.verts})
    
    if not all_boundary_edges:
        return None, "网格中没有找到任何外圈（边界边）。"
        
    # 构建边界邻接表
    adj = {v: [] for v in all_boundary_verts}
    
    for e in all_boundary_edges:
        v1, v2 = e.verts
        adj[v1].append(e)
        adj[v2].append(e)
        
    # BFS 划分整个网格的边界连通分支
    visited = set()
    components = []
    
    for v in all_boundary_verts:
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
        components.append((comp_verts, list(comp_edges)))
        
    # 筛选出与用户选择相关的连通分支
    if has_selection:
        # 扩展用户选择：如果选中的是面，也将其顶点 and 边加入到选择集合中
        selected_verts_extended = selected_verts.copy()
        selected_edges_extended = selected_edges.copy()
        for f in selected_faces:
            selected_verts_extended.update(f.verts)
            selected_edges_extended.update(f.edges)
            
        matched_components = []
        for comp_verts, comp_edges in components:
            # 判断连通分支是否与用户选择有交集
            has_intersect = any(v in selected_verts_extended for v in comp_verts) or \
                            any(e in selected_edges_extended for e in comp_edges)
            if has_intersect:
                matched_components.append((comp_verts, comp_edges))
                
        if not matched_components:
            return None, "选中的元素不在任何可以填充的外圈上。"
        components = matched_components
        
    parsed_components = []
    for comp_verts, comp_edges in components:
        # 度数分析
        local_degrees = {}
        for cv in comp_verts:
            deg_edges = [e for e in adj[cv] if e in comp_edges]
            local_degrees[cv] = len(deg_edges)
            
        # 1. 检查是否为简单闭合环
        is_loop = True
        for cv in comp_verts:
            if local_degrees[cv] != 2:
                is_loop = False
                break
                
        if is_loop:
            # 追踪循环边
            loop_verts = []
            start_v = comp_verts[0]
            curr_v = start_v
            prev_v = None
            while True:
                loop_verts.append(curr_v)
                next_v = None
                for e in adj[curr_v]:
                    if e in comp_edges:
                        v2 = e.other_vert(curr_v)
                        if v2 != prev_v:
                            next_v = v2
                            break
                if next_v == start_v or next_v is None:
                    break
                prev_v = curr_v
                curr_v = next_v
                
            if len(loop_verts) != len(comp_verts):
                parsed_components.append({
                    'type': 'invalid',
                    'err': "选中的循环边不是单一闭合圈。"
                })
            elif len(loop_verts) < 8:
                parsed_components.append({
                    'type': 'invalid',
                    'err': f"选中的循环边顶点数太少（当前为 {len(loop_verts)}），栅格填充至少需要 8 个顶点。"
                })
            elif len(loop_verts) % 2 != 0:
                parsed_components.append({
                    'type': 'invalid',
                    'err': f"选中的循环边顶点数必须为偶数（当前为 {len(loop_verts)}）。"
                })
            else:
                parsed_components.append({
                    'type': 'loop',
                    'verts': comp_verts,
                    'edges': comp_edges,
                    'loop_verts': loop_verts,
                    'is_grid_filled': check_is_grid_filled(bm, loop_verts, comp_edges)
                })
                
        else:
            # 检查是否有环 (C = E - V + 1)
            C = len(comp_edges) - len(comp_verts) + 1
            if C <= 0:
                parsed_components.append({
                    'type': 'invalid',
                    'err': "选中的圈不是闭合的，无法进行栅格填充。"
                })
            else:
                # 检查非线性连通分支中所有的子圈是否已被栅格化
                all_cycles_filled = False
                raw_cycles = find_minimum_cycle_basis(bm, comp_verts, comp_edges)
                if raw_cycles:
                    all_cycles_filled = True
                    for c in raw_cycles:
                        cycle_verts = trace_cycle_verts(c)
                        if not check_is_grid_filled(bm, cycle_verts, c):
                            all_cycles_filled = False
                            break
                parsed_components.append({
                    'type': 'non_linear_loops',
                    'verts': comp_verts,
                    'edges': comp_edges,
                    'is_grid_filled': all_cycles_filled
                })
                
    # 如果是自动栅格填充（由绘制自动触发，或无选择批量填充），过滤掉标记为外包围圈的连通分支
    is_automatic_fill = is_auto or (not has_selection)
    if is_automatic_fill:
        no_auto_layer = bm.edges.layers.int.get("tp_no_auto_fill")
        if no_auto_layer:
            filtered_components = []
            for c in parsed_components:
                if c['type'] == 'invalid':
                    filtered_components.append(c)
                    continue
                c_edges = c.get('edges', [])
                if c_edges:
                    marked_count = sum(1 for e in c_edges if e.is_valid and e[no_auto_layer] == 1)
                    if (marked_count / len(c_edges)) > 0.5:
                        continue
                filtered_components.append(c)
            parsed_components = filtered_components

    # 如果用户没有进行任何选择，则过滤掉已栅格化的和无效的连通分支，只自动填充未填充的有效分支
    if not has_selection:
        parsed_components = [c for c in parsed_components if c['type'] != 'invalid' and not c.get('is_grid_filled', False)]
        if not parsed_components:
            return None, "无需第二次执行栅格化"
            
    return parsed_components, None



def find_minimum_cycle_basis(bm, comp_verts, comp_edges):
    """
    使用 Horton 算法寻找图的最小环基 (MCB)
    """
    adj = {v: [] for v in comp_verts}
    for e in comp_edges:
        v1, v2 = e.verts
        adj[v1].append(e)
        adj[v2].append(e)
        
    candidate_cycles = []
    
    for e in comp_edges:
        u, v = e.verts
        # BFS 寻找不经过 e 的 u 到 v 的最短路径
        queue = [[u]]
        visited = {u}
        path_found = None
        
        while queue:
            path = queue.pop(0)
            curr = path[-1]
            if curr == v:
                path_found = path
                break
            for edge in adj[curr]:
                if edge == e:
                    continue
                v2 = edge.other_vert(curr)
                if v2 not in visited:
                    visited.add(v2)
                    queue.append(path + [v2])
                    
        if path_found:
            cycle_edges = {e}
            for idx in range(len(path_found) - 1):
                v_curr = path_found[idx]
                v_next = path_found[idx + 1]
                for ed in adj[v_curr]:
                    if ed.other_vert(v_curr) == v_next:
                        cycle_edges.add(ed)
                        break
            candidate_cycles.append(cycle_edges)
            
    # 按长度排序候选环
    candidate_cycles.sort(key=len)
    
    # 计算独立环数量 C = E - V + 1
    C = len(comp_edges) - len(comp_verts) + 1
    if C <= 0:
        return []
        
    basis = []
    selected_cycles = []
    edge_to_idx = {e: idx for idx, e in enumerate(comp_edges)}
    
    for cycle in candidate_cycles:
        vec = [0] * len(comp_edges)
        for edge in cycle:
            if edge in edge_to_idx:
                vec[edge_to_idx[edge]] = 1
                
        temp_vec = list(vec)
        for b_vec in basis:
            lead = b_vec.index(1)
            if temp_vec[lead] == 1:
                temp_vec = [x ^ y for x, y in zip(temp_vec, b_vec)]
                
        if any(x == 1 for x in temp_vec):
            basis.append(temp_vec)
            basis.sort(key=lambda x: x.index(1))
            selected_cycles.append(list(cycle))
            if len(selected_cycles) == C:
                break
                
    return selected_cycles


def trace_cycle_verts(cycle_edges):
    """
    将环边集追踪为有序的顶点闭合循环列表
    """
    verts = set()
    for e in cycle_edges:
        verts.update(e.verts)
    verts = list(verts)
    
    adj = {v: [] for v in verts}
    for e in cycle_edges:
        v1, v2 = e.verts
        adj[v1].append(v2)
        adj[v2].append(v1)
        
    loop = []
    start_v = verts[0]
    curr_v = start_v
    prev_v = None
    while True:
        loop.append(curr_v)
        neighbors = adj[curr_v]
        if len(neighbors) < 2:
            break
        next_v = neighbors[0] if neighbors[0] != prev_v else neighbors[1]
        if next_v == start_v:
            break
        prev_v = curr_v
        curr_v = next_v
    return loop


def find_shared_paths(V_i, V_j):
    """
    找出两个循环顶点列表 V_i 和 V_j 之间的所有共享连通路径。
    每个路径是一个按 V_i 中顺序排列 of BMVert 列表。
    """
    set_j = set(V_j)
    n = len(V_i)
    is_shared = [v in set_j for v in V_i]
    
    if not any(is_shared):
        return []
        
    # 寻找所有连续的 True 区间
    visited = set()
    runs = []
    for start in range(n):
        if is_shared[start] and start not in visited:
            run = [start]
            visited.add(start)
            curr = (start + 1) % n
            while is_shared[curr] and curr not in visited:
                run.append(curr)
                visited.add(curr)
                curr = (curr + 1) % n
            runs.append(run)
            
    # 如果有多个区间，且第一个区间包含 0，最后一个区间包含 n-1，说明它们跨越了起点，需要合并
    if len(runs) > 1:
        first = runs[0]
        last = runs[-1]
        if first[0] == 0 and last[-1] == n - 1:
            merged = last + first
            runs = runs[1:-1] + [merged]
            
    # 转换索引为顶点列表，且只保留边数 >= 1 的路径（即顶点数 >= 2）
    paths = []
    for run in runs:
        if len(run) >= 2:
            path_verts = [V_i[idx] for idx in run]
            paths.append(path_verts)
    return paths

def get_grid_side_and_type(corners, u, w):
    try:
        idx_u = corners.index(u)
        idx_w = corners.index(w)
    except ValueError:
        return -1, None
        
    diff = abs(idx_u - idx_w)
    if diff == 1 or diff == 3:
        if (idx_u == 0 and idx_w == 1) or (idx_u == 1 and idx_w == 0):
            return 0, 'horizontal'
        elif (idx_u == 1 and idx_w == 2) or (idx_u == 2 and idx_w == 1):
            return 1, 'vertical'
        elif (idx_u == 2 and idx_w == 3) or (idx_u == 3 and idx_w == 2):
            return 2, 'horizontal'
        else:
            return 3, 'vertical'
    return -1, None


def solve_global_grid_parameters(cycles_verts, shared_boundaries, ref_obj, topo_obj, fixed_boundaries=None):
    """
    全局求解拼接圈各子网格的最佳划分参数 (M, N, offset)，使得相邻网格共享边界的 corners 对齐，
    网格流向上下承接，且各个子网格自身的长宽比和角点形态最优。
    """
    num_cycles = len(cycles_verts)
    if num_cycles == 0:
        return []
        
    if fixed_boundaries is None:
        fixed_boundaries = []
        
    # 1. 预计算每个子圈的所有候选参数和其个体评分
    cycle_candidates = []
    for i, loop_verts in enumerate(cycles_verts):
        L = len(loop_verts)
        candidates = []
        interior_angles = compute_loop_interior_angles(loop_verts, ref_obj, topo_obj)
        
        half_L = L // 2
        coords = [v.co for v in loop_verts]
        
        # M 和 N 都必须至少为 2
        for M in range(2, half_L - 1):
            N = half_L - M
            for offset in range(L):
                i0 = offset
                i1 = (offset + M) % L
                i2 = (offset + M + N) % L
                i3 = (offset + 2 * M + N) % L
                
                p0, p1, p2, p3 = coords[i0], coords[i1], coords[i2], coords[i3]
                a, b, c, d = p1 - p0, p2 - p1, p3 - p2, p0 - p3
                a_len, b_len, c_len, d_len = a.length, b.length, c.length, d.length
                
                if a_len < 1e-6 or b_len < 1e-6 or c_len < 1e-6 or d_len < 1e-6:
                    continue
                    
                # 正交偏离度
                cos0 = abs(d.dot(a) / (d_len * a_len))
                cos1 = abs(a.dot(b) / (a_len * b_len))
                cos2 = abs(b.dot(c) / (b_len * c_len))
                cos3 = abs(c.dot(d) / (c_len * d_len))
                ortho_score = cos0 + cos1 + cos2 + cos3
                
                # 长宽比偏离度
                avg_len_x = (a_len + c_len) / (2.0 * M)
                avg_len_y = (b_len + d_len) / (2.0 * N)
                
                if avg_len_y > 1e-6 and avg_len_x > 1e-6:
                    ratio = avg_len_x / avg_len_y
                    aspect_score = max(ratio, 1.0 / ratio) - 1.0
                else:
                    aspect_score = 9999.0
                    
                # 角度惩罚项
                penalty = 0.0
                corners = {i0, i1, i2, i3}
                for j in range(L):
                    angle = interior_angles[j]
                    if angle < 90.0:
                        if j not in corners:
                            penalty += 1000.0
                    elif angle > 180.0:
                        if j in corners:
                            penalty += 1000.0
                            
                # 数量趋于相等的惩罚项 (横边和竖边的数量差值)
                div_diff_penalty = 5.0 * abs(M - N)
                
                # 综合评分：正交偏离 + 长宽比偏离 + 角度惩罚项 + 数量差值惩罚项
                score = ortho_score + 2.0 * aspect_score + penalty + div_diff_penalty
                candidates.append({
                    'M': M, 'N': N, 'offset': offset,
                    'score': score,
                    'corners': [loop_verts[i0], loop_verts[i1], loop_verts[i2], loop_verts[i3]]
                })
                
        candidates.sort(key=lambda x: x['score'])
        # 保留前 40 个最优秀的个体作为候选，既保证求解质量，又限制搜索树宽度
        cycle_candidates.append(candidates[:40])
        
    # 2. 决定回溯搜索的圈拓扑顺序 (BFS 连通树构建)
    visited = set()
    order = []
    degrees = [0] * num_cycles
    for sb in shared_boundaries:
        degrees[sb['cycle_a']] += 1
        degrees[sb['cycle_b']] += 1
        
    start_cycle = degrees.index(max(degrees)) if degrees else 0
    order.append(start_cycle)
    visited.add(start_cycle)
    
    while len(order) < num_cycles:
        best_next = None
        max_shared = -1
        for i in range(num_cycles):
            if i in visited:
                continue
            conn = sum(1 for sb in shared_boundaries if (sb['cycle_a'] == i and sb['cycle_b'] in visited) or (sb['cycle_b'] == i and sb['cycle_a'] in visited))
            if conn > max_shared:
                max_shared = conn
                best_next = i
        if best_next is not None:
            order.append(best_next)
            visited.add(best_next)
        else:
            for i in range(num_cycles):
                if i not in visited:
                    order.append(i)
                    visited.add(i)
                    break
                    
    # 3. 辅助函数：判断共享边界的两个端点是否是候选网格的 corners，并获取 side 序号与类型
    def get_shared_side_and_type(cand, u, w):
        corners = cand['corners']
        try:
            idx_u = corners.index(u)
            idx_w = corners.index(w)
        except ValueError:
            return -1, None
            
        diff = abs(idx_u - idx_w)
        if diff == 1 or diff == 3:
            if (idx_u == 0 and idx_w == 1) or (idx_u == 1 and idx_w == 0):
                return 0, 'horizontal'
            elif (idx_u == 1 and idx_w == 2) or (idx_u == 2 and idx_w == 1):
                return 1, 'vertical'
            elif (idx_u == 2 and idx_w == 3) or (idx_u == 3 and idx_w == 2):
                return 2, 'horizontal'
            else:
                return 3, 'vertical'
        return -1, None
        
    # 4. 回溯搜索最优全局参数组合
    best_global_score = float('inf')
    best_global_params = [None] * num_cycles
    
    def search(depth, current_params, current_score):
        nonlocal best_global_score, best_global_params
        
        if current_score >= best_global_score:
            return
            
        if depth == num_cycles:
            if current_score < best_global_score:
                best_global_score = current_score
                best_global_params = list(current_params)
            return
            
        curr_idx = order[depth]
        active_boundaries = []
        for sb in shared_boundaries:
            if sb['cycle_a'] == curr_idx and current_params[sb['cycle_b']] is not None:
                active_boundaries.append((sb, sb['cycle_b']))
            elif sb['cycle_b'] == curr_idx and current_params[sb['cycle_a']] is not None:
                active_boundaries.append((sb, sb['cycle_a']))
                
        # 收集该 active 圈与已填充栅格之间的固定边界约束
        curr_fixed_boundaries = [fb for fb in fixed_boundaries if fb['cycle_active'] == curr_idx]
                
        for cand in cycle_candidates[curr_idx]:
            compat_cost = 0.0
            aligned = True
            
            # Active-to-Active 边界兼容性检查
            for sb, neighbor_idx in active_boundaries:
                neighbor_cand = current_params[neighbor_idx]
                u, w = sb['endpoints']
                
                neighbor_side, neighbor_type = get_shared_side_and_type(neighbor_cand, u, w)
                curr_side, curr_type = get_shared_side_and_type(cand, u, w)
                
                if neighbor_side == -1 or curr_side == -1:
                    # 边界端点与网格角点没有完全对齐，给予惩罚
                    compat_cost += 10000.0
                    aligned = False
                else:
                    # 检查横纵类型是否一致
                    if neighbor_type != curr_type:
                        compat_cost += 5000.0
                    else:
                        # 检查流向是否上下承接（Side 0 承接 Side 2，Side 1 承接 Side 3）
                        perfect_match = False
                        if neighbor_side == 2 and curr_side == 0: perfect_match = True
                        elif neighbor_side == 0 and curr_side == 2: perfect_match = True
                        elif neighbor_side == 1 and curr_side == 3: perfect_match = True
                        elif neighbor_side == 3 and curr_side == 1: perfect_match = True
                        
                        if not perfect_match:
                            compat_cost += 2000.0  # 流向反向或错位，但轴向相同，给予惩罚以确保整体流向顺畅
                            
            # Active-to-Fixed (已填充) 边界兼容性检查
            for fb in curr_fixed_boundaries:
                u, w = fb['endpoints']
                filled_side = fb['filled_side']
                filled_type = fb['filled_type']
                
                curr_side, curr_type = get_shared_side_and_type(cand, u, w)
                
                if curr_side == -1:
                    # 与已存在栅格的角点未对齐，给予严厉惩罚
                    compat_cost += 10000.0
                    aligned = False
                else:
                    if curr_type != filled_type:
                        compat_cost += 5000.0
                    else:
                        perfect_match = False
                        if filled_side == 2 and curr_side == 0: perfect_match = True
                        elif filled_side == 0 and curr_side == 2: perfect_match = True
                        elif filled_side == 1 and curr_side == 3: perfect_match = True
                        elif filled_side == 3 and curr_side == 1: perfect_match = True
                        
                        if not perfect_match:
                            compat_cost += 2000.0  # 流向反向或错位，但轴向相同，给予惩罚以确保整体流向顺畅
                            
            if (active_boundaries or curr_fixed_boundaries) and not aligned and current_score + compat_cost >= best_global_score:
                continue
                
            next_score = current_score + cand['score'] + compat_cost
            current_params[curr_idx] = cand
            search(depth + 1, current_params, next_score)
            current_params[curr_idx] = None
            
    initial_params = [None] * num_cycles
    search(0, initial_params, 0.0)
    
    # 兜底保障
    if best_global_params[0] is None:
        for i in range(num_cycles):
            if cycle_candidates[i]:
                best_global_params[i] = cycle_candidates[i][0]
                
    return [(p['M'], p['N'], p['offset']) if p else None for p in best_global_params]



def compute_vertex_normal_from_curr_cos(v, F, curr_cos):
    normal_accum = Vector((0.0, 0.0, 0.0))
    for f in v.link_faces:
        if f in F:
            f_verts = f.verts
            if len(f_verts) >= 3:
                co0 = curr_cos[f_verts[0]]
                co1 = curr_cos[f_verts[1]]
                co2 = curr_cos[f_verts[2]]
                f_norm = (co1 - co0).cross(co2 - co0)
                if f_norm.length > 1e-6:
                    normal_accum += f_norm.normalized()
    if normal_accum.length > 1e-6:
        return normal_accum.normalized()
    return v.normal.normalized()


def global_optimize_spliced_grids(bm, ref_obj, topo_obj, iterations=40, smooth_factor=0.4, spring_factor=0.3):
    """
    对网格中所有已填充的拼接栅格区域进行全局拓扑优化（整体调优）。
    保持最外层边界点不动，释放所有内部顶点以及不同栅格区域之间的共享边界顶点，
    通过拉普拉斯平滑 + 局部自适应直联/对角弹簧 + 预松弛预热 + 双向射线/最近点贴合投影 + 物理边界碰撞，
    使得所有拼接的栅格过渡平滑自然。
    """
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if not grid_layer:
        return
        
    grid_faces = [f for f in bm.faces if f[grid_layer] > 0]
    if not grid_faces:
        return
        
    # 1. 划分连通分支
    visited_faces = set()
    components = []
    for f in grid_faces:
        if f in visited_faces:
            continue
        comp_faces = []
        queue = [f]
        visited_faces.add(f)
        while queue:
            curr = queue.pop(0)
            comp_faces.append(curr)
            for edge in curr.edges:
                for lf in edge.link_faces:
                    if lf[grid_layer] > 0 and lf not in visited_faces:
                         visited_faces.add(lf)
                         queue.append(lf)
        components.append(comp_faces)
        
    # 2. 获取高模空间变换矩阵
    if ref_obj and topo_obj:
        matrix_world_ref = ref_obj.matrix_world
        matrix_inverse_ref = matrix_world_ref.inverted()
        topo_world = topo_obj.matrix_world
        topo_inverse = topo_obj.matrix_world.inverted()
    else:
        matrix_world_ref = None
        
    # 3. 对每个连通分支独立进行优化
    for comp_faces in components:
        F = set(comp_faces)
        V = {v for f in F for v in f.verts}
        
        # 找出该分支的边界边：只连接了 F 中一个面的边
        boundary_edges = {e for e in bm.edges if len([f for f in e.link_faces if f in F]) == 1}
        boundary_verts = {v for e in boundary_edges for v in e.verts}
        
        # 内部顶点和内部共享边界顶点可以自由移动
        interior_verts = V - boundary_verts
        if not interior_verts:
            continue
            
        # 计算目标边长 L_target
        scene = bpy.context.scene
        L_target = getattr(scene, "tp_edge_length", 0.05)
        if L_target < 0.001:
            if boundary_edges:
                L_target = sum(e.calc_length() for e in boundary_edges) / len(boundary_edges)
            else:
                L_target = 0.05
                
        if boundary_edges:
            avg_boundary_len = sum(e.calc_length() for e in boundary_edges) / len(boundary_edges)
        else:
            avg_boundary_len = L_target
            
        # 预计算每个边界边的向内法线
        boundary_inwards = {}
        for e in boundary_edges:
            mid = (e.verts[0].co + e.verts[1].co) / 2.0
            connected_f = next(f for f in e.link_faces if f in F)
            center = connected_f.calc_center_median()
            inward = (center - mid).normalized()
            edge_vec = (e.verts[1].co - e.verts[0].co).normalized()
            inward_proj = (inward - inward.dot(edge_vec) * edge_vec).normalized()
            boundary_inwards[e] = inward_proj
            
        # 4. 迭代优化
        warmup_iters = int(iterations * 0.3)
        for step in range(iterations):
            should_project = (matrix_world_ref is not None) and (step >= warmup_iters)
            curr_cos = {v: v.co.copy() for v in V}
            
            for v in interior_verts:
                neighbors = []
                for e in v.link_edges:
                    if any(f in F for f in e.link_faces):
                        nb = e.other_vert(v)
                        neighbors.append((nb, e))
                        
                if not neighbors:
                    continue
                    
                # A. 拉普拉斯平滑
                pos_lap = Vector((0.0, 0.0, 0.0))
                for nb, _ in neighbors:
                    pos_lap += curr_cos[nb]
                pos_lap /= len(neighbors)
                
                # B. 弹簧力项
                force_spring = Vector((0.0, 0.0, 0.0))
                
                # 直连边弹簧
                for nb, e in neighbors:
                    diff = curr_cos[nb] - curr_cos[v]
                    length = diff.length
                    if length > 1e-6:
                        force_spring += (length - L_target) * (diff / length)
                        
                # 对角弹簧 (剪切力)
                L_diag_target = 1.41421356 * L_target
                for f in v.link_faces:
                    if f in F and len(f.verts) == 4:
                        f_verts = list(f.verts)
                        idx_v = f_verts.index(v)
                        diag_v = f_verts[(idx_v + 2) % 4]
                        diff_diag = curr_cos[diag_v] - curr_cos[v]
                        length_diag = diff_diag.length
                        if length_diag > 1e-6:
                            force_spring += 0.5 * (length_diag - L_diag_target) * (diff_diag / length_diag)
                            
                pos_spring = curr_cos[v] + 0.25 * force_spring
                relaxed_pos = curr_cos[v].lerp(pos_lap, smooth_factor).lerp(pos_spring, spring_factor)
                
                # C. 贴合高模表面（使用双向射线投影 + 备用最近点投影）
                if should_project:
                    try:
                        grid_normal = compute_vertex_normal_from_curr_cos(v, F, curr_cos)
                        
                        world_pos = topo_world @ curr_cos[v]
                        world_normal = (topo_world.to_3x3() @ grid_normal).normalized()
                        
                        local_origin = matrix_inverse_ref @ world_pos
                        local_dir = (matrix_inverse_ref.to_3x3() @ world_normal).normalized()
                        
                        # 正反双向射线投射
                        success_f, loc_f, norm_f, idx_f = ref_obj.ray_cast(local_origin, local_dir)
                        success_b, loc_b, norm_b, idx_b = ref_obj.ray_cast(local_origin, -local_dir)
                        
                        max_ray_dist = 4.0 * avg_boundary_len
                        
                        hit_pos = None
                        hit_normal = None
                        is_raycast = False
                        
                        dist_f = (loc_f - local_origin).length if success_f else float('inf')
                        dist_b = (loc_b - local_origin).length if success_b else float('inf')
                        
                        if dist_f < dist_b and dist_f < max_ray_dist:
                            hit_pos = loc_f
                            hit_normal = norm_f
                            is_raycast = True
                        elif dist_b < max_ray_dist:
                            hit_pos = loc_b
                            hit_normal = norm_b
                            is_raycast = True
                            
                        if hit_pos is None:
                            # 备用方案：高模表面最近点
                            success_cp, loc_cp, norm_cp, idx_cp = ref_obj.closest_point_on_mesh(local_origin)
                            if success_cp:
                                hit_pos = loc_cp
                                hit_normal = norm_cp
                                
                        if hit_pos is not None and hit_normal is not None:
                            normal_world = (matrix_world_ref.to_3x3() @ hit_normal).normalized()
                            world_hit_pos = matrix_world_ref @ hit_pos
                            
                            world_relaxed = topo_world @ relaxed_pos
                            disp = world_relaxed - world_hit_pos
                            disp_tangent = disp - disp.dot(normal_world) * normal_world
                            world_relaxed_tangent = world_hit_pos + disp_tangent
                            
                            local_target_tangent = matrix_inverse_ref @ world_relaxed_tangent
                            success_snap, location_snap, normal_snap, index_snap = ref_obj.closest_point_on_mesh(local_target_tangent)
                            
                            if success_snap:
                                local_pt = location_snap + normal_snap * 0.003
                                projected_pos = topo_inverse @ (matrix_world_ref @ local_pt)
                                
                                if is_raycast or (projected_pos - relaxed_pos).length < 2.0 * avg_boundary_len:
                                    relaxed_pos = projected_pos
                    except Exception:
                        pass
                        
                # D. 物理边界碰撞与厚度保护
                if boundary_edges:
                    min_dist_sq = float('inf')
                    best_proj = None
                    best_inward = None
                    best_ab_len = 0.0
                    
                    for e in boundary_edges:
                        p_curr = curr_cos[e.verts[0]]
                        p_next = curr_cos[e.verts[1]]
                        
                        AB = p_next - p_curr
                        ab_len_sq = AB.length_squared
                        if ab_len_sq < 1e-12:
                            proj = p_curr.copy()
                        else:
                            t_param = (relaxed_pos - p_curr).dot(AB) / ab_len_sq
                            t_param = max(0.0, min(1.0, t_param))
                            proj = p_curr + t_param * AB
                            
                        dist_sq = (relaxed_pos - proj).length_squared
                        if dist_sq < min_dist_sq:
                            min_dist_sq = dist_sq
                            best_proj = proj
                            best_inward = boundary_inwards.get(e, Vector((0.0, 0.0, 1.0)))
                            best_ab_len = math.sqrt(ab_len_sq)
                            
                    # 设定防穿透自适应局部物理厚度保护
                    local_margin = 0.15 * min(avg_boundary_len, best_ab_len)
                    V_vec = relaxed_pos - best_proj
                    dot_val = V_vec.dot(best_inward)
                    if dot_val < local_margin:
                        relaxed_pos = best_proj + best_inward * local_margin
                        
                v.co = relaxed_pos


def fill_non_linear_loops(bm, comp, ref_obj, topo_obj, iterations, smooth_factor, spring_factor, selected_verts=None, selected_edges=None, user_span=0, user_offset=0):
    """
    对非线性拼接圈进行多区域栅格填充，全局协调各子圈的划分参数以达到上下承接效果
    """
    comp_verts = comp['verts']
    comp_edges = comp['edges']
    
    # 1. 寻找最小环基 (MCB)
    raw_cycles = find_minimum_cycle_basis(bm, comp_verts, comp_edges)
    if not raw_cycles:
        return 0
    cycles = [set(c) for c in raw_cycles]
    
    # 2. 为奇数顶点圈进行边缘细分，确保所有子圈的顶点数均为偶数，以便全局协调划分
    for cycle_idx in range(len(cycles)):
        cycle_edges = cycles[cycle_idx]
        loop_verts = trace_cycle_verts(cycle_edges)
        
        if check_is_grid_filled(bm, loop_verts, cycle_edges):
            continue
            
        if selected_verts or selected_edges:
            has_intersect = any(v in selected_verts for v in loop_verts) or \
                            any(e in selected_edges for e in cycle_edges)
            if not has_intersect:
                continue
                
        if len(loop_verts) % 2 != 0:
            edge_counts = {}
            for c in cycles:
                for e in c:
                    edge_counts[e] = edge_counts.get(e, 0) + 1
                        
            best_edge = None
            min_count = 99999
            max_len = -1.0
            
            for e in cycle_edges:
                count = edge_counts.get(e, 1)
                if count < min_count:
                    min_count = count
                    best_edge = e
                    max_len = e.calc_length()
                elif count == min_count:
                    l = e.calc_length()
                    if l > max_len:
                        max_len = l
                        best_edge = e
                        
            if best_edge:
                res = bmesh.ops.subdivide_edges(bm, edges=[best_edge], cuts=1)
                bm.verts.index_update()
                bm.edges.index_update()
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                
                new_verts = [g for g in res['geom_split'] if isinstance(g, bmesh.types.BMVert)]
                if new_verts:
                    new_v = new_verts[0]
                    new_edges = list(new_v.link_edges)
                    for c in cycles:
                        if best_edge in c:
                            c.remove(best_edge)
                            for ne in new_edges:
                                c.add(ne)
                                
    # 3. 收集所有待填充的 active 圈顶点，并寻找它们之间的共享边界
    active_indices = []
    cycles_verts = {}
    for idx in range(len(cycles)):
        cycle_edges = cycles[idx]
        loop_verts = trace_cycle_verts(cycle_edges)
        
        if check_is_grid_filled(bm, loop_verts, cycle_edges):
            continue
            
        if selected_verts or selected_edges:
            has_intersect = any(v in selected_verts for v in loop_verts) or \
                            any(e in selected_edges for e in cycle_edges)
            if not has_intersect:
                continue
                
        active_indices.append(idx)
        cycles_verts[idx] = loop_verts
        
    if not active_indices:
        return 0
        
    # 寻找共享边界
    shared_boundaries = []
    for i in range(len(active_indices)):
        for j in range(i + 1, len(active_indices)):
            idx_a = active_indices[i]
            idx_b = active_indices[j]
            V_a = cycles_verts[idx_a]
            V_b = cycles_verts[idx_b]
            
            paths = find_shared_paths(V_a, V_b)
            for p in paths:
                shared_boundaries.append({
                    'cycle_a': idx_a,
                    'cycle_b': idx_b,
                    'path': p,
                    'length': len(p) - 1,
                    'endpoints': (p[0], p[-1])
                })
                
    # 4. 全局协调求解所有待填充圈的最佳参数
    # 映射索引到 0..K-1 列表传给 solver
    active_loops = [cycles_verts[idx] for idx in active_indices]
    mapped_boundaries = []
    for sb in shared_boundaries:
        idx_a_mapped = active_indices.index(sb['cycle_a'])
        idx_b_mapped = active_indices.index(sb['cycle_b'])
        mapped_boundaries.append({
            'cycle_a': idx_a_mapped,
            'cycle_b': idx_b_mapped,
            'path': sb['path'],
            'length': sb['length'],
            'endpoints': sb['endpoints']
        })
        
    # 寻找与待填充圈相邻的、已填充的栅格区域
    fixed_boundaries = []
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if grid_layer:
        shared_filled_loop_ids = set()
        for idx in active_indices:
            cycle_edges = cycles[idx]
            for e in cycle_edges:
                for f in e.link_faces:
                    lid = f[grid_layer]
                    if lid > 0:
                        shared_filled_loop_ids.add(lid)
                        
        for loop_id in shared_filled_loop_ids:
            F_filled = [f for f in bm.faces if f[grid_layer] == loop_id]
            if not F_filled:
                continue
                
            F_filled_set = set(F_filled)
            E_filled_b = []
            for f in F_filled:
                for e in f.edges:
                    if len([lf for lf in e.link_faces if lf in F_filled_set]) == 1:
                        E_filled_b.append(e)
            if not E_filled_b:
                continue
                
            try:
                filled_loop_verts = trace_cycle_verts(E_filled_b)
            except Exception:
                continue
            if not filled_loop_verts:
                continue
                
            # 寻找该已填充区域的 4 个角点
            corners = []
            for v in filled_loop_verts:
                if len([lf for lf in v.link_faces if lf in F_filled_set]) == 1:
                    corners.append(v)
            if len(corners) != 4:
                continue
                
            # 计算共享边界的固定约束
            for idx in active_indices:
                V_active = cycles_verts[idx]
                paths = find_shared_paths(V_active, filled_loop_verts)
                for p in paths:
                    u, w = p[0], p[-1]
                    filled_side, filled_type = get_grid_side_and_type(corners, u, w)
                    if filled_side != -1:
                        fixed_boundaries.append({
                            'cycle_active': active_indices.index(idx),
                            'endpoints': (u, w),
                            'filled_side': filled_side,
                            'filled_type': filled_type
                        })
                        
    solved_params = solve_global_grid_parameters(active_loops, mapped_boundaries, ref_obj, topo_obj, fixed_boundaries=fixed_boundaries)
    params_map = {active_indices[k]: solved_params[k] for k in range(len(active_indices))}
    
    # 5. 按照协调好的参数为每个圈独立生成初始 Coons 栅格并拓扑生成面
    faces_total = 0
    last_span = 0
    last_offset = 0
    
    for idx in active_indices:
        bm.verts.index_update()
        bm.edges.index_update()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        
        cycle_edges = cycles[idx]
        loop_verts = cycles_verts[idx]
        L = len(loop_verts)
        
        M, N, offset = params_map[idx]
        
        if user_span >= 2:
            half_L = L // 2
            N = max(2, min(half_L - 2, user_span))
            M = half_L - N
            
        offset = (offset + user_offset) % L
        
        # 记录最后一圈的参数作为最优跨分和偏移的参考
        last_span = N
        last_offset = offset
        
        grid_coords = init_coons_grid(loop_verts, M, N, offset)
        optimize_grid(
            grid_coords, M, N,
            ref_obj=ref_obj,
            topo_obj=topo_obj,
            iterations=iterations,
            smooth_factor=smooth_factor,
            spring_factor=spring_factor
        )
        
        def get_loop_vert(index):
            return loop_verts[index % L]
            
        grid_verts = [[None for _ in range(N + 1)] for _ in range(M + 1)]
        grid_verts[0][0] = get_loop_vert(offset)
        grid_verts[M][0] = get_loop_vert(offset + M)
        grid_verts[M][N] = get_loop_vert(offset + M + N)
        grid_verts[0][N] = get_loop_vert(offset + 2 * M + N)
        
        for u in range(1, M):
            grid_verts[u][0] = get_loop_vert(offset + u)
            grid_verts[u][N] = get_loop_vert(offset + 2 * M + N - u)
        for v in range(1, N):
            grid_verts[M][v] = get_loop_vert(offset + M + v)
            grid_verts[0][v] = get_loop_vert(offset - v)
            
        for u in range(1, M):
            for v in range(1, N):
                vert = bm.verts.new(grid_coords[u][v])
                grid_verts[u][v] = vert
                
        bm.verts.ensure_lookup_table()
        
        grid_layer = bm.faces.layers.int.get("tp_is_grid") or bm.faces.layers.int.new("tp_is_grid")
        max_id = max([f[grid_layer] for f in bm.faces] + [0])
        loop_id = max_id + 1
        
        faces_created = 0
        for u in range(M):
            for v in range(N):
                v00 = grid_verts[u][v]
                v10 = grid_verts[u+1][v]
                v11 = grid_verts[u+1][v+1]
                v01 = grid_verts[u][v+1]
                try:
                    f = bm.faces.new((v00, v10, v11, v01))
                    f[grid_layer] = loop_id
                    faces_created += 1
                except Exception:
                    pass
                    
        faces_total += faces_created
        
    return faces_total, last_span, last_offset


def compute_loop_interior_angles(loop_verts, ref_obj, topo_obj):
    import math
    from mathutils import Vector
    L = len(loop_verts)
    coords = [v.co for v in loop_verts]
    
    def safe_normalize(v):
        l = v.length
        if l > 1e-6:
            return v / l
        return v
        
    # 1. 获取每个顶点的法线
    normals = []
    if ref_obj and topo_obj:
        matrix_world_ref = ref_obj.matrix_world
        matrix_inverse_ref = ref_obj.matrix_world.inverted()
        topo_world = topo_obj.matrix_world
        topo_inverse = topo_obj.matrix_world.inverted()
        for co in coords:
            world_pos = topo_world @ co
            local_target = matrix_inverse_ref @ world_pos
            success, location, normal_ref, index = ref_obj.closest_point_on_mesh(local_target)
            if success:
                normal_world = (matrix_world_ref.to_3x3() @ normal_ref).normalized()
                normal_topo = (topo_inverse.to_3x3() @ normal_world).normalized()
                normals.append(normal_topo)
            else:
                normals.append(safe_normalize(loop_verts[0].normal.normalized()))
    else:
        for v in loop_verts:
            normals.append(safe_normalize(v.normal.normalized()))
            
    # 2. 计算有符号转向角之和，确定环的顺时针/逆时针方向
    turning_angles = []
    for j in range(L):
        prev_idx = (j - 1) % L
        next_idx = (j + 1) % L
        
        P_prev = coords[prev_idx]
        P_curr = coords[j]
        P_next = coords[next_idx]
        N = normals[j]
        
        u = safe_normalize(P_curr - P_prev)
        w = safe_normalize(P_next - P_curr)
        
        u_proj = safe_normalize(u - u.dot(N) * N)
        w_proj = safe_normalize(w - w.dot(N) * N)
        
        x = u_proj
        y = safe_normalize(N.cross(x))
        
        w_x = w_proj.dot(x)
        w_y = w_proj.dot(y)
        
        phi = math.atan2(w_y, w_x)
        turning_angles.append(phi)
        
    sum_phi = sum(turning_angles)
    is_ccw = (sum_phi >= 0.0)
    
    # 3. 计算内角（角度制）
    interior_angles = []
    for j in range(L):
        prev_idx = (j - 1) % L
        next_idx = (j + 1) % L
        
        P_prev = coords[prev_idx]
        P_curr = coords[j]
        P_next = coords[next_idx]
        N = normals[j]
        
        v1 = safe_normalize(P_prev - P_curr)
        v2 = safe_normalize(P_next - P_curr)
        
        v1_proj = safe_normalize(v1 - v1.dot(N) * N)
        v2_proj = safe_normalize(v2 - v2.dot(N) * N)
        
        if is_ccw:
            x = v2_proj
            y = safe_normalize(N.cross(x))
            val_x = v1_proj.dot(x)
            val_y = v1_proj.dot(y)
        else:
            x = v1_proj
            y = safe_normalize(N.cross(x))
            val_x = v2_proj.dot(x)
            val_y = v2_proj.dot(y)
            
        alpha = math.atan2(val_y, val_x)
        if alpha < 0:
            alpha += 2.0 * math.pi
            
        interior_angles.append(math.degrees(alpha))
        
    return interior_angles


def find_best_corners_3d(loop_verts, ref_obj=None, topo_obj=None):
    """
    在 3D 空间中直接搜索最佳的角点分段参数 (M, N, offset)，避免 2D 投影引起的拉伸失真。
    M: 横向划分段数
    N: 纵向划分段数
    offset: 起点索引偏移量
    """
    L = len(loop_verts)
    interior_angles = compute_loop_interior_angles(loop_verts, ref_obj, topo_obj)
    
    best_score = float('inf')
    best_params = None
    
    half_L = L // 2
    coords = [v.co for v in loop_verts]
    
    # M 和 N 代表四边形两个方向的边数，都必须至少为 2
    for M in range(2, half_L - 1):
        N = half_L - M
        for offset in range(L):
            i0 = offset
            i1 = (offset + M) % L
            i2 = (offset + M + N) % L
            i3 = (offset + 2 * M + N) % L
            
            p0 = coords[i0]
            p1 = coords[i1]
            p2 = coords[i2]
            p3 = coords[i3]
            
            # 计算 4 条边界的 3D 弦向量
            a = p1 - p0
            b = p2 - p1
            c = p3 - p2
            d = p0 - p3
            
            a_len = a.length
            b_len = b.length
            c_len = c.length
            d_len = d.length
            
            if a_len < 1e-6 or b_len < 1e-6 or c_len < 1e-6 or d_len < 1e-6:
                continue
                
            # 计算 3D 弦向量之间的夹角余弦，评估 4 个角点的正交性偏离值（越接近 90 度，值越小）
            cos0 = abs(d.dot(a) / (d_len * a_len))
            cos1 = abs(a.dot(b) / (a_len * b_len))
            cos2 = abs(b.dot(c) / (b_len * c_len))
            cos3 = abs(c.dot(d) / (c_len * d_len))
            ortho_score = cos0 + cos1 + cos2 + cos3
            
            # 计算网格单元的平均长宽比偏离值，使单元更接近正方形
            avg_len_x = (a_len + c_len) / (2.0 * M)
            avg_len_y = (b_len + d_len) / (2.0 * N)
            
            if avg_len_y > 1e-6 and avg_len_x > 1e-6:
                ratio = avg_len_x / avg_len_y
                aspect_score = max(ratio, 1.0 / ratio) - 1.0
            else:
                aspect_score = float('inf')
            
            # 引入角度限制规则的惩罚项：
            # 1. 小于 90 度的顶点不可以接新边（必须成为 4 个角点之一）
            # 2. 大于 180 度的顶点必须接一条新边（绝不能是角点）
            penalty = 0.0
            corners = {i0, i1, i2, i3}
            for j in range(L):
                angle = interior_angles[j]
                if angle < 90.0:
                    if j not in corners:
                        penalty += 1000.0
                elif angle > 180.0:
                    if j in corners:
                        penalty += 1000.0
            
            # 数量趋于相等的惩罚项 (横边 and 竖边的数量差值)
            div_diff_penalty = 5.0 * abs(M - N)
            
            # 综合评分：正交偏离 + 长宽比偏离 + 规则惩罚项 + 数量差异惩罚项
            score = ortho_score + 2.0 * aspect_score + penalty + div_diff_penalty
            
            if score < best_score:
                best_score = score
                best_params = (M, N, offset)
                
    return best_params


def init_coons_grid(loop_verts, M, N, offset):
    """
    通过库恩斯曲面 (Coons Patch) 双线性混合插值初始化内部网格顶点坐标
    """
    L = len(loop_verts)
    i0 = offset
    i1 = (offset + M) % L
    i2 = (offset + M + N) % L
    i3 = (offset + 2 * M + N) % L
    
    def get_loop_vert(index):
        return loop_verts[index % L]
        
    grid_coords = [[None for _ in range(N + 1)] for _ in range(M + 1)]
    
    # 填充 4 个角点坐标
    grid_coords[0][0] = get_loop_vert(i0).co.copy()
    grid_coords[M][0] = get_loop_vert(i1).co.copy()
    grid_coords[M][N] = get_loop_vert(i2).co.copy()
    grid_coords[0][N] = get_loop_vert(i3).co.copy()
    
    # 填充 4 条边界物理曲线坐标
    for u in range(1, M):
        grid_coords[u][0] = get_loop_vert(i0 + u).co.copy()
        grid_coords[u][N] = get_loop_vert(i3 - u).co.copy()
    for v in range(1, N):
        grid_coords[M][v] = get_loop_vert(i1 + v).co.copy()
        grid_coords[0][v] = get_loop_vert(i0 - v).co.copy()
        
    # Coons Patch 双线性混合插值内部坐标
    for u in range(1, M):
        x = u / M
        for v in range(1, N):
            y = v / N
            
            # 边界曲线值
            A = grid_coords[u][0]
            C = grid_coords[u][N]
            D = grid_coords[0][v]
            B = grid_coords[M][v]
            
            # 角点值
            c00 = grid_coords[0][0]
            c10 = grid_coords[M][0]
            c01 = grid_coords[0][N]
            c11 = grid_coords[M][N]
            
            # 经典 Coons 曲面插值公式
            P = (1.0 - y) * A + y * C + (1.0 - x) * D + x * B - \
                ((1.0 - x) * (1.0 - y) * c00 + x * (1.0 - y) * c10 + \
                 (1.0 - x) * y * c01 + x * y * c11)
                 
            grid_coords[u][v] = P
            
    return grid_coords


def optimize_grid(grid_coords, M, N, ref_obj, topo_obj, iterations=40, smooth_factor=0.4, spring_factor=0.3):
    """
    拉普拉斯平滑 + 局部自适应直联/对角弹簧 + 预松弛预热机制 + 渐进式拓扑拉力控制 + 双向射线/最近点贴合投影 + 物理边界碰撞与安全守护
    """
    # 估算网格的整体法线方向，用于边界碰撞检测与默认投影方向
    c00 = grid_coords[0][0]
    c10 = grid_coords[M][0]
    c01 = grid_coords[0][N]
    normal_dir = (c10 - c00).cross(c01 - c00).normalized()
    
    # 1. 预计算每个网格边局部的目标长度
    # 计算四条边界 of 各段弦长
    # Bottom 边界 (v = 0): u = 0..M-1
    L_bottom = [0.0] * M
    for u in range(M):
        L_bottom[u] = (grid_coords[u+1][0] - grid_coords[u][0]).length
        
    # Top 边界 (v = N): u = 0..M-1
    L_top = [0.0] * M
    for u in range(M):
        L_top[u] = (grid_coords[u+1][N] - grid_coords[u][N]).length
        
    # 双线性插值预计算网格中每一条边的目标长度
    # target_u[u][v] 表示连接 (u,v) 和 (u+1,v) 的边长，尺寸为 M x (N+1)
    target_u = [[0.0 for _ in range(N + 1)] for _ in range(M)]
    for u in range(M):
        for v in range(N + 1):
            target_u[u][v] = (1.0 - v / N) * L_bottom[u] + (v / N) * L_top[u]
            
    # target_v[u][v] 表示连接 (u,v) 和 (u,v+1) 的边长，尺寸为 (M+1) x N
    # 对于纵向边，我们使用该列首尾边界点之间的实际距离进行平分，防止在两端极窄的凹陷结构中出现弹簧缩水导致的网格堆叠。
    target_v = [[0.0 for _ in range(N)] for _ in range(M + 1)]
    for u in range(M + 1):
        col_len = (grid_coords[u][N] - grid_coords[u][0]).length
        avg_len = col_len / N if N > 0 else 0.0
        for v in range(N):
            target_v[u][v] = avg_len
            
    # target_diag[u][v] 表示 cell(u,v) 的对角线目标长度，尺寸为 M x N
    target_diag = [[0.0 for _ in range(N)] for _ in range(M)]
    for u in range(M):
        for v in range(N):
            # 取周围四个直连接局部边长的平均，然后按勾股定理估算对角线长
            lu = (target_u[u][v] + target_u[u][v+1]) / 2.0
            lv = (target_v[u][v] + target_v[u+1][v]) / 2.0
            target_diag[u][v] = (lu*lu + lv*lv) ** 0.5

    # 计算全局边界平均边长，用于投影最大偏移阈值检测
    sum_left = sum((grid_coords[0][v+1] - grid_coords[0][v]).length for v in range(N))
    sum_right = sum((grid_coords[M][v+1] - grid_coords[M][v]).length for v in range(N))
    avg_boundary_len = (sum(L_bottom) + sum(L_top) + sum_left + sum_right) / (2 * (M + N))
    
    # 构造边界循环顶点坐标列表，用于物理边界碰撞与安全守护
    boundary_loop = []
    for u in range(M):
        boundary_loop.append(grid_coords[u][0])
    for v in range(N):
        boundary_loop.append(grid_coords[M][v])
    for u in range(M, 0, -1):
        boundary_loop.append(grid_coords[u][N])
    for v in range(N, 0, -1):
        boundary_loop.append(grid_coords[0][v])
        
    # 确保 boundary_loop 是逆时针 (CCW) 方向，以便叉乘计算的法线始终指向多边形内部
    total_area = 0.0
    for idx in range(len(boundary_loop)):
        p_curr = boundary_loop[idx]
        p_next = boundary_loop[(idx + 1) % len(boundary_loop)]
        total_area += p_curr.cross(p_next).dot(normal_dir)
    if total_area < 0:
        boundary_loop.reverse()
    
    # 2. 准备高模空间变换矩阵
    if ref_obj and topo_obj:
        matrix_world_ref = ref_obj.matrix_world
        matrix_inverse_ref = matrix_world_ref.inverted()
        topo_world = topo_obj.matrix_world
        topo_inverse = topo_obj.matrix_world.inverted()
    else:
        matrix_world_ref = None
        
    # 预松弛（Warm-up）迭代次数：前 30% 迭代不投影，只在 3D 中将网格结构拉均匀以解开折叠
    warmup_iters = int(iterations * 0.3)
    
    # 3. 迭代松弛
    for step in range(iterations):
        should_project = (matrix_world_ref is not None) and (step >= warmup_iters)
        
        # 复制当前点坐标
        curr_coords = [[grid_coords[u][v].copy() for v in range(N + 1)] for u in range(M + 1)]
        
        for u in range(1, M):
            for v in range(1, N):
                pos = curr_coords[u][v]
                
                # 计算局部网格法线，用于投影和边界碰撞检测（提取到最前面，保证全局可用且最新）
                tangent_u = curr_coords[u+1][v] - curr_coords[u-1][v]
                tangent_v = curr_coords[u][v+1] - curr_coords[u][v-1]
                cross_prod = tangent_u.cross(tangent_v)
                if cross_prod.length > 1e-6:
                    grid_normal = cross_prod.normalized()
                else:
                    grid_normal = normal_dir.copy()
                
                # 获取直连邻居
                n_left = curr_coords[u-1][v]
                n_right = curr_coords[u+1][v]
                n_bottom = curr_coords[u][v-1]
                n_top = curr_coords[u][v+1]
                
                # A. 拉普拉斯平滑项：向直连邻居的几何中心靠近
                pos_lap = (n_left + n_right + n_bottom + n_top) / 4.0
                
                # B. 弹簧力项（应用局部自适应弹簧长度约束）
                force_spring = Vector((0.0, 0.0, 0.0))
                
                # 左直连弹簧力
                diff = n_left - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += (length - target_u[u-1][v]) * (diff / length)
                # 右直连弹簧力
                diff = n_right - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += (length - target_u[u][v]) * (diff / length)
                # 下直连弹簧力
                diff = n_bottom - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += (length - target_v[u][v-1]) * (diff / length)
                # 上直连弹簧力
                diff = n_top - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += (length - target_v[u][v]) * (diff / length)
                    
                # 获取四个对角邻居
                n_bottom_left = curr_coords[u-1][v-1]
                n_top_right = curr_coords[u+1][v+1]
                n_top_left = curr_coords[u-1][v+1]
                n_bottom_right = curr_coords[u+1][v-1]
                
                # 对角弹簧力（抗剪切，权重 0.5）
                # 左下角对角弹簧
                diff = n_bottom_left - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += 0.5 * (length - target_diag[u-1][v-1]) * (diff / length)
                # 右上角对角弹簧
                diff = n_top_right - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += 0.5 * (length - target_diag[u][v]) * (diff / length)
                # 左上角对角弹簧
                diff = n_top_left - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += 0.5 * (length - target_diag[u-1][v]) * (diff / length)
                # 右下角对角弹簧
                diff = n_bottom_right - pos
                length = diff.length
                if length > 1e-6:
                    force_spring += 0.5 * (length - target_diag[u][v-1]) * (diff / length)
                    
                pos_spring = pos + 0.25 * force_spring
                
                # 混合拉普拉斯和自适应弹簧力
                relaxed_pos = pos.lerp(pos_lap, smooth_factor).lerp(pos_spring, spring_factor)
                
                # C. 渐进式拓扑拉力控制（Barycentric Restorative Pull）
                # 放在投影之前，使其作为一种松弛力，通过投影贴合到表面上，防止拉力导致顶点下陷到模型内部
                B = grid_coords[u][0]
                T = grid_coords[u][N]
                col_vec = T - B
                col_len = col_vec.length
                if col_len > 1e-6:
                    col_dir = col_vec / col_len
                    diff = relaxed_pos - B
                    t_val = diff.dot(col_dir)
                    t = t_val / col_len
                    y_target = v / N
                    
                    # 仅在纵向上往理想拓扑高度微调，防止行重叠
                    t_new = t + 0.15 * (y_target - t)
                    relaxed_pos = relaxed_pos + ((t_new - t) * col_len) * col_dir
                        
                L_bound = grid_coords[0][v]
                R_bound = grid_coords[M][v]
                row_vec = R_bound - L_bound
                row_len = row_vec.length
                if row_len > 1e-6:
                    row_dir = row_vec / row_len
                    diff = relaxed_pos - L_bound
                    s_val = diff.dot(row_dir)
                    s = s_val / row_len
                    x_target = u / M
                    
                    # 仅在横向上往理想拓扑宽度微调，防止列重叠
                    s_new = s + 0.15 * (x_target - s)
                    relaxed_pos = relaxed_pos + ((s_new - s) * row_len) * row_dir

                # D. 贴合高模表面（使用双向射线投影 + 备用最近点投影）
                if should_project:
                    try:
                        # 转换当前位置到高模空间，获取投影射线起点与方向
                        world_pos = topo_world @ pos
                        world_normal = (topo_world.to_3x3() @ grid_normal).normalized()
                        
                        local_origin = matrix_inverse_ref @ world_pos
                        local_dir = (matrix_inverse_ref.to_3x3() @ world_normal).normalized()
                        
                        # 在正反两个方向进行射线求交
                        success_f, loc_f, norm_f, idx_f = ref_obj.ray_cast(local_origin, local_dir)
                        success_b, loc_b, norm_b, idx_b = ref_obj.ray_cast(local_origin, -local_dir)
                        
                        max_ray_dist = max(M, N) * avg_boundary_len * 2.5
                        
                        hit_pos = None
                        hit_normal = None
                        is_raycast = False
                        
                        dist_f = (loc_f - local_origin).length if success_f else float('inf')
                        dist_b = (loc_b - local_origin).length if success_b else float('inf')
                        
                        # 优先选择距离较近且在合理范围内的交点
                        if dist_f < dist_b and dist_f < max_ray_dist:
                            hit_pos = loc_f
                            hit_normal = norm_f
                            is_raycast = True
                        elif dist_b < max_ray_dist:
                            hit_pos = loc_b
                            hit_normal = norm_b
                            is_raycast = True
                            
                        if hit_pos is None:
                            # 备用方案：寻找高模表面最近点
                            success_cp, loc_cp, norm_cp, idx_cp = ref_obj.closest_point_on_mesh(local_origin)
                            if success_cp:
                                hit_pos = loc_cp
                                hit_normal = norm_cp
                                
                        if hit_pos is not None and hit_normal is not None:
                            normal_world = (matrix_world_ref.to_3x3() @ hit_normal).normalized()
                            world_hit_pos = matrix_world_ref @ hit_pos
                            
                            # 切向松弛投影
                            world_relaxed = topo_world @ relaxed_pos
                            disp = world_relaxed - world_hit_pos
                            disp_tangent = disp - disp.dot(normal_world) * normal_world
                            world_relaxed_tangent = world_hit_pos + disp_tangent
                            
                            local_target_tangent = matrix_inverse_ref @ world_relaxed_tangent
                            success_snap, location_snap, normal_snap, index_snap = ref_obj.closest_point_on_mesh(local_target_tangent)
                            
                            if success_snap:
                                local_pt = location_snap + normal_snap * 0.003
                                projected_pos = topo_inverse @ (matrix_world_ref @ local_pt)
                                
                                # 如果是通过射线投射找到的，直接采纳；如果是备用最近点，保留距离安全限制
                                if is_raycast or (projected_pos - relaxed_pos).length < 2.0 * avg_boundary_len:
                                    relaxed_pos = projected_pos
                    except Exception:
                        pass

                # E. 物理边界碰撞与安全守护 (Boundary Collision Guard)
                # 使用局部网格法线 grid_normal 代替全局法线 normal_dir，确保在陡峭表面上 inward 方向始终指向网格内部。
                # 设定防穿透物理厚度保护（自适应局部边长，防止在狭窄区域过度排斥导致网格折叠与畸变）。
                min_dist_sq = float('inf')
                best_proj = None
                best_inward = None
                best_ab_len = 0.0
                
                for idx in range(len(boundary_loop)):
                    p_curr = boundary_loop[idx]
                    p_next = boundary_loop[(idx + 1) % len(boundary_loop)]
                    
                    AB = p_next - p_curr
                    ab_len_sq = AB.length_squared
                    if ab_len_sq < 1e-12:
                        proj = p_curr.copy()
                    else:
                        t_param = (relaxed_pos - p_curr).dot(AB) / ab_len_sq
                        t_param = max(0.0, min(1.0, t_param))
                        proj = p_curr + t_param * AB
                        
                    dist_sq = (relaxed_pos - proj).length_squared
                    if dist_sq < min_dist_sq:
                        min_dist_sq = dist_sq
                        best_proj = proj
                        edge_dir = AB.normalized() if ab_len_sq > 1e-12 else Vector((1.0, 0.0, 0.0))
                        best_inward = grid_normal.cross(edge_dir).normalized()
                        best_ab_len = math.sqrt(ab_len_sq)
                        
                # 设定防穿透自适应局部物理厚度保护
                local_margin = 0.15 * min(avg_boundary_len, best_ab_len)
                V_collision = relaxed_pos - best_proj
                dot_collision = V_collision.dot(best_inward)
                if dot_collision < local_margin:
                    relaxed_pos = best_proj + best_inward * local_margin
                        
                grid_coords[u][v] = relaxed_pos


class OBJECT_OT_tp_topology_grid_fill(bpy.types.Operator):
    bl_idname = "object.tp_topology_grid_fill"
    bl_label = "TP拓扑栅格填充"
    bl_description = "将选中的圈进行高质量的栅格化填充，并优化为均匀的正方形面"
    bl_options = {'REGISTER', 'UNDO'}
    
    is_auto: bpy.props.BoolProperty(
        name="是否自动填充",
        default=False,
        description="是否由绘制操作自动触发的填充"
    )
    
    # 导出可调节的属性，支持重做面板
    iterations: bpy.props.IntProperty(
        name="平滑迭代",
        default=40,
        min=1,
        max=200,
        description="内部网格松弛优化的迭代次数"
    )
    
    smooth_factor: bpy.props.FloatProperty(
        name="平滑力度",
        default=0.4,
        min=0.0,
        max=1.0,
        description="拉普拉斯平滑的权重，使网格过渡平顺"
    )
    
    spring_factor: bpy.props.FloatProperty(
        name="边长均匀度",
        default=0.3,
        min=0.0,
        max=1.0,
        description="弹簧强度约束权重，控制面大小均匀度，防止收缩"
    )
    
    project_to_ref: bpy.props.BoolProperty(
        name="贴合表面",
        default=True,
        description="是否将内部栅格投影贴合到高模物体表面"
    )
    
    span: bpy.props.IntProperty(
        name="跨分 (Span)",
        default=0,
        min=0,
        description="栅格一边的网格数。0 表示自动计算（Auto）"
    )
    
    offset: bpy.props.IntProperty(
        name="偏移 (Offset)",
        default=0,
        description="网格顶点的起点偏移量，相对于自动寻找的最佳起点进行偏移"
    )
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'
        
    def execute(self, context):
        # Sync scene settings with operator properties if they were modified in the Redo panel
        if self.span == 0 and context.scene.tp_grid_span != 0:
            self.span = context.scene.tp_grid_span
        if self.offset == 0 and context.scene.tp_grid_offset != 0:
            self.offset = context.scene.tp_grid_offset
            
        if self.span != context.scene.tp_grid_span:
            context.scene.tp_grid_span = self.span
        if self.offset != context.scene.tp_grid_offset:
            context.scene.tp_grid_offset = self.offset

        topo_obj = context.active_object
        if not topo_obj or topo_obj.type != 'MESH':
            self.report({'ERROR'}, "活动对象不是网格体")
            return {'CANCELLED'}
            
        # 获取 BMesh 数据
        bm = bmesh.from_edit_mesh(topo_obj.data)
        
        # 1. 提取并分析选中的图形
        components, err_msg = analyze_selection(bm, is_auto=self.is_auto)
        if err_msg:
            self.report({'ERROR'}, err_msg)
            return {'CANCELLED'}
            
        # 检查是否有任何 invalid 连通分支
        for idx, comp in enumerate(components):
            if comp['type'] == 'invalid':
                self.report({'ERROR'}, f"分支 #{idx+1} 错误: {comp['err']}")
                return {'CANCELLED'}
                
        # 检查是否所有匹配的圈都已栅格化
        if all(comp.get('is_grid_filled', False) for comp in components):
            self.report({'WARNING'}, "选中的圈已经全部被栅格化")
            return {'CANCELLED'}
                
        # 获取扩展选择集合，用于在多圈拼接情况下精确定位要填充的子圈
        selected_verts = {v for v in bm.verts if v.select}
        selected_edges = {e for e in bm.edges if e.select}
        selected_faces = {f for f in bm.faces if f.select}
        
        selected_verts_extended = selected_verts.copy()
        selected_edges_extended = selected_edges.copy()
        for f in selected_faces:
            selected_verts_extended.update(f.verts)
            selected_edges_extended.update(f.edges)
                
        # 查找高模参考物体
        ref_obj = None
        if self.project_to_ref:
            ref_obj_name = context.window_manager.tp_ref_object_name
            if ref_obj_name:
                ref_obj = bpy.data.objects.get(ref_obj_name)
                
        # 填充各个连通分支
        loops_filled = 0
        joined_filled = 0
        total_faces = 0
        
        last_optimal_span = 0
        last_optimal_offset = 0
        
        for comp in components:
            if comp.get('is_grid_filled', False):
                continue
                
            if comp['type'] == 'loop':
                # 执行标准单闭合圈填充
                loop_verts = comp['loop_verts']
                L = len(loop_verts)
                best_params = find_best_corners_3d(loop_verts, ref_obj=ref_obj, topo_obj=topo_obj)
                if not best_params:
                    self.report({'ERROR'}, "单圈填充失败：无法找到合适的划分方案")
                    return {'CANCELLED'}
                    
                M, N, offset = best_params
                
                # 记录最优参数
                last_optimal_span = N
                last_optimal_offset = offset
                
                # Apply user span override if specified (span >= 2)
                if self.span >= 2:
                    half_L = L // 2
                    N = max(2, min(half_L - 2, self.span))
                    M = half_L - N
                    
                # Apply user offset override
                offset = (offset + self.offset) % L
                
                grid_coords = init_coons_grid(loop_verts, M, N, offset)
                
                optimize_grid(
                    grid_coords, M, N, 
                    ref_obj=ref_obj, 
                    topo_obj=topo_obj, 
                    iterations=self.iterations, 
                    smooth_factor=self.smooth_factor, 
                    spring_factor=self.spring_factor
                )
                
                # 创建顶点并生成面
                def get_loop_vert(index):
                    return loop_verts[index % L]
                    
                grid_verts = [[None for _ in range(N + 1)] for _ in range(M + 1)]
                grid_verts[0][0] = get_loop_vert(offset)
                grid_verts[M][0] = get_loop_vert(offset + M)
                grid_verts[M][N] = get_loop_vert(offset + M + N)
                grid_verts[0][N] = get_loop_vert(offset + 2 * M + N)
                
                for u in range(1, M):
                    grid_verts[u][0] = get_loop_vert(offset + u)
                    grid_verts[u][N] = get_loop_vert(offset + 2 * M + N - u)
                for v in range(1, N):
                    grid_verts[M][v] = get_loop_vert(offset + M + v)
                    grid_verts[0][v] = get_loop_vert(offset - v)
                    
                for u in range(1, M):
                    for v in range(1, N):
                        vert = bm.verts.new(grid_coords[u][v])
                        grid_verts[u][v] = vert
                        
                bm.verts.ensure_lookup_table()
                
                grid_layer = bm.faces.layers.int.get("tp_is_grid") or bm.faces.layers.int.new("tp_is_grid")
                max_id = max([f[grid_layer] for f in bm.faces] + [0])
                loop_id = max_id + 1
                
                faces_created = 0
                for u in range(M):
                    for v in range(N):
                        v00 = grid_verts[u][v]
                        v10 = grid_verts[u+1][v]
                        v11 = grid_verts[u+1][v+1]
                        v01 = grid_verts[u][v+1]
                        try:
                            f = bm.faces.new((v00, v10, v11, v01))
                            f[grid_layer] = loop_id
                            faces_created += 1
                        except Exception:
                            pass
                            
                loops_filled += 1
                total_faces += faces_created
                
            elif comp['type'] == 'non_linear_loops':
                # 执行非线性多区域填充
                faces_created, last_span, last_offset = fill_non_linear_loops(
                    bm, comp,
                    ref_obj=ref_obj,
                    topo_obj=topo_obj,
                    iterations=self.iterations,
                    smooth_factor=self.smooth_factor,
                    spring_factor=self.spring_factor,
                    selected_verts=selected_verts_extended,
                    selected_edges=selected_edges_extended,
                    user_span=self.span,
                    user_offset=self.offset
                )
                joined_filled += 1
                total_faces += faces_created
                if last_span >= 2:
                    last_optimal_span = last_span
                    last_optimal_offset = last_offset
                
        # 全局整体调优所有拼接的栅格，确保过渡平滑美观
        global_optimize_spliced_grids(
            bm, ref_obj, topo_obj,
            iterations=self.iterations,
            smooth_factor=self.smooth_factor,
            spring_factor=self.spring_factor
        )
        
        # 重新计算法线方向，确保显示正常（防止非包裹状态下因法线朝向问题显示为黑色）
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        
        # 更新 BMesh 并刷新视图
        bmesh.update_edit_mesh(topo_obj.data)
        
        # 如果是自动栅格填充模式，将计算得出的最优跨分与偏移回填写入场景属性中
        if self.span == 0 and last_optimal_span >= 2:
            global _in_grid_update
            _in_grid_update = True
            try:
                context.scene.tp_grid_span = last_optimal_span
                context.scene.tp_grid_offset = last_optimal_offset
            finally:
                _in_grid_update = False
        
        # 报告成功信息
        report_msg = "成功填充网格！"
        if loops_filled > 0:
            report_msg += f" 独立圈: {loops_filled}个"
        if joined_filled > 0:
            report_msg += f" 拼接圈: {joined_filled}个"
        report_msg += f"，共生成 {total_faces} 个面。"
        
        self.report({'INFO'}, report_msg)
        return {'FINISHED'}


class OBJECT_OT_tp_topology_remove_grid(bpy.types.Operator):
    bl_idname = "object.tp_topology_remove_grid"
    bl_label = "TP拓扑移除栅格"
    bl_description = "将已经栅格化的圈恢复到未栅格化之前的状态"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'
        
    def execute(self, context):
        topo_obj = context.active_object
        if not topo_obj or topo_obj.type != 'MESH':
            self.report({'ERROR'}, "活动对象不是网格体")
            return {'CANCELLED'}
            
        bm = bmesh.from_edit_mesh(topo_obj.data)
        
        # 获取 face layer
        grid_layer = bm.faces.layers.int.get("tp_is_grid")
        if not grid_layer:
            self.report({'WARNING'}, "未找到任何已栅格化的区域")
            return {'CANCELLED'}
            
        # 收集用户选择
        selected_verts = {v for v in bm.verts if v.select}
        selected_edges = {e for e in bm.edges if e.select}
        selected_faces = {f for f in bm.faces if f.select}
        
        has_selection = bool(selected_verts or selected_edges or selected_faces)
        
        # 找出所有已栅格化的面
        grid_faces = [f for f in bm.faces if f[grid_layer] > 0]
        if not grid_faces:
            self.report({'WARNING'}, "未找到任何已栅格化的区域")
            return {'CANCELLED'}
            
        # 收集要删除的 loop_ids
        IDs_del = set()
        
        if not has_selection:
            # 未选中任何元素，删除所有已栅格化的区域
            IDs_del = {f[grid_layer] for f in grid_faces if f[grid_layer] > 0}
        else:
            # 选中了元素，只删除与选择相交的区域
            for f in selected_faces:
                if f[grid_layer] > 0:
                    IDs_del.add(f[grid_layer])
            for e in selected_edges:
                for f in e.link_faces:
                    if f[grid_layer] > 0:
                        IDs_del.add(f[grid_layer])
            for v in selected_verts:
                for f in v.link_faces:
                    if f[grid_layer] > 0:
                        IDs_del.add(f[grid_layer])
                        
        if not IDs_del:
            self.report({'WARNING'}, "选中的元素不在任何已栅格化的区域上")
            return {'CANCELLED'}
            
        # 逐个 loop_id 进行分析和删除，以保证共享边界边的拓扑计算完全独立，不被误删
        total_faces_deleted = 0
        
        faces_to_delete = []
        edges_to_delete = []
        verts_to_delete = []
        
        for lid in IDs_del:
            F_loop = [f for f in bm.faces if f[grid_layer] == lid]
            if not F_loop:
                continue
                
            F_loop_set = set(F_loop)
            E_loop_all = set()
            for f in F_loop_set:
                E_loop_all.update(f.edges)
                
            # 独立计算该 loop_id 区域的边界边 and 边界点
            # 边界边是仅与该 loop_id 中的一个面相连的边
            E_boundary = {e for e in E_loop_all if len([f for f in e.link_faces if f[grid_layer] == lid]) == 1}
            V_boundary = {v for e in E_boundary for v in e.verts}
            
            E_internal = E_loop_all - E_boundary
            V_internal = {v for f in F_loop_set for v in f.verts} - V_boundary
            
            faces_to_delete.extend(F_loop)
            edges_to_delete.extend(E_internal)
            verts_to_delete.extend(V_internal)
            
            total_faces_deleted += len(F_loop)
            
        # 执行安全删除
        if faces_to_delete:
            faces_to_delete = [f for f in faces_to_delete if f.is_valid]
            bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES_ONLY')
            
        if edges_to_delete:
            edges_to_delete = [e for e in edges_to_delete if e.is_valid]
            bmesh.ops.delete(bm, geom=edges_to_delete, context='EDGES')
            
        if verts_to_delete:
            verts_to_delete = [v for v in verts_to_delete if v.is_valid]
            bmesh.ops.delete(bm, geom=verts_to_delete, context='VERTS')
            
        # 刷新网格和视图
        bmesh.update_edit_mesh(topo_obj.data)
        
        self.report({'INFO'}, f"已成功移除栅格，共删除 {total_faces_deleted} 个面。")
        return {'FINISHED'}


# --- Real-time Grid Micro-Adjustment from N-Panel ---
_in_grid_update = False

def on_grid_settings_update(self, context):
    global _in_grid_update
    if _in_grid_update:
        return
    _in_grid_update = True
    try:
        update_last_grid(context)
    finally:
        _in_grid_update = False

def update_last_grid(context):
    import bmesh
    import bpy
    from mathutils import Vector
    
    topo_obj = context.active_object
    if not topo_obj or topo_obj.type != 'MESH' or topo_obj.mode != 'EDIT':
        return
        
    bm = bmesh.from_edit_mesh(topo_obj.data)
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if not grid_layer:
        return
        
    # 获取所有的已填充的 loop ids
    ids = {f[grid_layer] for f in bm.faces if f[grid_layer] > 0}
    if not ids:
        return
        
    # 收集用户选择以决定对哪些区域进行参数调整 (参考 "移除栅格" 逻辑)
    selected_verts = {v for v in bm.verts if v.select}
    selected_edges = {e for e in bm.edges if e.select}
    selected_faces = {f for f in bm.faces if f.select}
    
    has_selection = bool(selected_verts or selected_edges or selected_faces)
    
    IDs_adjust = set()
    if not has_selection:
        # 如果未选择任何元素，调整全部已经栅格化的区域 (与"移除栅格"行为完全看齐)
        IDs_adjust.update(ids)
    else:
        # 如果有选择，仅调整与选择元素相交的栅格区域
        for f in selected_faces:
            if f[grid_layer] > 0:
                IDs_adjust.add(f[grid_layer])
        for e in selected_edges:
            for f in e.link_faces:
                if f[grid_layer] > 0:
                    IDs_adjust.add(f[grid_layer])
        for v in selected_verts:
            for f in v.link_faces:
                if f[grid_layer] > 0:
                    IDs_adjust.add(f[grid_layer])
                    
    if not IDs_adjust:
        return
        
    # 物理记忆：在网格被物理删除重建前，记录选中顶点信息以备后续恢复
    all_deleted_vert_indices = set()
    for lid in IDs_adjust:
        F_loop_temp = [f for f in bm.faces if f[grid_layer] == lid]
        if F_loop_temp:
            F_loop_set_temp = set(F_loop_temp)
            E_loop_all_temp = set()
            for f in F_loop_set_temp:
                E_loop_all_temp.update(f.edges)
            E_boundary_temp = {e for e in E_loop_all_temp if len([f for f in e.link_faces if f[grid_layer] == lid]) == 1}
            V_boundary_temp = {v for e in E_boundary_temp for v in e.verts}
            V_internal_temp = {v for f in F_loop_set_temp for v in f.verts} - V_boundary_temp
            for v in V_internal_temp:
                all_deleted_vert_indices.add(v.index)
                
    # 保留点（不会被删除）的选择状态：记录其顶点索引（Index）
    keep_selected_indices = [v.index for v in selected_verts if v.index not in all_deleted_vert_indices]
    
    # 物理删除点的选择状态：记录其三维局部坐标（Coordinates）
    deleted_selected_cos = [v.co.copy() for v in selected_verts if v.index in all_deleted_vert_indices]

    # Get span and offset from scene
    scene = context.scene
    user_span = scene.tp_grid_span
    user_offset = scene.tp_grid_offset
    
    # Find high-poly reference object
    ref_obj = None
    ref_obj_name = context.window_manager.tp_ref_object_name
    if ref_obj_name:
        ref_obj = bpy.data.objects.get(ref_obj_name)
        
    # 对所有匹配的栅格区域依次执行擦除与重建
    for lid in IDs_adjust:
        # Gather all faces with this loop_id
        F_loop = [f for f in bm.faces if f[grid_layer] == lid]
        if not F_loop:
            continue
            
        F_loop_set = set(F_loop)
        E_loop_all = set()
        for f in F_loop_set:
            E_loop_all.update(f.edges)
            
        E_boundary = {e for e in E_loop_all if len([f for f in e.link_faces if f[grid_layer] == lid]) == 1}
        V_boundary = {v for e in E_boundary for v in e.verts}
        
        E_internal = E_loop_all - E_boundary
        V_internal = {v for f in F_loop_set for v in f.verts} - V_boundary
        
        # Delete internal elements
        faces_to_delete = [f for f in F_loop if f.is_valid]
        if faces_to_delete:
            bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES_ONLY')
        edges_to_delete = [e for e in E_internal if e.is_valid]
        if edges_to_delete:
            bmesh.ops.delete(bm, geom=edges_to_delete, context='EDGES')
        verts_to_delete = [v for v in V_internal if v.is_valid]
        if verts_to_delete:
            bmesh.ops.delete(bm, geom=verts_to_delete, context='VERTS')
            
        # Trace the boundary loop to get ordered loop_verts
        loop_verts = trace_cycle_verts(E_boundary)
        if not loop_verts:
            continue
            
        # Calculate best parameters
        L = len(loop_verts)
        best_params = find_best_corners_3d(loop_verts, ref_obj=ref_obj, topo_obj=topo_obj)
        if not best_params:
            continue
            
        M, N, offset = best_params
        
        if user_span >= 2:
            half_L = L // 2
            N = max(2, min(half_L - 2, user_span))
            M = half_L - N
            
        offset = (offset + user_offset) % L
        
        # Initialize and optimize grid
        grid_coords = init_coons_grid(loop_verts, M, N, offset)
        optimize_grid(
            grid_coords, M, N,
            ref_obj=ref_obj,
            topo_obj=topo_obj,
            iterations=40,
            smooth_factor=0.4,
            spring_factor=0.3
        )
        
        # Create new vertices and faces
        def get_loop_vert(index):
            return loop_verts[index % L]
            
        grid_verts = [[None for _ in range(N + 1)] for _ in range(M + 1)]
        grid_verts[0][0] = get_loop_vert(offset)
        grid_verts[M][0] = get_loop_vert(offset + M)
        grid_verts[M][N] = get_loop_vert(offset + M + N)
        grid_verts[0][N] = get_loop_vert(offset + 2 * M + N)
        
        for u in range(1, M):
            grid_verts[u][0] = get_loop_vert(offset + u)
            grid_verts[u][N] = get_loop_vert(offset + 2 * M + N - u)
        for v in range(1, N):
            grid_verts[M][v] = get_loop_vert(offset + M + v)
            grid_verts[0][v] = get_loop_vert(offset - v)
            
        for u in range(1, M):
            for v in range(1, N):
                vert = bm.verts.new(grid_coords[u][v])
                grid_verts[u][v] = vert
                
        bm.verts.ensure_lookup_table()
        
        # Re-apply the same loop_id
        for u in range(M):
            for v in range(N):
                v00 = grid_verts[u][v]
                v10 = grid_verts[u+1][v]
                v11 = grid_verts[u+1][v+1]
                v01 = grid_verts[u][v+1]
                try:
                    f = bm.faces.new((v00, v10, v11, v01))
                    f[grid_layer] = lid
                except Exception:
                    pass
                    
        # Recalculate normals for new faces
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        
    # 全局整体调优所有拼接的栅格，确保过渡平滑美观
    global_optimize_spliced_grids(
        bm, ref_obj, topo_obj,
        iterations=40,
        smooth_factor=0.4,
        spring_factor=0.3
    )
    
    # === 物理记忆与投影追踪恢复 ===
    bm.verts.ensure_lookup_table()
    
    # 1. 恢复保留点的选中状态
    for idx in keep_selected_indices:
        if idx < len(bm.verts):
            bm.verts[idx].select = True
            
    # 2. 针对被物理删除点，通过最近邻算法在重建后的新网格中寻找最佳替代点
    for old_co in deleted_selected_cos:
        closest_v = None
        min_dist = float('inf')
        for v in bm.verts:
            dist = (v.co - old_co).length
            if dist < min_dist:
                min_dist = dist
                closest_v = v
        if closest_v and min_dist < 0.2:
            closest_v.select = True
            
    # 刷新选择历史，确保活动项正确
    selected_verts_after = [v for v in bm.verts if v.select]
    if selected_verts_after:
        bm.select_history.clear()
        bm.select_history.add(selected_verts_after[-1])

    # Update edit mesh and viewport
    bmesh.update_edit_mesh(topo_obj.data)
    context.area.tag_redraw()
