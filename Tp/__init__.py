bl_info = {
    "name": "TP拓扑",
    "author": "Antigravity",
    "version": (2, 1, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > TP拓扑",
    "description": "在被选网格对象表面绘制连续拓扑线的工具",
    "warning": "",
    "doc_url": "",
    "category": "Mesh",
}

# Standard hot-reload pattern for Blender multi-file addons
if "bpy" in locals():
    import importlib
    if "draw_utils" in locals():
        importlib.reload(draw_utils)
    if "op_draw" in locals():
        importlib.reload(op_draw)
    if "op_grid_fill" in locals():
        importlib.reload(op_grid_fill)
    if "ui" in locals():
        importlib.reload(ui)

import bpy
from . import draw_utils
from . import op_draw
from . import op_grid_fill
from . import ui

classes = (
    op_draw.OBJECT_OT_tp_topology_draw,
    op_draw.OBJECT_OT_tp_apply_symmetry,
    op_grid_fill.OBJECT_OT_tp_topology_grid_fill,
    op_grid_fill.OBJECT_OT_tp_topology_remove_grid,
    ui.OBJECT_OT_tp_topology_auto_outline,
    ui.OBJECT_OT_tp_set_fixed_point_count,
    ui.VIEW3D_PT_tp_topology,
)

def register():
    try:
        with open("d:/文档/addons/TP/debug_log_draw.txt", "a", encoding="utf-8") as f:
            f.write("--- TP Addon Registered (Version 2.1.1) ---\n")
    except:
        pass
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.tp_topology_running = bpy.props.BoolProperty(default=False)
    bpy.types.WindowManager.tp_ref_object_name = bpy.props.StringProperty(
        name="参考对象名称",
        default=""
    )
    
    bpy.types.Scene.tp_edge_length = bpy.props.FloatProperty(
        name="边长",
        description="控制绘制时的目标点间距",
        default=0.1,
        min=0.005,
        max=10.0,
        precision=3,
        step=0.5,
        update=op_draw.on_edge_length_update
    )
    bpy.types.Scene.tp_use_fixed_point_count = bpy.props.BoolProperty(
        name="点的数量",
        description="开启后，绘制的圈或线点的数量固定为指定的数量（不含栅格化的点）",
        default=False
    )
    bpy.types.Scene.tp_fixed_point_count = bpy.props.IntProperty(
        name="点数",
        description="指定的点的数量",
        default=0,
        min=0
    )
    bpy.types.Scene.tp_use_wrap = bpy.props.BoolProperty(
        name="包裹",
        description="拓扑时自动吸附并使用收缩包裹贴合表面",
        default=True
    )
    bpy.types.Scene.tp_boundary_mode = bpy.props.BoolProperty(
        name="边界",
        description="显示白边外圈并允许拖拽调整栅格",
        default=True,
        update=op_draw.on_boundary_mode_update
    )
    bpy.types.Scene.tp_smooth_strength = bpy.props.FloatProperty(
        name="平滑力度",
        description="控制边界平滑笔刷的力度",
        default=0.2,
        min=0.01,
        max=1.0,
        precision=2,
        step=5
    )
    bpy.types.Scene.tp_pin_boundary = bpy.props.BoolProperty(
        name="固定",
        description="固定外圈的点（边界点）不可移动",
        default=False,
        update=op_draw.on_pin_boundary_update
    )
    bpy.types.Scene.tp_seam_edge = bpy.props.BoolProperty(
        name="缝合边",
        description="将网格的边界边或选中边设置为缝合边",
        default=False,
        update=op_draw.on_seam_edge_update
    )
    bpy.types.Scene.tp_symmetry_mode = bpy.props.BoolProperty(
        name="对称",
        description="启用对称显示拓扑内容",
        default=True,
        update=op_draw.on_symmetry_mode_update
    )
    bpy.types.Scene.tp_symmetry_x = bpy.props.BoolProperty(
        name="X",
        description="X轴对称",
        default=True,
        update=op_draw.on_symmetry_axis_update
    )
    bpy.types.Scene.tp_symmetry_y = bpy.props.BoolProperty(
        name="Y",
        description="Y轴对称",
        default=False,
        update=op_draw.on_symmetry_axis_update
    )
    bpy.types.Scene.tp_symmetry_z = bpy.props.BoolProperty(
        name="Z",
        description="Z轴对称",
        default=False,
        update=op_draw.on_symmetry_axis_update
    )
    bpy.types.Scene.tp_auto_grid_fill = bpy.props.BoolProperty(
        name="自动填充栅格",
        description="是否自动将绘制的闭合圈填充为栅格网格",
        default=True
    )
    bpy.types.Scene.tp_grid_span = bpy.props.IntProperty(
        name="跨分",
        description="栅格一边的网格数。0 表示自动计算",
        default=0,
        min=0,
        update=op_grid_fill.on_grid_settings_update
    )
    bpy.types.Scene.tp_grid_offset = bpy.props.IntProperty(
        name="偏移",
        description="网格顶点的起点偏移量",
        default=0,
        min=0,
        max=100000,
        update=op_grid_fill.on_grid_settings_update
    )
    bpy.types.Scene.tp_show_tutorial = bpy.props.BoolProperty(
        name="显示教程",
        description="展开/折叠 教程 & Tutorial",
        default=False
    )
    bpy.types.Scene.tp_tutorial_lang = bpy.props.EnumProperty(
        items=[
            ('ZH', "中文教程", "显示中文使用教程"),
            ('EN', "English Tutorial", "Show English tutorial instructions")
        ],
        name="教程语言",
        default='ZH'
    )
    # Register depsgraph update handler
    bpy.app.handlers.depsgraph_update_post.append(op_draw.tp_pin_depsgraph_handler)

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.tp_topology_running
    del bpy.types.WindowManager.tp_ref_object_name
    del bpy.types.Scene.tp_edge_length
    del bpy.types.Scene.tp_use_fixed_point_count
    del bpy.types.Scene.tp_fixed_point_count
    del bpy.types.Scene.tp_use_wrap
    del bpy.types.Scene.tp_boundary_mode
    del bpy.types.Scene.tp_smooth_strength
    del bpy.types.Scene.tp_pin_boundary
    del bpy.types.Scene.tp_seam_edge
    del bpy.types.Scene.tp_symmetry_mode
    del bpy.types.Scene.tp_symmetry_x
    del bpy.types.Scene.tp_symmetry_y
    del bpy.types.Scene.tp_symmetry_z
    del bpy.types.Scene.tp_auto_grid_fill
    del bpy.types.Scene.tp_grid_span
    del bpy.types.Scene.tp_grid_offset
    del bpy.types.Scene.tp_show_tutorial
    del bpy.types.Scene.tp_tutorial_lang
    # Unregister depsgraph update handler
    if op_draw.tp_pin_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(op_draw.tp_pin_depsgraph_handler)

if __name__ == "__main__":
    register()