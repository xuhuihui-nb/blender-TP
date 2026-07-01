import bpy
import bmesh
import gpu
import blf
import math
import mathutils
from gpu_extras.batch import batch_for_shader
from bpy_extras.view3d_utils import (
    location_3d_to_region_2d
)

def get_shader():
    try:
        return gpu.shader.from_builtin('UNIFORM_COLOR')
    except Exception:
        try:
            return gpu.shader.from_builtin('3D_UNIFORM_COLOR')
        except Exception:
            return None

def get_shader_points():
    try:
        return gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    except Exception:
        try:
            return gpu.shader.from_builtin('UNIFORM_COLOR')
        except Exception:
            try:
                return gpu.shader.from_builtin('3D_UNIFORM_COLOR')
            except Exception:
                return None

def get_shader_2d():
    try:
        return gpu.shader.from_builtin('2D_UNIFORM_COLOR')
    except Exception:
        try:
            return gpu.shader.from_builtin('UNIFORM_COLOR')
        except Exception:
            return None

_polyline_shader = None
_is_polyline = False
_polyline_initialized = False

def get_polyline_info():
    global _polyline_shader, _is_polyline, _polyline_initialized
    if not _polyline_initialized:
        _polyline_initialized = True
        try:
            _polyline_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            _is_polyline = True
        except Exception:
            try:
                _polyline_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                _is_polyline = False
            except Exception:
                try:
                    _polyline_shader = gpu.shader.from_builtin('3D_UNIFORM_COLOR')
                    _is_polyline = False
                except Exception:
                    _polyline_shader = None
                    _is_polyline = False
    return _polyline_shader, _is_polyline

def draw_lines_smooth(coords, color, width, shader, is_polyline=False, draw_type='LINES'):
    if not coords or shader is None:
        return
    
    shader.bind()
    
    try:
        gpu.state.blend_set('ALPHA')
    except Exception:
        pass
        
    if is_polyline:
        try:
            shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
            shader.uniform_float("lineWidth", width)
            shader.uniform_float("color", color)
        except Exception:
            pass
    else:
        try:
            shader.uniform_float("color", color)
        except Exception:
            pass
        try:
            gpu.state.line_width_set(width)
        except Exception:
            pass
            
    try:
        batch = batch_for_shader(shader, draw_type, {"pos": coords})
        batch.draw(shader)
    except Exception as e:
        print(f"Error drawing smooth lines ({draw_type}):", e)
        
    try:
        gpu.state.blend_set('NONE')
    except Exception:
        pass

def draw_smooth_circle_2d(cx, cy, radius, color, shader2d, poly_shader=None, is_poly=False):
    # 1. Draw the filled inner part of the circle
    inner_radius = max(0.1, radius - 1.0)
    num_segments = 32
    coords = []
    for i in range(num_segments):
        theta1 = 2.0 * math.pi * i / num_segments
        theta2 = 2.0 * math.pi * (i + 1) / num_segments
        coords.append((cx, cy))
        coords.append((cx + inner_radius * math.cos(theta1), cy + inner_radius * math.sin(theta1)))
        coords.append((cx + inner_radius * math.cos(theta2), cy + inner_radius * math.sin(theta2)))
    
    if shader2d:
        shader2d.bind()
        shader2d.uniform_float("color", color)
        try:
            batch_circle = batch_for_shader(shader2d, 'TRIS', {"pos": coords})
            batch_circle.draw(shader2d)
        except Exception:
            pass

    # 2. Draw the smooth outline circle to anti-alias the edge
    if poly_shader:
        border_coords = []
        for i in range(num_segments + 1):
            theta = 2.0 * math.pi * i / num_segments
            border_coords.append((cx + (radius - 0.5) * math.cos(theta), cy + (radius - 0.5) * math.sin(theta), 0.0))
        
        draw_lines_smooth(border_coords, color, 1.5, poly_shader, is_poly, 'LINE_STRIP')

_diag_logged = False

def get_seam_target_edges_local(bm):
    target_edges = set()
    # 1. Wire edges (edges with no faces)
    for e in bm.edges:
        if len(e.link_faces) == 0:
            target_edges.add(e)
            
    # 2. Rasterized loop boundaries
    grid_layer = bm.faces.layers.int.get("tp_is_grid")
    if grid_layer:
        faces_by_lid = {}
        for f in bm.faces:
            lid = f[grid_layer]
            if lid > 0:
                faces_by_lid.setdefault(lid, []).append(f)
                
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

def get_mirrored_coords(co, context, topo_obj):
    scene = context.scene
    if not getattr(scene, "tp_symmetry_mode", False):
        return []
        
    use_x = getattr(scene, "tp_symmetry_x", True)
    use_y = getattr(scene, "tp_symmetry_y", False)
    use_z = getattr(scene, "tp_symmetry_z", False)
    
    if not (use_x or use_y or use_z):
        return []
        
    ref_obj_name = context.window_manager.tp_ref_object_name
    ref_obj = bpy.data.objects.get(ref_obj_name)
    mirror_obj = ref_obj if ref_obj else topo_obj
    
    if not mirror_obj:
        return []
        
    mat = mirror_obj.matrix_world
    try:
        mat_inv = mat.inverted()
    except Exception:
        return []
        
    local_co = mat_inv @ co
    
    xs = [local_co.x]
    if use_x:
        xs.append(-local_co.x)
        
    ys = [local_co.y]
    if use_y:
        ys.append(-local_co.y)
        
    zs = [local_co.z]
    if use_z:
        zs.append(-local_co.z)
        
    mirrored_coords = []
    for x in xs:
        for y in ys:
            for z in zs:
                mc = mathutils.Vector((x, y, z))
                if (mc - local_co).length > 1e-5:
                    mirrored_coords.append(mat @ mc)
                    
    return mirrored_coords


def offset_towards_camera(co, context, offset_amount=0.0025):
    try:
        rv3d = context.space_data.region_3d
        cam_matrix = rv3d.view_matrix.inverted()
        is_persp = (rv3d.view_perspective == 'PERSP')
        if is_persp:
            cam_pos = cam_matrix.to_translation()
            direction = cam_pos - co
            dist = direction.length
            if dist > 1e-4:
                shift_dist = min(0.08, max(0.001, dist * 0.0018))
                return co + direction.normalized() * shift_dist
        else:
            view_dir = cam_matrix.to_3x3() @ mathutils.Vector((0.0, 0.0, 1.0))
            return co + view_dir.normalized() * offset_amount
    except Exception:
        pass
    return co


def get_mirrored_edges(edges, context, topo_obj):
    scene = context.scene
    if not getattr(scene, "tp_symmetry_mode", False):
        return []
        
    use_x = getattr(scene, "tp_symmetry_x", True)
    use_y = getattr(scene, "tp_symmetry_y", False)
    use_z = getattr(scene, "tp_symmetry_z", False)
    
    if not (use_x or use_y or use_z):
        return []
        
    ref_obj_name = context.window_manager.tp_ref_object_name
    ref_obj = bpy.data.objects.get(ref_obj_name)
    mirror_obj = ref_obj if ref_obj else topo_obj
    if not mirror_obj:
        return []
        
    mat = mirror_obj.matrix_world
    try:
        mat_inv = mat.inverted()
    except Exception:
        return []
        
    ops = []
    if use_x:
        ops.append((-1.0, 1.0, 1.0))
    if use_y:
        ops.append((1.0, -1.0, 1.0))
    if use_z:
        ops.append((1.0, 1.0, -1.0))
    if use_x and use_y:
        ops.append((-1.0, -1.0, 1.0))
    if use_x and use_z:
        ops.append((-1.0, 1.0, -1.0))
    if use_y and use_z:
        ops.append((1.0, -1.0, -1.0))
    if use_x and use_y and use_z:
        ops.append((-1.0, -1.0, -1.0))
        
    mirrored = []
    for p1, p2 in edges:
        lp1 = mat_inv @ p1
        lp2 = mat_inv @ p2
        for sx, sy, sz in ops:
            mp1 = mat @ mathutils.Vector((lp1.x * sx, lp1.y * sy, lp1.z * sz))
            mp2 = mat @ mathutils.Vector((lp2.x * sx, lp2.y * sy, lp2.z * sz))
            mirrored.append((mp1, mp2))
            
    return mirrored


def is_point_in_mask(co, context):
    scene = context.scene
    if not getattr(scene, "tp_symmetry_mode", False):
        return False
        
    use_x = getattr(scene, "tp_symmetry_x", True)
    use_y = getattr(scene, "tp_symmetry_y", False)
    use_z = getattr(scene, "tp_symmetry_z", False)
    
    if not (use_x or use_y or use_z):
        return False
        
    ref_obj_name = context.window_manager.tp_ref_object_name
    ref_obj = bpy.data.objects.get(ref_obj_name)
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    mirror_obj = ref_obj if ref_obj else topo_obj
    if not mirror_obj:
        return False
        
    try:
        mat_inv = mirror_obj.matrix_world.inverted()
        local_co = mat_inv @ co
        
        # We use a threshold of 1e-4 / -1e-4 to allow vertices on the symmetry planes
        if use_x and local_co.x < -1e-4:
            return True
        if use_y and local_co.y > 1e-4:
            return True
        if use_z and local_co.z < -1e-4:
            return True
    except Exception:
        pass
    return False


def clamp_to_mask_boundary(co, context):
    scene = context.scene
    if not getattr(scene, "tp_symmetry_mode", False):
        return co
        
    use_x = getattr(scene, "tp_symmetry_x", True)
    use_y = getattr(scene, "tp_symmetry_y", False)
    use_z = getattr(scene, "tp_symmetry_z", False)
    
    if not (use_x or use_y or use_z):
        return co
        
    ref_obj_name = context.window_manager.tp_ref_object_name
    ref_obj = bpy.data.objects.get(ref_obj_name)
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    mirror_obj = ref_obj if ref_obj else topo_obj
    if not mirror_obj:
        return co
        
    try:
        mat_world = mirror_obj.matrix_world
        mat_inv = mat_world.inverted()
        local_co = mat_inv @ co
        
        clamped = False
        if use_x and local_co.x < 0.0:
            local_co.x = 0.0
            clamped = True
        if use_y and local_co.y > 0.0:
            local_co.y = 0.0
            clamped = True
        if use_z and local_co.z < 0.0:
            local_co.z = 0.0
            clamped = True
            
        if clamped:
            return mat_world @ local_co
    except Exception:
        pass
    return co




def add_custom_quad(x1, x2, y1, y2, z1, z2, face_name, matrix_world, faces, edges):
    if face_name == 'LEFT':
        p0 = matrix_world @ mathutils.Vector((x1, y2, z1))
        p1 = matrix_world @ mathutils.Vector((x1, y1, z1))
        p2 = matrix_world @ mathutils.Vector((x1, y1, z2))
        p3 = matrix_world @ mathutils.Vector((x1, y2, z2))
    elif face_name == 'RIGHT':
        p0 = matrix_world @ mathutils.Vector((x2, y1, z1))
        p1 = matrix_world @ mathutils.Vector((x2, y2, z1))
        p2 = matrix_world @ mathutils.Vector((x2, y2, z2))
        p3 = matrix_world @ mathutils.Vector((x2, y1, z2))
    elif face_name == 'BOTTOM':
        p0 = matrix_world @ mathutils.Vector((x1, y1, z1))
        p1 = matrix_world @ mathutils.Vector((x2, y1, z1))
        p2 = matrix_world @ mathutils.Vector((x2, y1, z2))
        p3 = matrix_world @ mathutils.Vector((x1, y1, z2))
    elif face_name == 'TOP':
        p0 = matrix_world @ mathutils.Vector((x2, y2, z1))
        p1 = matrix_world @ mathutils.Vector((x1, y2, z1))
        p2 = matrix_world @ mathutils.Vector((x1, y2, z2))
        p3 = matrix_world @ mathutils.Vector((x2, y2, z2))
    elif face_name == 'BACK':
        p0 = matrix_world @ mathutils.Vector((x1, y2, z1))
        p1 = matrix_world @ mathutils.Vector((x2, y2, z1))
        p2 = matrix_world @ mathutils.Vector((x2, y1, z1))
        p3 = matrix_world @ mathutils.Vector((x1, y1, z1))
    elif face_name == 'FRONT':
        p0 = matrix_world @ mathutils.Vector((x1, y1, z2))
        p1 = matrix_world @ mathutils.Vector((x2, y1, z2))
        p2 = matrix_world @ mathutils.Vector((x2, y2, z2))
        p3 = matrix_world @ mathutils.Vector((x1, y2, z2))
        
    faces.extend((p0, p1, p2, p0, p2, p3))
    edges.extend((p0, p1, p1, p2, p2, p3, p3, p0))

def draw_symmetry_masks(self, context):
    scene = context.scene
    if not getattr(scene, "tp_symmetry_mode", False):
        return
        
    use_x = getattr(scene, "tp_symmetry_x", True)
    use_y = getattr(scene, "tp_symmetry_y", False)
    use_z = getattr(scene, "tp_symmetry_z", False)
    
    if not (use_x or use_y or use_z):
        return
        
    ref_obj_name = context.window_manager.tp_ref_object_name
    ref_obj = bpy.data.objects.get(ref_obj_name)
    if not ref_obj:
        return
        
    try:
        bbox = [mathutils.Vector(corner) for corner in ref_obj.bound_box]
    except Exception:
        return
        
    min_local = mathutils.Vector((min(v.x for v in bbox), min(v.y for v in bbox), min(v.z for v in bbox)))
    max_local = mathutils.Vector((max(v.x for v in bbox), max(v.y for v in bbox), max(v.z for v in bbox)))
    
    # Scale outward by 10% relative to the origin (keeps symmetry planes anchored at 0.0)
    x_min, x_max = min_local.x * 1.1, max_local.x * 1.1
    y_min, y_max = min_local.y * 1.1, max_local.y * 1.1
    z_min, z_max = min_local.z * 1.1, max_local.z * 1.1
    
    shader_3d = get_shader()
    if not shader_3d:
        return
        
    orig_depth_test = 'NONE'
    try:
        orig_depth_test = gpu.state.depth_test_get()
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL') # Depth test enabled so it is occluded by the reference mesh!
    except Exception:
        pass
        
    face_color = (0.0, 0.4, 1.0, 0.15)
    line_color = (0.0, 0.5, 1.0, 0.4)
    
    matrix_world = ref_obj.matrix_world
    
    # Collect all faces ranges
    boxes_ranges = []
    
    if use_x and not use_y and not use_z:
        boxes_ranges.append((x_min, 0.0, y_min, y_max, z_min, z_max, ('LEFT', 'RIGHT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
    elif not use_x and use_y and not use_z:
        boxes_ranges.append((x_min, x_max, 0.0, y_max, z_min, z_max, ('LEFT', 'RIGHT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
    elif not use_x and not use_y and use_z:
        boxes_ranges.append((x_min, x_max, y_min, y_max, z_min, 0.0, ('LEFT', 'RIGHT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
    elif use_x and use_y and not use_z:
        boxes_ranges.append((x_min, 0.0, y_min, y_max, z_min, z_max, ('LEFT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
        boxes_ranges.append((0.0, 0.0, y_min, 0.0, z_min, z_max, ('RIGHT',)))
        boxes_ranges.append((0.0, x_max, 0.0, y_max, z_min, z_max, ('RIGHT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
    elif use_x and not use_y and use_z:
        boxes_ranges.append((x_min, 0.0, y_min, y_max, z_min, z_max, ('LEFT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
        boxes_ranges.append((0.0, 0.0, y_min, y_max, 0.0, z_max, ('RIGHT',)))
        boxes_ranges.append((0.0, x_max, y_min, y_max, z_min, 0.0, ('RIGHT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
    elif not use_x and use_y and use_z:
        boxes_ranges.append((x_min, x_max, y_min, y_max, z_min, 0.0, ('LEFT', 'RIGHT', 'BOTTOM', 'TOP', 'BACK')))
        boxes_ranges.append((x_min, x_max, y_min, 0.0, 0.0, 0.0, ('FRONT',)))
        boxes_ranges.append((x_min, x_max, 0.0, y_max, 0.0, z_max, ('LEFT', 'RIGHT', 'BOTTOM', 'TOP', 'FRONT')))
    elif use_x and use_y and use_z:
        boxes_ranges.append((x_min, 0.0, y_min, y_max, z_min, z_max, ('LEFT', 'BOTTOM', 'TOP', 'BACK', 'FRONT')))
        boxes_ranges.append((0.0, 0.0, y_min, 0.0, 0.0, z_max, ('RIGHT',)))
        boxes_ranges.append((0.0, x_max, 0.0, y_max, z_min, z_max, ('RIGHT', 'TOP', 'BACK', 'FRONT')))
        boxes_ranges.append((0.0, x_max, 0.0, 0.0, 0.0, z_max, ('BOTTOM',)))
        boxes_ranges.append((0.0, x_max, y_min, 0.0, z_min, 0.0, ('RIGHT', 'BOTTOM', 'BACK', 'FRONT')))
        
    combined_faces = []
    combined_edges = []
    
    for xr_min, xr_max, yr_min, yr_max, zr_min, zr_max, faces_list in boxes_ranges:
        for f in faces_list:
            add_custom_quad(xr_min, xr_max, yr_min, yr_max, zr_min, zr_max, f, matrix_world, combined_faces, combined_edges)
                
    # Remove duplicate wireframe lines to keep them perfectly clean
    unique_edges = []
    seen = set()
    for i in range(0, len(combined_edges), 2):
        p1, p2 = combined_edges[i], combined_edges[i+1]
        key = frozenset((
            (round(p1[0], 4), round(p1[1], 4), round(p1[2], 4)),
            (round(p2[0], 4), round(p2[1], 4), round(p2[2], 4))
        ))
        if key not in seen:
            seen.add(key)
            unique_edges.extend((p1, p2))
            
    if combined_faces:
        shader_3d.bind()
        shader_3d.uniform_float("color", face_color)
        try:
            batch = batch_for_shader(shader_3d, 'TRIS', {"pos": combined_faces})
            batch.draw(shader_3d)
        except Exception:
            pass
            
    if unique_edges:
        shader_3d.bind()
        shader_3d.uniform_float("color", line_color)
        try:
            gpu.state.line_width_set(1.5)
            batch = batch_for_shader(shader_3d, 'LINES', {"pos": unique_edges})
            batch.draw(shader_3d)
        except Exception:
            pass
            
    try:
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set(orig_depth_test)
    except Exception:
        pass


def draw_mirrored_mesh_tints_3d(self, context):
    topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
    if not (topo_obj and topo_obj.type == 'MESH' and getattr(context.scene, "tp_symmetry_mode", False)):
        return
        
    is_edit = (topo_obj.mode == 'EDIT')
    if not is_edit:
        return
        
    try:
        bm = bmesh.from_edit_mesh(topo_obj.data)
        shader_3d = get_shader()
        if not shader_3d:
            return
            
        face_color_tint = (0.0, 0.4, 1.0, 0.25)  # semi-transparent blue
        line_color_tint = (0.0, 0.5, 1.0, 0.5)   # blue outline
        
        ref_obj_name = context.window_manager.tp_ref_object_name
        ref_obj = bpy.data.objects.get(ref_obj_name)
        mirror_obj = ref_obj if ref_obj else topo_obj
        matrix_world = mirror_obj.matrix_world
        matrix_world_topo = topo_obj.matrix_world
        
        mat_inv = matrix_world.inverted()
        
        use_x = getattr(context.scene, "tp_symmetry_x", True)
        use_y = getattr(context.scene, "tp_symmetry_y", False)
        use_z = getattr(context.scene, "tp_symmetry_z", False)
        
        # Determine active local symmetry scaling vectors
        ops = []
        if use_x:
            ops.append((-1.0, 1.0, 1.0))
        if use_y:
            ops.append((1.0, -1.0, 1.0))
        if use_z:
            ops.append((1.0, 1.0, -1.0))
        if use_x and use_y:
            ops.append((-1.0, -1.0, 1.0))
        if use_x and use_z:
            ops.append((-1.0, 1.0, -1.0))
        if use_y and use_z:
            ops.append((1.0, -1.0, -1.0))
        if use_x and use_y and use_z:
            ops.append((-1.0, -1.0, -1.0))
            
        combined_faces = []
        combined_edges = []
        
        for face in bm.faces:
            if len(face.verts) < 3:
                continue
            world_cos = [matrix_world_topo @ v.co for v in face.verts]
            local_cos = [mat_inv @ co for co in world_cos]
            
            for sx, sy, sz in ops:
                m_world_cos = []
                for co in local_cos:
                    m_world_cos.append(matrix_world @ mathutils.Vector((co.x * sx, co.y * sy, co.z * sz)))
                    
                # Convert to triangles for drawing
                if len(m_world_cos) == 4:
                    combined_faces.extend([
                        m_world_cos[0], m_world_cos[1], m_world_cos[2],
                        m_world_cos[0], m_world_cos[2], m_world_cos[3]
                    ])
                elif len(m_world_cos) == 3:
                    combined_faces.extend(m_world_cos)
                    
                # Outlines
                for i in range(len(m_world_cos)):
                    combined_edges.append(m_world_cos[i])
                    combined_edges.append(m_world_cos[(i + 1) % len(m_world_cos)])
                    
        orig_depth_test = 'NONE'
        try:
            orig_depth_test = gpu.state.depth_test_get()
            gpu.state.blend_set('ALPHA')
            # Follow show_in_front to dynamically toggle depth testing
            if topo_obj.show_in_front:
                gpu.state.depth_test_set('NONE')
            else:
                gpu.state.depth_test_set('LESS_EQUAL')
        except Exception:
            pass
            
        if combined_faces:
            shader_3d.bind()
            shader_3d.uniform_float("color", face_color_tint)
            try:
                batch = batch_for_shader(shader_3d, 'TRIS', {"pos": combined_faces})
                batch.draw(shader_3d)
            except Exception:
                pass
                
        if combined_edges:
            shader_3d.bind()
            shader_3d.uniform_float("color", line_color_tint)
            try:
                gpu.state.line_width_set(2.0)
                batch = batch_for_shader(shader_3d, 'LINES', {"pos": combined_edges})
                batch.draw(shader_3d)
            except Exception:
                pass
                
        try:
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set(orig_depth_test)
        except Exception:
            pass
    except Exception as e:
        print("Error drawing 3D mirrored mesh tints:", e)


def draw_callback(self, context):
    # Draw symmetry masks in POST_VIEW pass with depth testing LESS_EQUAL
    draw_symmetry_masks(self, context)
    # Draw 3D mirrored mesh tints (respecting depth testing based on show_in_front)
    draw_mirrored_mesh_tints_3d(self, context)

    # 1. Draw the active stroke line strip in real-time using resampled points
    if self.stroke_points and len(self.stroke_points) >= 2:
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
                        
        resampled_pts, resampled_snaps = self.resample_stroke_segments(context, self.stroke_points, self.stroke_snap_indices, is_closed)
        
        self._last_resampled_pts = resampled_pts
        poly_shader, is_poly = get_polyline_info()
        if poly_shader:
            coords = [tuple(p) for p in resampled_pts]
            draw_lines_smooth(coords, (1.0, 0.6, 0.0, 1.0), 3.0, poly_shader, is_poly, 'LINE_STRIP')
            
            # Draw mirrored active strokes
            topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
            if topo_obj and getattr(context.scene, "tp_symmetry_mode", False):
                mirrored_strips = []
                for p in resampled_pts:
                    m_pts = get_mirrored_coords(p, context, topo_obj)
                    if not mirrored_strips:
                        mirrored_strips = [[] for _ in m_pts]
                    for i, mp in enumerate(m_pts):
                        mirrored_strips[i].append(tuple(mp))
                
                for strip in mirrored_strips:
                    draw_lines_smooth(strip, (1.0, 0.6, 0.0, 0.7), 3.0, poly_shader, is_poly, 'LINE_STRIP')

    # 2. Draw rubber-band preview line for polyline mode
    if not self.is_dragging and self.stroke_points and self.is_polyline:
        preview_pt = self.hover_snap_pt
        if not preview_pt:
            coord = (self.last_mouse_coord[0], self.last_mouse_coord[1])
            preview_pt = self.get_surface_point(context, coord)
            
        if preview_pt:
            poly_shader, is_poly = get_polyline_info()
            if poly_shader:
                coords = [tuple(self.stroke_points[-1]), tuple(preview_pt)]
                draw_lines_smooth(coords, (1.0, 1.0, 1.0, 0.6), 2.0, poly_shader, is_poly, 'LINE_STRIP')
                
                topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
                if topo_obj and getattr(context.scene, "tp_symmetry_mode", False):
                    m_p1 = get_mirrored_coords(self.stroke_points[-1], context, topo_obj)
                    m_p2 = get_mirrored_coords(preview_pt, context, topo_obj)
                    for mp1, mp2 in zip(m_p1, m_p2):
                        m_coords = [tuple(mp1), tuple(mp2)]
                        draw_lines_smooth(m_coords, (1.0, 1.0, 1.0, 0.4), 2.0, poly_shader, is_poly, 'LINE_STRIP')

    # 3. Draw boundary edges overlay in 3D in boundary mode
    if getattr(context.scene, "tp_boundary_mode", True):
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if topo_obj and topo_obj.type == 'MESH':
            is_grabbing = getattr(self, 'is_grabbing', False)
            grab_weights = getattr(self, 'grab_weights', {}) if is_grabbing else {}

            # Each entry: (p1, p2, weight)  weight=-1 means "selected/active orange"
            unselected_edges = []  # (p1, p2)
            selected_edges   = []  # (p1, p2)
            # During grab: (p1, p2, alpha) for influenced white→orange edges
            influenced_edges = []  # (p1, p2, alpha)

            matrix_world = topo_obj.matrix_world
            is_edit = (topo_obj.mode == 'EDIT')
            if is_edit:
                try:
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    target_edges = get_seam_target_edges_local(bm)
                    for e in target_edges:
                        p1 = matrix_world @ e.verts[0].co
                        p2 = matrix_world @ e.verts[1].co
                        is_sel = e.select
                        if is_sel:
                            selected_edges.append((p1, p2))
                        else:
                            unselected_edges.append((p1, p2))
                            if is_grabbing and grab_weights:
                                w0 = grab_weights.get(e.verts[0].index, 0.0)
                                w1 = grab_weights.get(e.verts[1].index, 0.0)
                                w = max(w0, w1)
                                if w > 0.0:
                                    influenced_edges.append((p1, p2, w))
                except Exception as e:
                    print("Error getting edit mesh boundary edges:", e)
            else:
                try:
                    mesh = topo_obj.data
                    bm = bmesh.new()
                    bm.from_mesh(mesh)
                    target_edges = get_seam_target_edges_local(bm)
                    for e in target_edges:
                        p1 = matrix_world @ e.verts[0].co
                        p2 = matrix_world @ e.verts[1].co
                        is_sel = e.select
                        if is_sel:
                            selected_edges.append((p1, p2))
                        else:
                            unselected_edges.append((p1, p2))
                            if is_grabbing and grab_weights:
                                w0 = grab_weights.get(e.verts[0].index, 0.0)
                                w1 = grab_weights.get(e.verts[1].index, 0.0)
                                w = max(w0, w1)
                                if w > 0.0:
                                    influenced_edges.append((p1, p2, w))
                    bm.free()
                except Exception as e:
                    print("Error getting mesh boundary edges:", e)

            if getattr(context.scene, "tp_symmetry_mode", False):
                # Mirror unselected edges
                if unselected_edges:
                    m_unselected = get_mirrored_edges(unselected_edges, context, topo_obj)
                    unselected_edges.extend(m_unselected)
                
                # Mirror selected edges
                if selected_edges:
                    m_selected = get_mirrored_edges(selected_edges, context, topo_obj)
                    selected_edges.extend(m_selected)
                
                # Mirror influenced edges
                if influenced_edges:
                    m_influenced = []
                    edges_to_mirror = [(p1, p2) for p1, p2, w in influenced_edges]
                    mirrored_pairs = get_mirrored_edges(edges_to_mirror, context, topo_obj)
                    ops_count = len(mirrored_pairs) // len(influenced_edges) if influenced_edges else 0
                    idx = 0
                    for p1, p2, w in influenced_edges:
                        for _ in range(ops_count):
                            mp1, mp2 = mirrored_pairs[idx]
                            m_influenced.append((mp1, mp2, w))
                            idx += 1
                    influenced_edges.extend(m_influenced)

            poly_shader, is_poly = get_polyline_info()
            if poly_shader and (unselected_edges or selected_edges or influenced_edges):
                orig_depth_test = 'NONE'
                try:
                    orig_depth_test = gpu.state.depth_test_get()
                    if topo_obj.show_in_front:
                        gpu.state.depth_test_set('NONE')
                    else:
                        gpu.state.depth_test_set('LESS_EQUAL')
                except Exception:
                    pass

                # 1. Draw unaffected boundary edges (white)
                if unselected_edges:
                    coords = []
                    for p1, p2 in unselected_edges:
                        coords.append(tuple(offset_towards_camera(p1, context)))
                        coords.append(tuple(offset_towards_camera(p2, context)))
                    draw_lines_smooth(coords, (1.0, 1.0, 1.0, 1.0), 4.0, poly_shader, is_poly, 'LINES')

                # 2. Draw selected boundary edges (orange, fully opaque)
                if selected_edges:
                    coords = []
                    for p1, p2 in selected_edges:
                        coords.append(tuple(offset_towards_camera(p1, context)))
                        coords.append(tuple(offset_towards_camera(p2, context)))
                    draw_lines_smooth(coords, (1.0, 0.6, 0.0, 1.0), 4.0, poly_shader, is_poly, 'LINES')

                # 3. During grab: draw influence-weighted orange edges
                if influenced_edges:
                    from collections import defaultdict
                    buckets = defaultdict(list)
                    for p1, p2, w in influenced_edges:
                        bucket = round(w * 16) / 16
                        buckets[bucket].append((p1, p2))
                    for alpha, pairs in buckets.items():
                        coords = []
                        for p1, p2 in pairs:
                            coords.append(tuple(offset_towards_camera(p1, context)))
                            coords.append(tuple(offset_towards_camera(p2, context)))
                        draw_lines_smooth(coords, (1.0, 0.6, 0.0, alpha), 4.0, poly_shader, is_poly, 'LINES')

                # Restore original depth test state
                try:
                    gpu.state.depth_test_set(orig_depth_test)
                except Exception:
                    pass


    # 4. Draw pinned boundary vertices/edges overlay
    # Draw it always to ensure pinned edges stay visible even when the selection changes
    if True:
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if topo_obj and topo_obj.type == 'MESH':
            pinned_coords = []
            pinned_edges = []
            matrix_world = topo_obj.matrix_world
            is_edit = (topo_obj.mode == 'EDIT')
            
            if is_edit:
                try:
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    pin_layer = bm.verts.layers.int.get("tp_is_pinned")
                    if pin_layer:
                        for v in bm.verts:
                            if v[pin_layer] == 1:
                                pinned_coords.append(matrix_world @ v.co)
                        for e in bm.edges:
                            if e.verts[0][pin_layer] == 1 and e.verts[1][pin_layer] == 1:
                                pinned_edges.append((matrix_world @ e.verts[0].co, matrix_world @ e.verts[1].co))
                except Exception:
                    pass
            else:
                mesh = topo_obj.data
                pin_attr = mesh.attributes.get("tp_is_pinned")
                if pin_attr:
                    for i, v in enumerate(mesh.vertices):
                        if pin_attr.data[i].value == 1:
                            pinned_coords.append(matrix_world @ v.co)
                    for e in mesh.edges:
                        if pin_attr.data[e.vertices[0]].value == 1 and pin_attr.data[e.vertices[1]].value == 1:
                            pinned_edges.append((matrix_world @ mesh.vertices[e.vertices[0]].co, matrix_world @ mesh.vertices[e.vertices[1]].co))
            
            if getattr(context.scene, "tp_symmetry_mode", False):
                m_pinned_coords = []
                for p in pinned_coords:
                    m_pinned_coords.extend(get_mirrored_coords(p, context, topo_obj))
                pinned_coords.extend(m_pinned_coords)
                
                if pinned_edges:
                    m_pinned_edges = get_mirrored_edges(pinned_edges, context, topo_obj)
                    pinned_edges.extend(m_pinned_edges)

            poly_shader, is_poly = get_polyline_info()
            if poly_shader and (pinned_coords or pinned_edges):
                orig_depth_test = 'NONE'
                try:
                    orig_depth_test = gpu.state.depth_test_get()
                    if topo_obj.show_in_front:
                        gpu.state.depth_test_set('NONE')
                    else:
                        gpu.state.depth_test_set('LESS_EQUAL')
                except Exception:
                    pass

                # 1. Always draw edges (lines) if they exist
                if pinned_edges:
                    coords = []
                    for p1, p2 in pinned_edges:
                        coords.append(tuple(offset_towards_camera(p1, context)))
                        coords.append(tuple(offset_towards_camera(p2, context)))
                    draw_lines_smooth(coords, (1.0, 0.9, 0.0, 1.0), 4.0, poly_shader, is_poly, 'LINES')

                # Restore original depth test state
                try:
                    gpu.state.depth_test_set(orig_depth_test)
                except Exception:
                    pass




def filter_occluded_points(points, context, topo_obj):
    if not points:
        return []
    ref_obj_name = context.window_manager.tp_ref_object_name
    ref_obj = bpy.data.objects.get(ref_obj_name)
    if not ref_obj or topo_obj.show_in_front:
        return points
        
    filtered = []
    try:
        rv3d = context.space_data.region_3d
        cam_matrix = rv3d.view_matrix.inverted()
        cam_pos = cam_matrix.to_translation()
        
        mat_inv = ref_obj.matrix_world.inverted()
        origin_local = mat_inv @ cam_pos
        depsgraph = context.evaluated_depsgraph_get()
        
        for p in points:
            direction = p - cam_pos
            dist_to_point = direction.length
            if dist_to_point < 1e-4:
                filtered.append(p)
                continue
                
            dest_local = mat_inv @ p
            dir_local = dest_local - origin_local
            dir_len = dir_local.length
            if dir_len < 1e-4:
                filtered.append(p)
                continue
                
            dir_local_norm = dir_local / dir_len
            max_dist_local = dir_len * 0.999 # stop just before hitting the point on the surface
            
            try:
                success, hit_loc, hit_normal, face_idx = ref_obj.ray_cast(origin_local, dir_local_norm, distance=max_dist_local, depsgraph=depsgraph)
            except:
                try:
                    success, hit_loc, hit_normal, face_idx = ref_obj.ray_cast(origin_local, dir_local_norm, distance=max_dist_local)
                except:
                    success = False
                    
            if not success:
                filtered.append(p)
    except Exception as e:
        print("Error filtering occluded points:", e)
        return points
        
    return filtered

def draw_text_callback(self, context):
    # 0. Draw the active stroke dots in real-time as smooth 2D circles
    resampled_pts = getattr(self, '_last_resampled_pts', None)
    if resampled_pts and self.stroke_points and len(self.stroke_points) >= 2:
        region = context.region
        rv3d = context.space_data.region_3d
        shader2d = get_shader_2d()
        poly_shader, is_poly = get_polyline_info()
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        visible_resampled = filter_occluded_points(resampled_pts, context, topo_obj) if topo_obj else resampled_pts
        for p in visible_resampled:
            screen_coord = location_3d_to_region_2d(region, rv3d, p)
            if screen_coord:
                cx, cy = screen_coord[0], screen_coord[1]
                draw_smooth_circle_2d(cx, cy, 3.0, (1.0, 0.6, 0.0, 1.0), shader2d, poly_shader, is_poly)
                
        # Draw mirrored stroke dots
        if topo_obj and getattr(context.scene, "tp_symmetry_mode", False):
            for p in resampled_pts:
                m_pts = get_mirrored_coords(p, context, topo_obj)
                visible_m_pts = filter_occluded_points(m_pts, context, topo_obj)
                for mp in visible_m_pts:
                    screen_coord = location_3d_to_region_2d(region, rv3d, mp)
                    if screen_coord:
                        cx, cy = screen_coord[0], screen_coord[1]
                        draw_smooth_circle_2d(cx, cy, 3.0, (1.0, 0.6, 0.0, 0.7), shader2d, poly_shader, is_poly)

    # 0.1. Draw boundary smoothing brush indicator
    is_boundary_mode = getattr(context.scene, "tp_boundary_mode", True)
    shift_held = getattr(self, 'shift_pressed', False)
    if getattr(self, 'is_smoothing', False) or (is_boundary_mode and shift_held):
        cx, cy = self.last_mouse_coord[0], self.last_mouse_coord[1]
        poly_shader, is_poly = get_polyline_info()
        if poly_shader:
            radius = getattr(self, 'smooth_brush_radius', 50.0)
            num_segments = 32
            circle_coords = []
            for i in range(num_segments + 1):
                theta = 2.0 * math.pi * i / num_segments
                circle_coords.append((cx + radius * math.cos(theta), cy + radius * math.sin(theta), 0.0))
            draw_lines_smooth(circle_coords, (1.0, 1.0, 1.0, 1.0), 4.0, poly_shader, is_poly, 'LINE_STRIP')

    # 0.5. Draw selected boundary vertices and pinned vertices
    if True:
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if topo_obj and topo_obj.type == 'MESH':
            is_edit = (topo_obj.mode == 'EDIT')
            is_boundary_mode = getattr(context.scene, "tp_boundary_mode", True)
            unpinned_selected_co = []
            pinned_isolated_co = []
            pinned_continuous_co = []
            if is_edit:
                try:
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    target_edges = get_seam_target_edges_local(bm)
                    boundary_verts = {v for e in target_edges for v in e.verts}
                    
                    global _diag_logged
                    if not _diag_logged:
                        _diag_logged = True
                        try:
                            with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_log:
                                f_log.write(f"[draw_text_success] total_verts={len(bm.verts)}, target_edges={len(target_edges)}, boundary_verts={[v.index for v in boundary_verts]}, selected={[v.index for v in bm.verts if v.select]}\n")
                        except Exception:
                            pass
                            
                    pin_layer = bm.verts.layers.int.get("tp_is_pinned")
                    for v in boundary_verts:
                        co = topo_obj.matrix_world @ v.co
                        is_pinned = pin_layer and (v[pin_layer] == 1)
                        if is_pinned:
                            # Check if this pinned vertex is isolated (no pinned neighbors)
                            is_isolated_pin = True
                            if pin_layer:
                                for e in v.link_edges:
                                    if e.other_vert(v)[pin_layer] == 1:
                                        is_isolated_pin = False
                                        break
                            if is_isolated_pin:
                                pinned_isolated_co.append(co)
                            else:
                                pinned_continuous_co.append(co)
                        elif is_boundary_mode and v.select:
                            # Only draw as white dot if none of the neighbor vertices in BMesh are selected
                            is_isolated = not any(e.other_vert(v).select for e in v.link_edges)
                            if is_isolated:
                                unpinned_selected_co.append(co)
                except Exception as e:
                    try:
                        with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f_log:
                            import traceback
                            f_log.write(f"[draw_text] Error: {e}\n{traceback.format_exc()}\n")
                    except:
                        pass

            else:
                try:
                    mesh = topo_obj.data
                    bm = bmesh.new()
                    bm.from_mesh(mesh)
                    target_edges = get_seam_target_edges_local(bm)
                    boundary_verts = {v for e in target_edges for v in e.verts}
                    pin_attr = mesh.attributes.get("tp_is_pinned")
                    for v in boundary_verts:
                        co = topo_obj.matrix_world @ v.co
                        is_pinned = pin_attr and (pin_attr.data[v.index].value == 1)
                        if is_pinned:
                            # Check if this pinned vertex is isolated (no pinned neighbors)
                            is_isolated_pin = True
                            if pin_attr:
                                for e in v.link_edges:
                                    if pin_attr.data[e.other_vert(v).index].value == 1:
                                        is_isolated_pin = False
                                        break
                            if is_isolated_pin:
                                pinned_isolated_co.append(co)
                            else:
                                pinned_continuous_co.append(co)
                        elif is_boundary_mode and v.select:
                            # Only draw as white dot if none of the neighbor vertices in BMesh are selected
                            is_isolated = not any(e.other_vert(v).select for e in v.link_edges)
                            if is_isolated:
                                unpinned_selected_co.append(co)
                    bm.free()
                except Exception as e:
                    print("Error getting mesh boundary verts for 2D draw:", e)
            if getattr(context.scene, "tp_symmetry_mode", False):
                m_unpinned_selected = []
                for p in unpinned_selected_co:
                    m_unpinned_selected.extend(get_mirrored_coords(p, context, topo_obj))
                unpinned_selected_co.extend(m_unpinned_selected)
                
                m_pinned_isolated = []
                for p in pinned_isolated_co:
                    m_pinned_isolated.extend(get_mirrored_coords(p, context, topo_obj))
                pinned_isolated_co.extend(m_pinned_isolated)
                
                m_pinned_continuous = []
                for p in pinned_continuous_co:
                    m_pinned_continuous.extend(get_mirrored_coords(p, context, topo_obj))
                pinned_continuous_co.extend(m_pinned_continuous)

            # Filter points to respect occlusion when show_in_front is False
            unpinned_selected_co = filter_occluded_points(unpinned_selected_co, context, topo_obj)
            pinned_isolated_co = filter_occluded_points(pinned_isolated_co, context, topo_obj)
            pinned_continuous_co = filter_occluded_points(pinned_continuous_co, context, topo_obj)

            if unpinned_selected_co or pinned_isolated_co:
                region = context.region
                rv3d = context.space_data.region_3d
                shader2d = get_shader_2d()
                poly_shader, is_poly = get_polyline_info()
                if shader2d:
                    # Draw unpinned selected vertices: white dot (r=8) + green center dot (r=4)
                    for world_co in unpinned_selected_co:
                        screen_coord = location_3d_to_region_2d(region, rv3d, world_co)
                        if screen_coord:
                            cx, cy = screen_coord[0], screen_coord[1]
                            # Layer 1: white base dot
                            draw_smooth_circle_2d(cx, cy, 8.0, (1.0, 1.0, 1.0, 1.0), shader2d, poly_shader, is_poly)
                            # Layer 2: green center dot (darker green)
                            draw_smooth_circle_2d(cx, cy, 4.0, (0.0, 0.6, 0.3, 1.0), shader2d, poly_shader, is_poly)

                    # Draw isolated pinned vertices: white dot (r=12) base + orange dot (r=6) overlay
                    for world_co in pinned_isolated_co:
                        screen_coord = location_3d_to_region_2d(region, rv3d, world_co)
                        if screen_coord:
                            cx, cy = screen_coord[0], screen_coord[1]
                            # Layer 1: white base dot (same as selected but larger)
                            draw_smooth_circle_2d(cx, cy, 12.0, (1.0, 1.0, 1.0, 1.0), shader2d, poly_shader, is_poly)
                            # Layer 2: orange snap-style dot on top (larger)
                            draw_smooth_circle_2d(cx, cy, 6.0, (1.0, 0.6, 0.0, 1.0), shader2d, poly_shader, is_poly)

    # 2. Draw hover snap point indicator
    if self.hover_snap_pt:
        region = context.region
        rv3d = context.space_data.region_3d
        screen_coord = location_3d_to_region_2d(region, rv3d, self.hover_snap_pt)
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        visible_self_snap = filter_occluded_points([self.hover_snap_pt], context, topo_obj) if topo_obj else [self.hover_snap_pt]
        if visible_self_snap and screen_coord:
            cx, cy = screen_coord[0], screen_coord[1]
            shader2d = get_shader_2d()
            poly_shader, is_poly = get_polyline_info()
            draw_smooth_circle_2d(cx, cy, 4.0, (1.0, 0.6, 0.0, 1.0), shader2d, poly_shader, is_poly)
            
            # Mirror hover snap indicator
            if topo_obj and getattr(context.scene, "tp_symmetry_mode", False):
                m_pts = get_mirrored_coords(self.hover_snap_pt, context, topo_obj)
                visible_m_pts = filter_occluded_points(m_pts, context, topo_obj)
                for mp in visible_m_pts:
                    m_screen = location_3d_to_region_2d(region, rv3d, mp)
                    if m_screen:
                        draw_smooth_circle_2d(m_screen[0], m_screen[1], 4.0, (1.0, 0.6, 0.0, 0.7), shader2d, poly_shader, is_poly)

    # 2. If actively drawing/placing: show point count
    if self.is_drawing and self.stroke_points:
        last_pt = self.hover_snap_pt if (self.hover_snap_pt and not self.is_dragging) else self.stroke_points[-1]
        region = context.region
        rv3d = context.space_data.region_3d
        screen_coord = location_3d_to_region_2d(region, rv3d, last_pt)
        
        if screen_coord:
            text = f"{len(self.stroke_points)}"
            x = screen_coord[0] + 15
            y = screen_coord[1] + 15
            
            font_id = 0
            try:
                blf.size(font_id, 16)
            except Exception:
                blf.size(font_id, 16, 72)
            blf.color(font_id, 0.0, 1.0, 0.5, 1.0)
            blf.position(font_id, x, y, 0)
            blf.draw(font_id, text)
            
    # 3. If not drawing: show selected point count of the topology mesh
    elif not self.is_drawing:
        topo_obj_name = "TP_Topology_Mesh"
        topo_obj = bpy.data.objects.get(topo_obj_name)
        
        if topo_obj and topo_obj.type == 'MESH':
            selected_count = 0
            selected_center = mathutils.Vector((0.0, 0.0, 0.0))
            
            if topo_obj.mode == 'EDIT':
                try:
                    bm = bmesh.from_edit_mesh(topo_obj.data)
                    selected_verts = [v for v in bm.verts if v.select]
                    selected_count = len(selected_verts)
                    if selected_count > 0:
                        for v in selected_verts:
                            selected_center += v.co
                        selected_center /= selected_count
                        selected_center = topo_obj.matrix_world @ selected_center
                except Exception:
                    pass
            else:
                selected_verts = [v for v in topo_obj.data.vertices if v.select]
                selected_count = len(selected_verts)
                if selected_count > 0:
                    for v in selected_verts:
                        selected_center += v.co
                    selected_center /= selected_count
                    selected_center = topo_obj.matrix_world @ selected_center
                    
            if selected_count > 0:
                region = context.region
                rv3d = context.space_data.region_3d
                screen_coord = location_3d_to_region_2d(region, rv3d, selected_center)
                
                if screen_coord:
                    text = f"{selected_count}"
                    
                    if selected_count % 4 == 0:
                        color = (0.0, 1.0, 0.5, 1.0)
                    else:
                        color = (1.0, 0.8, 0.0, 1.0)
                        
                    x = screen_coord[0] + 15
                    y = screen_coord[1] + 15
                    
                    font_id = 0
                    try:
                        blf.size(font_id, 16)
                    except Exception:
                        blf.size(font_id, 16, 72)
                    blf.color(font_id, color[0], color[1], color[2], color[3])
                    blf.position(font_id, x, y, 0)
                    blf.draw(font_id, text)

    # 4. Draw 2D straight line guide for outside drag
    if getattr(self, 'is_outside_drawing', False):
        poly_shader, is_poly = get_polyline_info()
        if poly_shader:
            start_coord = self.drag_start_coord
            end_coord = self.last_mouse_coord
            coords = [(start_coord[0], start_coord[1], 0.0), (end_coord[0], end_coord[1], 0.0)]
            draw_lines_smooth(coords, (1.0, 0.6, 0.0, 0.8), 3.0, poly_shader, is_poly, 'LINE_STRIP')



