import bpy

class OBJECT_OT_tp_topology_auto_outline(bpy.types.Operator):
    bl_idname = "object.tp_topology_auto_outline"
    bl_label = "生成结构线"
    bl_description = "生成结构线 (暂无功能)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        self.report({'INFO'}, "生成结构线功能暂未实现")
        return {'FINISHED'}

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
        
        row = col.row(align=True)
        row.enabled = is_enabled
        row.scale_y = 3.0
        row.operator(
            "object.tp_topology_draw", 
            text="拓扑", 
            depress=wm.tp_topology_running, 
            icon='GREASEPENCIL'
        )
        
        topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
        if topo_obj:
            col.separator()
            row_front = col.row(align=True)
            row_front.prop(topo_obj, "show_in_front", text="最前显示", toggle=True, icon='AXIS_FRONT')
            row_front.prop(scene, "tp_use_wrap", text="包裹", toggle=True, icon='MOD_SHRINKWRAP')
            row_front.prop(scene, "tp_pin_boundary", text="固定", toggle=True, icon='PINNED')
        
        col.separator()
        col.prop(scene, "tp_edge_length", text="边长")
        
        if wm.tp_topology_running or (obj and obj.name == "TP_Topology_Mesh"):
            col.separator()
            row_auto = col.row(align=True)
            row_auto.scale_y = 2.0
            row_auto.operator(
                "object.tp_topology_auto_outline",
                text=""
            )
            col.separator()
            
            # 栅格微调面板
            grid_box = col.box()
            grid_box.label(text="栅格微调:", icon='GRID')
            grid_box.prop(scene, "tp_grid_span", text="跨分")
            grid_box.prop(scene, "tp_grid_offset", text="偏移")
            
            col.separator()
            row2 = col.row(align=True)
            row2.operator(
                "object.tp_topology_grid_fill",
                text="栅格填充",
                icon='GRID'
            )
            row2.operator(
                "object.tp_topology_remove_grid",
                text="移除栅格",
                icon='TRASH'
            )
        
        if not wm.tp_topology_running and not is_mesh:
            col.label(text="请选择一个网格对象开始拓扑", icon='INFO')

        # 使用教程 & 快捷键说明
        layout.separator()
        box = layout.box()
        
        row = box.row(align=True)
        row.prop(
            scene,
            "tp_show_tutorial",
            text="使用教程 & 快捷键说明",
            icon="TRIA_DOWN" if scene.tp_show_tutorial else "TRIA_RIGHT",
            emboss=False,
            toggle=True
        )
        
        if scene.tp_show_tutorial:
            col_t = box.column(align=True)
            
            col_t.label(text="【起步与退出】", icon='PLAY')
            col_t.label(text="• 启动拓扑: 选中高模网格 -> 点击【拓扑】按钮")
            col_t.label(text="• 退出拓扑: 按 ESC 键 或 再次点击【拓扑】按钮")
            
            col_t.separator()
            col_t.label(text="【绘制拓扑线】", icon='GREASEPENCIL')
            col_t.label(text="• 连续画线: 按住 Ctrl + 左键拖动")
            col_t.label(text="• 多段画线: 按住 Ctrl + 左键单击 (回车键/Enter提交)")
            col_t.label(text="• 自动合并: 靠近起点或已有顶点时自动吸附并合并")
            col_t.label(text="• 取消绘制: 绘制未提交时，点击右键取消")
            col_t.label(text="• 撤销重做: Ctrl + Z 撤销 (多段画线时撤销上一个点) / Ctrl + Y 重做")
            
            col_t.separator()
            col_t.label(text="【编辑与调整】", icon='GRIP')
            col_t.label(text="• 循环边选择: Alt + 左键点击顶点/边")
            col_t.label(text="  (多次点击切换候选路径，按 Shift 追加选择)")
            col_t.label(text="• 移动顶点: 悬停在点附近按 G 键直接移动，或选中后按 G 键")
            col_t.label(text="  (鼠标左键/回车/空格确认，鼠标右键/ESC取消)")
            col_t.label(text="• 循环细分: 按住 Ctrl + 鼠标滚轮调整细分")
            
            col_t.separator()
            col_t.label(text="【快捷栅格填充】", icon='GRID')
            col_t.label(text="• 选择闭合圈: Alt + 左键选中边界线圈")
            col_t.label(text="• 生成网格: 点击【栅格填充】")
            col_t.label(text="• 实时微调: 选中已填充的栅格(或不选任何元素)在面板修改【跨分】与【偏移】可实时更新")
            col_t.label(text="• 移除栅格: 选中已填充的栅格(或不选任何元素)点击【移除栅格】")


