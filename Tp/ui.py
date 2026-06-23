import bpy

class VIEW3D_PT_tp_topology(bpy.types.Panel):
    bl_label = "TP拓扑"
    bl_idname = "VIEW3D_PT_tp_topology"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TP拓扑'

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        wm = context.window_manager
        scene = context.scene
        
        col = layout.column(align=True)
        is_mesh = obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT'
        is_enabled = wm.tp_topology_running or is_mesh
        
        col.prop(scene, "tp_edge_length", text="边长")
        col.prop(scene, "tp_smooth_factor", text="平滑力度")
        col.separator()
        
        row = col.row(align=True)
        row.enabled = is_enabled
        
        row.operator(
            "object.tp_topology_draw", 
            text="拓扑", 
            depress=wm.tp_topology_running, 
            icon='GREASEPENCIL'
        )
        
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if topo_obj:
            row.prop(topo_obj, "show_in_front", text="最前显示", toggle=True, icon='AXIS_FRONT')
        
        if wm.tp_topology_running or (obj and obj.name == "TP_Topology_Mesh"):
            col.separator()
            col.prop(scene, "tp_grid_decay", text="渐变比例")
            row2 = col.row()
            row2.operator(
                "object.tp_topology_grid_fill",
                text="栅格填充",
                icon='GRID'
            )
        
        if not wm.tp_topology_running and not is_mesh:
            col.label(text="请选择一个网格对象开始拓扑", icon='INFO')
