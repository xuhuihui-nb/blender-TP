bl_info = {
    "name": "TP拓扑",
    "author": "Antigravity",
    "version": (2, 1, 0),
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
    op_grid_fill.OBJECT_OT_tp_topology_grid_fill,
    op_grid_fill.OBJECT_OT_tp_topology_remove_grid,
    ui.OBJECT_OT_tp_topology_auto_outline,
    ui.VIEW3D_PT_tp_topology,
)

def register():
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
        default=0.05,
        min=0.005,
        max=10.0,
        precision=3,
        step=1.0
    )
    bpy.types.Scene.tp_smooth_factor = bpy.props.FloatProperty(
        name="平滑力度",
        description="绘制结束后对线条进行自动平滑的强度 (0为不平滑)",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=2,
        step=5.0
    )
    bpy.types.Scene.tp_use_wrap = bpy.props.BoolProperty(
        name="包裹",
        description="拓扑时自动吸附并使用收缩包裹贴合表面",
        default=True
    )
    bpy.types.Scene.tp_pin_boundary = bpy.props.BoolProperty(
        name="固定",
        description="固定外圈的点（边界点）不可移动",
        default=False,
        update=op_draw.on_pin_boundary_update
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
        update=op_grid_fill.on_grid_settings_update
    )
    
    # Register depsgraph update handler
    bpy.app.handlers.depsgraph_update_post.append(op_draw.tp_pin_depsgraph_handler)

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.tp_topology_running
    del bpy.types.WindowManager.tp_ref_object_name
    del bpy.types.Scene.tp_edge_length
    del bpy.types.Scene.tp_smooth_factor
    del bpy.types.Scene.tp_use_wrap
    del bpy.types.Scene.tp_pin_boundary
    del bpy.types.Scene.tp_grid_span
    del bpy.types.Scene.tp_grid_offset
    
    # Unregister depsgraph update handler
    if op_draw.tp_pin_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(op_draw.tp_pin_depsgraph_handler)

if __name__ == "__main__":
    register()