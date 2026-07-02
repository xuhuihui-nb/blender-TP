import bpy

class OBJECT_OT_tp_set_fixed_point_count(bpy.types.Operator):
    bl_idname = "object.tp_set_fixed_point_count"
    bl_label = "设置固定点数"
    bl_description = "快速设置固定点数"
    bl_options = {'REGISTER', 'UNDO'}

    count: bpy.props.IntProperty(name="点数", default=0)

    def execute(self, context):
        if self.count == 0:
            context.scene.tp_use_fixed_point_count = False
            context.scene.tp_fixed_point_count = 0
        else:
            context.scene.tp_use_fixed_point_count = True
            context.scene.tp_fixed_point_count = self.count
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
        
        if wm.tp_topology_running:
            topo_obj = bpy.data.objects.get("TP_Topology_Mesh")
            if topo_obj:
                col.separator()
                row_front = col.row(align=True)
                row_front.prop(scene, "tp_boundary_mode", text="边界", toggle=True, icon='EDGESEL')
                row_front.prop(topo_obj, "show_in_front", text="最前显示", toggle=True, icon='AXIS_FRONT')
                row_front.prop(scene, "tp_use_wrap", text="包裹", toggle=True, icon='MOD_SHRINKWRAP')
                row_front.prop(scene, "tp_pin_boundary", text="固定", toggle=True, icon='PINNED')
                row_front.prop(scene, "tp_seam_edge", text="缝合边", toggle=True, icon='EDGE_SEAM')
                
                row_sym = col.row(align=True)
                row_sym.prop(scene, "tp_symmetry_mode", text="对称", toggle=True, icon='MOD_MIRROR')
                row_sym.operator("object.tp_apply_symmetry", text="确认")
                sub_row = row_sym.row(align=True)
                sub_row.enabled = scene.tp_symmetry_mode
                sub_row.prop(scene, "tp_symmetry_x", text="X", toggle=True)
                sub_row.prop(scene, "tp_symmetry_y", text="Y", toggle=True)
                sub_row.prop(scene, "tp_symmetry_z", text="Z", toggle=True)
            col.separator()
            row_auto = col.row(align=True)
            row_auto.scale_y = 2.0
            row_auto.prop(scene, "tp_square_mode", text="方形", toggle=True)
            row_auto.prop(scene, "tp_circle_mode", text="圆形", toggle=True)
            col.separator()
            
            # 栅格微调面板
            grid_box = col.box()
            grid_box.label(text="参数调整:", icon='GRID')
            row_point = grid_box.row(align=True)
            split = row_point.split(factor=0.333, align=True)
            split.prop(scene, "tp_use_fixed_point_count", text="点的数量", toggle=True)
            split.prop(scene, "tp_fixed_point_count", text="")
            
            row_quick = grid_box.row(align=True)
            for val in [0, 4, 8, 16, 32, 64, 128]:
                is_depressed = False
                if val == 0:
                    is_depressed = not scene.tp_use_fixed_point_count
                else:
                    is_depressed = scene.tp_use_fixed_point_count and scene.tp_fixed_point_count == val
                
                op = row_quick.operator("object.tp_set_fixed_point_count", text=str(val), depress=is_depressed)
                op.count = val
                
            if scene.tp_boundary_mode:
                grid_box.prop(scene, "tp_smooth_strength", text="平滑力度")
            grid_box.prop(scene, "tp_edge_length", text="边长")
            grid_box.prop(scene, "tp_grid_span", text="跨分")
            grid_box.prop(scene, "tp_grid_offset", text="偏移")
            
            col.separator()
            row2 = col.row(align=True)
            row2.prop(scene, "tp_auto_grid_fill", text="自动栅格", toggle=True, icon='GRID')
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
            
            # 使用教程 & 快捷键说明
            layout.separator()
            box = layout.box()
            
            row = box.row(align=True)
            row.prop(
                scene,
                "tp_show_tutorial",
                text="教程 & Tutorial",
                icon="TRIA_DOWN" if scene.tp_show_tutorial else "TRIA_RIGHT",
                emboss=False,
                toggle=True
            )
            
            if scene.tp_show_tutorial:
                row_lang = box.row(align=True)
                row_lang.prop(scene, "tp_tutorial_lang", expand=True)
                
                col_t = box.column(align=True)
                if scene.tp_tutorial_lang == 'ZH':
                    col_t.label(text="【起步与退出】", icon='PLAY')
                    col_t.label(text="• 启动拓扑: 选中高模网格 -> 点击【拓扑】按钮")
                    col_t.label(text="• 退出拓扑: 按 ESC 键 或 再次点击【拓扑】按钮")
                    
                    col_t.separator()
                    col_t.label(text="【绘制与生成】", icon='GREASEPENCIL')
                    col_t.label(text="• 连续画线: 按住 Ctrl + 左键拖动")
                    col_t.label(text="• 多段画线: 按住 Ctrl + 左键单击 (回车/Enter提交)")
                    col_t.label(text="• 包围拓扑: Ctrl + 左键从外部拖拽划过网格，自动生成包围闭合圈")
                    col_t.label(text="• 自动合并: 靠近起点或已有顶点时自动吸附并合并")
                    col_t.label(text="• 自动填充: 绘制闭合圈符合栅格要求时，将自动进行栅格填充")
                    col_t.label(text="• 取消绘制: 绘制未提交时，点击鼠标右键取消")
                    col_t.label(text="• 撤销重做: Ctrl + Z 撤销 (多段线时撤销上一个点) / Ctrl + Y 重做")
                    
                    col_t.separator()
                    col_t.label(text="【参数与选项】", icon='PROPERTIES')
                    col_t.label(text="• 对称与确认: 开启【对称】将实时显示镜像模型；点击【确认】可应用镜像（将镜像实体化）并退出对称状态")
                    col_t.label(text="• 目标边长: 调整【边长】控制绘制线段的默认采样密度")
                    col_t.label(text="• 固定点数: 勾选【点的数量】并设数值，将固定顶点数绘制")
                    col_t.label(text="• 最前与包裹: 开启【最前显示】便于观察，【包裹】使点自动贴合高模")
                    col_t.label(text="• 锁定边界: 开启【固定】可锁定边界顶点；若选点则仅锁定选中点")
                    
                    col_t.separator()
                    col_t.label(text="【边界交互 (白线)】", icon='EDGESEL')
                    col_t.label(text="• 自动模式: 启用【边界】交互模式时，自动切换为网格“边选择”模式，操作更直观")
                    col_t.label(text="• 边界选择: 【边界】模式下，左键单击白线顶点或边可选中/取消选择该元素；按住 Shift + 左键可加选/减选单个顶点或边；按住 Alt + 左键点击则可选择循环边")
                    col_t.label(text="• 边界拖动: 【边界】模式下，左键直接在白线顶点/边附近拖动可实时改变边界 (拖动时将自动选择当前顶点并取消选择其他元素)")
                    col_t.label(text="• 双击固定: 【边界】模式下，双击白线顶点可单独固定/取消固定该点 (非连续固定点显示为橙黄色大圆点，连续固定点显示为黄色边)")
                    col_t.label(text="• 边界平滑: 【边界】模式下，按住 Shift + 左键拖动，可用圆形笔刷实时平滑白线")
                    col_t.label(text="• 元素隔离: 【边界】模式下，内部栅格元素将不可选，防止意外选中或误操作")
                    col_t.label(text="• 安全吸附: 边界点吸附时，只吸附白线顶点，且防止鼠标穿透网格吸附到背面")
                    
                    col_t.separator()
                    col_t.label(text="【编辑与调整】", icon='GRIP')
                    col_t.label(text="• 循环边选择: Alt + 左键点击顶点/边 (多次点击切换候选路径，Shift+Alt可加选)")
                    col_t.label(text="• 顶点微调: 点附近按 G 键直接移动，或选中后按 G 键 (右键/ESC取消，LMB/Enter确认)")
                    col_t.label(text="• 循环细分: 选中边/圈或已填栅格，Ctrl + 滚轮可实时调整密度且保持网格形状稳定")
                    
                    col_t.separator()
                    col_t.label(text="【栅格填充微调】", icon='GRID')
                    col_t.label(text="• 手动填充: Alt + 左键选中边界线圈 -> 点击【栅格填充】")
                    col_t.label(text="• 实时微调: 修改【跨分】与【偏移】可实时微调选中的已填充栅格")
                    col_t.label(text="• 移除栅格: 选中已填充栅格或不选任何元素，点击【移除栅格】")
                else:
                    col_t.label(text="【Start & Exit】", icon='PLAY')
                    col_t.label(text="• Start Topology: Select high-poly mesh -> Click [Topology] button")
                    col_t.label(text="• Exit Topology: Press ESC or click [Topology] button again")
                    
                    col_t.separator()
                    col_t.label(text="【Draw & Generate】", icon='GREASEPENCIL')
                    col_t.label(text="• Continuous Draw: Hold Ctrl + drag LMB (Left Mouse Button)")
                    col_t.label(text="• Polyline Draw: Hold Ctrl + click LMB (Press Enter to submit)")
                    col_t.label(text="• Envelope Topology: Hold Ctrl + drag LMB from outside across mesh to auto-generate a closed loop")
                    col_t.label(text="• Auto Merge: Snaps and merges automatically when close to start point or existing vertices")
                    col_t.label(text="• Auto Fill: Automatically grid-fills when drawing a closed loop that meets grid requirements")
                    col_t.label(text="• Cancel Drawing: Click RMB (Right Mouse Button) to cancel before submitting")
                    col_t.label(text="• Undo/Redo: Ctrl + Z to undo (undos the last point for polylines) / Ctrl + Y to redo")
                    
                    col_t.separator()
                    col_t.label(text="【Parameters & Options】", icon='PROPERTIES')
                    col_t.label(text="• Symmetry & Confirm: Enable [Symmetry] to mirror topology in real-time; click [Confirm] to apply/bake mirror geometry and exit symmetry mode")
                    col_t.label(text="• Target Edge Length: Adjust [Edge Length] to control default sampling density of drawn strokes")
                    col_t.label(text="• Fixed Point Count: Enable [Number of Points] and set value to draw with fixed vertices")
                    col_t.label(text="• In Front & Wrap: Enable [In Front] for visibility, [Wrap] to automatically snap vertices to the high-poly surface")
                    col_t.label(text="• Lock Boundary: Enable [Pin] to lock boundary vertices; if vertices are selected, only locks selected vertices")
                    
                    col_t.separator()
                    col_t.label(text="【Boundary Interaction (White Lines)】", icon='EDGESEL')
                    col_t.label(text="• Auto Mode: Enabling [Boundary] mode automatically switches Blender to [Edge Select] mode for easier interaction")
                    col_t.label(text="• Boundary Selection: Under [Boundary] mode, click LMB on a white boundary vertex or edge to select/deselect it (Shift + LMB to toggle select individual vertex or edge), or hold Alt + LMB to select loops")
                    col_t.label(text="• Drag Boundary: Under [Boundary] mode, drag LMB near white boundary vertices/edges to adjust boundary shape (automatically selects the dragged vertex and deselects other vertices)")
                    col_t.label(text="• Double-Click Pin: Under [Boundary] mode, double-click a white boundary vertex to pin/unpin it individually (isolated pinned points display as large orange-yellow dots, continuous pinned edges display as yellow lines)")
                    col_t.label(text="• Smooth Boundary: Under [Boundary] mode, hold Shift + drag LMB to smooth white lines using a circular brush")
                    col_t.label(text="• Element Isolation: Under [Boundary] mode, internal grid elements are locked/unselectable to prevent errors")
                    col_t.label(text="• Safe Snapping: Snaps only to other boundary vertices, preventing cursor from penetrating the mesh")
                    
                    col_t.separator()
                    col_t.label(text="【Edit & Adjust】", icon='GRIP')
                    col_t.label(text="• Edge Loop Select: Alt + click LMB on vertex/edge (click repeatedly to cycle paths, Shift+Alt to add)")
                    col_t.label(text="• Tweak Vertex: Press G near a vertex to move it directly, or press G after selecting (RMB/ESC to cancel, LMB/Enter to confirm)")
                    col_t.label(text="• Loop Subdivide: Select edge/loop/grid, Ctrl + Scroll Wheel to adjust density dynamically with stable shape")
                    
                    col_t.separator()
                    col_t.label(text="【Grid Fill Adjustment】", icon='GRID')
                    col_t.label(text="• Manual Fill: Alt + click LMB to select a boundary loop -> Click [Grid Fill]")
                    col_t.label(text="• Real-time Tweak: Adjust [Span] and [Offset] to tweak the selected grid fill in real-time")
                    col_t.label(text="• Remove Grid: Select a grid fill or nothing, then click [Remove Grid]")
        else:
            if not is_mesh:
                col.label(text="请选择一个网格对象开始拓扑", icon='INFO')


