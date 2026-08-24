import shutil
import folder_paths
import os

comfy_path = os.path.dirname(folder_paths.__file__)
nodes_path = os.path.join(os.path.dirname(__file__))

# Modern ComfyUI serves the front end script straight from this folder.
WEB_DIRECTORY = "./js"

def setup_js():
    # Legacy location, only used by older ComfyUI versions that do not
    # understand WEB_DIRECTORY. Failing here is harmless.
    try:
        js_dest_path = os.path.join(comfy_path, "web", "extensions", "smart-prompting")
        if not os.path.exists(js_dest_path):
            os.makedirs(js_dest_path)

        js_src_path = os.path.join(nodes_path, "js", "smart-prompting.js")
        shutil.copy(js_src_path, js_dest_path)
    except Exception as error:
        print(f"[smart-prompting] Skipped legacy js copy: {error}")

# only import if running as a custom node
try:
	import comfy.utils
except ImportError:
	pass
else:
	from .smart_prompting import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
	__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']

	setup_js()
