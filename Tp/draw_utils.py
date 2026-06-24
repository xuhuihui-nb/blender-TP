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

def get_shader_2d():
    try:
        return gpu.shader.from_builtin('2D_UNIFORM_COLOR')
    except Exception:
        try:
            return gpu.shader.from_builtin('UNIFORM_COLOR')
        except Exception:
            return None

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
        
        shader = get_shader()
        if shader:
            shader.bind()
            shader.uniform_float("color", (1.0, 0.6, 0.0, 1.0))
            try:
                gpu.state.line_width_set(3.0)
            except Exception:
                pass
            coords = [tuple(p) for p in resampled_pts]
            try:
                batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": coords})
                batch.draw(shader)
            except Exception as e:
                print("TP Topology Draw Error:", e)
                
            try:
                gpu.state.point_size_set(6.0)
            except Exception:
                pass
            try:
                batch_dots = batch_for_shader(shader, 'POINTS', {"pos": coords})
                batch_dots.draw(shader)
            except Exception:
                pass

    # 2. Draw rubber-band preview line for polyline mode
    if not self.is_dragging and self.stroke_points and self.is_polyline:
        preview_pt = self.hover_snap_pt
        if not preview_pt:
            coord = (self.last_mouse_coord[0], self.last_mouse_coord[1])
            preview_pt = self.get_surface_point(context, coord)
            
        if preview_pt:
            shader = get_shader()
            if shader:
                shader.bind()
                shader.uniform_float("color", (1.0, 1.0, 1.0, 0.6))
                try:
                    gpu.state.line_width_set(2.0)
                except Exception:
                    pass
                coords = [tuple(self.stroke_points[-1]), tuple(preview_pt)]
                try:
                    batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": coords})
                    batch.draw(shader)
                except Exception:
                    pass

    # 3. Draw pinned boundary vertices/edges overlay
    if context.scene.tp_pin_boundary:
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
            
            shader = get_shader()
            if shader and (pinned_coords or pinned_edges):
                shader.bind()
                # Pure White color: (1.0, 1.0, 1.0, 1.0)
                shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
                
                # 1. Always draw edges (lines) if they exist
                if pinned_edges:
                    try:
                        gpu.state.line_width_set(4.0)
                    except Exception:
                        pass
                    coords = []
                    for p1, p2 in pinned_edges:
                        coords.append(tuple(p1))
                        coords.append(tuple(p2))
                    try:
                        batch_pinned_edges = batch_for_shader(shader, 'LINES', {"pos": coords})
                        batch_pinned_edges.draw(shader)
                    except Exception:
                        pass
                
                # 2. Always draw points (dots) if they exist
                if pinned_coords:
                    try:
                        gpu.state.point_size_set(10.0)
                    except Exception:
                        pass
                    try:
                        batch_pinned = batch_for_shader(shader, 'POINTS', {"pos": [tuple(p) for p in pinned_coords]})
                        batch_pinned.draw(shader)
                    except Exception:
                        pass

def draw_text_callback(self, context):
    # 1. Draw hover snap point indicator
    if self.hover_snap_pt:
        region = context.region
        rv3d = context.space_data.region_3d
        screen_coord = location_3d_to_region_2d(region, rv3d, self.hover_snap_pt)
        if screen_coord:
            cx, cy = screen_coord[0], screen_coord[1]
            shader = get_shader_2d()
            if shader:
                shader.bind()
                shader.uniform_float("color", (0.0, 1.0, 0.2, 1.0))
                
                num_segments = 16
                circle_coords = []
                for i in range(num_segments):
                    theta = 2.0 * math.pi * i / num_segments
                    circle_coords.append((cx + 6.0 * math.cos(theta), cy + 6.0 * math.sin(theta)))
                    
                try:
                    gpu.state.line_width_set(3.0)
                except Exception:
                    pass
                try:
                    batch_circle = batch_for_shader(shader, 'LINE_LOOP', {"pos": circle_coords})
                    batch_circle.draw(shader)
                except Exception:
                    pass
                    
                try:
                    gpu.state.point_size_set(6.0)
                except Exception:
                    pass
                try:
                    batch_dot = batch_for_shader(shader, 'POINTS', {"pos": [(cx, cy)]})
                    batch_dot.draw(shader)
                except Exception:
                    pass

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
        shader = get_shader_2d()
        if shader:
            shader.bind()
            shader.uniform_float("color", (1.0, 0.6, 0.0, 0.8))
            try:
                gpu.state.line_width_set(3.0)
            except Exception:
                pass
            
            start_coord = self.drag_start_coord
            end_coord = self.last_mouse_coord
            coords = [start_coord, end_coord]
            try:
                batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": coords})
                batch.draw(shader)
            except Exception:
                pass

