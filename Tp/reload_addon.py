import bpy
import sys
import importlib

addon_name = "TP"

# Force reload all modules under the TP package
modules_to_reload = [name for name in sys.modules.keys() if name.startswith(addon_name) or name == addon_name]

print("--- Reloading TP Addon ---")
for mod_name in modules_to_reload:
    try:
        importlib.reload(sys.modules[mod_name])
        print(f"Successfully reloaded: {mod_name}")
    except Exception as e:
        print(f"Failed to reload {mod_name}: {e}")

# Disable and re-enable the addon to refresh registration
try:
    bpy.ops.preferences.addon_disable(module=addon_name)
    bpy.ops.preferences.addon_enable(module=addon_name)
    print("Addon re-registered successfully!")
except Exception as e:
    print(f"Error re-registering addon: {e}")
