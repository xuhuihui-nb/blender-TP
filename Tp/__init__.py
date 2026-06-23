bl_info = {
    "name": "TP拓扑",
    "author": "huihui-nb",
    "version": (1, 0, 4),
    "blender": (5, 0, 1),
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
        default=0.15,
        min=0.005,
        max=10.0,
        precision=3,
        step=1.0
    )
    bpy.types.Scene.tp_smooth_factor = bpy.props.FloatProperty(
        name="平滑力度",
        description="绘制结束后对线条进行自动平滑的强度 (0为不平滑)",
        default=0.05,
        min=0.0,
        max=1.0,
        precision=2,
        step=5.0
    )
    bpy.types.Scene.tp_grid_decay = bpy.props.FloatProperty(
        name="栅格渐变比例",
        description="控制网格从外圈到内圈的尺寸收缩渐变程度 (0为无渐变)",
        default=2.0,
        min=0.0,
        max=10.0,
        precision=2,
        step=10.0
    )

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.tp_topology_running
    del bpy.types.WindowManager.tp_ref_object_name
    del bpy.types.Scene.tp_edge_length
    del bpy.types.Scene.tp_smooth_factor
    del bpy.types.Scene.tp_grid_decay

if __name__ == "__main__":
    register()