import shutil
import folder_paths
import os

comfy_path = os.path.dirname(folder_paths.__file__)
nodes_path = os.path.join(os.path.dirname(__file__))

def setup_js():
    js_dest_path = os.path.join(comfy_path, "web", "extensions", "smart-prompting")
    if not os.path.exists(js_dest_path):
        os.makedirs(js_dest_path)

    js_src_path = os.path.join(nodes_path, "js", "smart-prompting.js")
    shutil.copy(js_src_path, js_dest_path)

# only import if running as a custom node
try:
	import comfy.utils
except ImportError:
	pass
else:
	from .smart_prompting import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
	__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']
    
	setup_js()
