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

def draw_callback(self, context):
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
                        coords.append(tuple(p1))
                        coords.append(tuple(p2))
                    draw_lines_smooth(coords, (1.0, 1.0, 1.0, 1.0), 4.0, poly_shader, is_poly, 'LINES')

                # 2. Draw selected boundary edges (orange, fully opaque)
                if selected_edges:
                    coords = []
                    for p1, p2 in selected_edges:
                        coords.append(tuple(p1))
                        coords.append(tuple(p2))
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
                            coords.append(tuple(p1))
                            coords.append(tuple(p2))
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
                        coords.append(tuple(p1))
                        coords.append(tuple(p2))
                    draw_lines_smooth(coords, (1.0, 0.9, 0.0, 1.0), 4.0, poly_shader, is_poly, 'LINES')

                # Restore original depth test state
                try:
                    gpu.state.depth_test_set(orig_depth_test)
                except Exception:
                    pass


def draw_text_callback(self, context):
    # 0. Draw the active stroke dots in real-time as smooth 2D circles
    resampled_pts = getattr(self, '_last_resampled_pts', None)
    if resampled_pts and self.stroke_points and len(self.stroke_points) >= 2:
        region = context.region
        rv3d = context.space_data.region_3d
        shader2d = get_shader_2d()
        poly_shader, is_poly = get_polyline_info()
        for p in resampled_pts:
            screen_coord = location_3d_to_region_2d(region, rv3d, p)
            if screen_coord:
                cx, cy = screen_coord[0], screen_coord[1]
                draw_smooth_circle_2d(cx, cy, 3.0, (1.0, 0.6, 0.0, 1.0), shader2d, poly_shader, is_poly)

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
        if screen_coord:
            cx, cy = screen_coord[0], screen_coord[1]
            shader2d = get_shader_2d()
            poly_shader, is_poly = get_polyline_info()
            draw_smooth_circle_2d(cx, cy, 4.0, (1.0, 0.6, 0.0, 1.0), shader2d, poly_shader, is_poly)

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

