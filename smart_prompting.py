import os
import re
import json
import hashlib
import locale
import threading
import folder_paths

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import numpy as np

# ExecutionBlocker lets a node skip everything downstream of it without an
# error. Older ComfyUI builds lack it; there we fall back to an exception.
try:
    from comfy_execution.graph import ExecutionBlocker
except ImportError:
    ExecutionBlocker = None

# TextFile input path is the 'text' folder inside ComfyUI's root folder.
supported_text_extensions = set(['.txt'])
text_dir = os.path.join(folder_paths.base_path, "text")

folder_paths.folder_names_and_paths["texts"] = ([text_dir], supported_text_extensions)

# Our base path.
base_dir = os.path.dirname( os.path.realpath(__file__) )

# Helper function that works out how a text file is encoded.
#
# Returns (codec to read the file with, codec to append to it with). Files
# written by Windows tools are not always UTF-8: PowerShell's '>' writes
# UTF-16 and Notepad used to write the ANSI code page.
def detect_encoding(data):
    if data.startswith(b'\xff\xfe'):
        return 'utf-16', 'utf-16-le'
    if data.startswith(b'\xfe\xff'):
        return 'utf-16', 'utf-16-be'
    if data.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig', 'utf-8'
    try:
        data.decode('utf-8')
        return 'utf-8', 'utf-8'
    except UnicodeDecodeError:
        fallback = locale.getpreferredencoding(False) or 'cp1252'
        return fallback, fallback

# Helper function that reads a text file whatever its encoding.
def read_text(path):
    with open(path, 'rb') as stream:
        data = stream.read()
    read_codec, append_codec = detect_encoding(data)
    return data.decode(read_codec, errors='replace'), append_codec

# Helper function to load a text file and split each line into a separate item.
def load_text(filename):
    path = os.path.join(base_dir, filename)

    # Load file contents
    content, _ = read_text(path)

    # Remove empty lines and trim white space
    items = [line.strip() for line in content.splitlines() if line.strip()]

    return items

# Helper function that fingerprints a file on disk.
#
# ComfyUI caches a node's output and only re-runs it when one of its widget
# values changes. Returning a content hash from IS_CHANGED makes the node
# re-run whenever the file itself is edited, even if the seed stayed the same.
def file_fingerprint(path):
    try:
        hasher = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        # Missing or unreadable: NaN never equals itself, so ComfyUI always
        # treats the node as changed and the real error surfaces on execution.
        return float("nan")

# ---------------------------------------------------------------------------
# Prompt capture
#
# Appends prompts to a file in the 'text' folder, so the Text File node can
# pick them up again. Used by the Text Capture node (auto mode) and by the
# /smart-prompting/capture endpoint behind its "Save to file" button.
# ---------------------------------------------------------------------------
_capture_lock = threading.Lock()

def prompt_to_line(text):
    """Text File reads one prompt per line, so line breaks become spaces."""
    return re.sub(r'\s*[\r\n]+\s*', ' ', text or '').strip()

def resolve_text_file(name):
    """Returns (absolute path, name relative to the text folder)."""
    name = (name or '').strip()
    if not name:
        raise ValueError("No file name given")
    if not name.lower().endswith('.txt'):
        name += '.txt'

    root = os.path.abspath(text_dir)
    path = os.path.abspath(os.path.join(root, name))
    if not os.path.normcase(path).startswith(os.path.normcase(root) + os.sep):
        raise ValueError(f"'{name}' is outside the text folder")

    return path, os.path.relpath(path, root)

def save_prompt(file, text):
    """Appends a prompt as a new line unless the file already has it.

    Returns a dict with 'status' (saved, duplicate, empty or error), the file
    name, the number of prompts in the file and its new fingerprint.
    """
    result = {"status": "error", "file": file, "count": 0, "created": False, "hash": None, "message": ""}

    try:
        path, relative = resolve_text_file(file)
    except ValueError as error:
        result["message"] = str(error)
        return result
    result["file"] = relative

    line = prompt_to_line(text)
    if not line:
        result["status"] = "empty"
        result["message"] = "Nothing to save, the prompt is empty"
        return result

    try:
        with _capture_lock:
            exists = os.path.exists(path)
            content, append_codec = read_text(path) if exists else ('', 'utf-8')
            lines = [item.strip() for item in content.splitlines() if item.strip()]

            if line in lines:
                result["status"] = "duplicate"
            else:
                # Keep whatever line ending the file already uses.
                if '\r\n' in content:
                    newline = '\r\n'
                elif '\n' in content:
                    newline = '\n'
                else:
                    newline = '\r\n' if os.name == 'nt' else '\n'

                chunk = line + newline
                if content and not content.endswith(('\n', '\r')):
                    chunk = newline + chunk

                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'ab') as stream:
                    stream.write(chunk.encode(append_codec, errors='replace'))

                lines.append(line)
                result["status"] = "saved"
                result["created"] = not exists

            result["count"] = len(lines)
            fingerprint = file_fingerprint(path)
            result["hash"] = fingerprint if isinstance(fingerprint, str) else None
    except OSError as error:
        result["status"] = "error"
        result["message"] = str(error)

    return result

# Helper function to load a JSON file containing prompt styles.
def load_styles(filename):
    path = os.path.join(base_dir, filename)

    try:
        with open(path, encoding='utf-8') as file:
            return json.load(file)
    except UnicodeDecodeError:
        with open(path, encoding='cp949') as file:
            return json.load(file)

# Helper function to return a list of available style names.
def get_style_names(filename):
    data = load_styles(filename)

    names = []
    for style in data:
        names.append(style["name"])

    return names


# ---------------------------------------------------------------------------
# Style catalog
#
# Styles live in the 'styles' sub folder, one JSON file per category. Each file
# holds a list of {"name", "prompt", "negative_prompt"} entries, the same format
# used by Fooocus / SDXL Prompt Styler. Drop in your own file and it shows up as
# a new category after a restart.
#
# The legacy 'sdxl_styles.json' in the node folder is still used as a fallback
# when the 'styles' folder is missing.
# ---------------------------------------------------------------------------
styles_dir = os.path.join(base_dir, "styles")

ALL_CATEGORIES = "all"

_catalog_cache = None

def _category_label(filename):
    # "04_art_movements.json" -> "art movements"
    label = os.path.splitext(filename)[0]
    label = re.sub(r'^\d+[_\-\s]*', '', label)
    label = label.replace('_', ' ').replace('-', ' ').strip()
    return label or os.path.splitext(filename)[0]

def get_style_catalog(refresh=False):
    """Returns an OrderedDict of {category name: [style, ...]}."""
    global _catalog_cache
    if _catalog_cache is not None and not refresh:
        return _catalog_cache

    catalog = {}
    if os.path.isdir(styles_dir):
        for filename in sorted(os.listdir(styles_dir)):
            if not filename.lower().endswith(".json"):
                continue
            try:
                data = load_styles(os.path.join(styles_dir, filename))
            except Exception as error:
                print(f"[smart-prompting] Could not load styles from {filename}: {error}")
                continue
            if isinstance(data, list) and data:
                catalog[_category_label(filename)] = data

    if not catalog:
        # Fallback to the original single file.
        try:
            catalog["sdxl styles"] = load_styles('sdxl_styles.json')
        except Exception as error:
            print(f"[smart-prompting] Could not load sdxl_styles.json: {error}")
            catalog["sdxl styles"] = []

    _catalog_cache = catalog
    return catalog

def get_category_names():
    return [ALL_CATEGORIES] + list(get_style_catalog().keys())

def get_all_style_names():
    """Every style name across all categories (the combo box needs the full
    list, the category widget only filters what is shown in the UI)."""
    names = []
    seen = set()
    for styles in get_style_catalog().values():
        for style in styles:
            name = style.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names

def find_style(style_name, category=ALL_CATEGORIES):
    """Look in the selected category first, then anywhere else."""
    catalog = get_style_catalog()

    ordered = []
    if category in catalog:
        ordered.append(catalog[category])
    ordered.extend(styles for name, styles in catalog.items() if name != category)

    for styles in ordered:
        for style in styles:
            if style.get("name") == style_name:
                return style
    return None

def apply_style(style, positive, negative):
    """Merges a style entry with the incoming prompts."""
    positive = positive or ""
    negative = negative or ""
    prompt = positive
    negative_prompt = negative

    if style is not None:
        template = (style.get("prompt") or "").strip()
        if "{prompt}" in template:
            prompt = template.replace("{prompt}", positive)
        elif template:
            # Styles without a placeholder (e.g. some Fooocus ones) get appended.
            prompt = (positive + ", " + template) if positive.strip() else template

        extra_negative = (style.get("negative_prompt") or "").strip()
        if extra_negative:
            negative_prompt = (negative.strip() + ", " + extra_negative) if negative.strip() else extra_negative

    return prompt, negative_prompt


class TextString:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"text": ("STRING", {"multiline": True})}}
    RETURN_TYPES = ("STRING","INT",)
    RETURN_NAMES = ("TEXT", "ITEM_COUNT",)
    FUNCTION = "set"

    CATEGORY = "text"

    def set(self, text):
        items = text.strip().split(",")
        return (text, len(items))
    
class TextAppend:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"text": ("STRING", {"forceInput": True}), "append": ("STRING", {"forceInput": True})}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "append"

    CATEGORY = "text"

    def append(self, text, append):
        text = text + ", " + append
        return (text, )
    
class TextSearchReplace:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"text": ("STRING", {"forceInput": True}), "search_replace": ("STRING", {"forceInput": True}), "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}), "divisor": ("INT", {"default": 1, "min": 1, "max": 0xffffffffffffffff})}}
    RETURN_TYPES = ("STRING","INT",)
    RETURN_NAMES = ("TEXT", "SEED",)
    FUNCTION = "action"

    CATEGORY = "text"

    def action(self, text, search_replace, seed, divisor):
        # Obtain search & replace items
        items = search_replace.strip().split(",")
        items = [s.strip() for s in items]

        # Perform search & replace
        index = seed
        count = len(items)
        if( count > 1 ):
            if( text.find( items[0] ) == -1 ):
                print(f"Warning: search string '{items[0]}' not found in body text")

            index = int( seed / divisor ) % count
            if( index > 0 ):
                text = text.replace( items[0], items[index] )

        print(f"Prompt after S&R:", text)

        return (text, seed,)
    
class TextFile:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"file": (folder_paths.get_filename_list("texts"), ), "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}), "divisor": ("INT", {"default": 1, "min": 1, "max": 0xffffffffffffffff}), "prefix": ("STRING", {"multiline": True}), "suffix": ("STRING", {"multiline": True}), "at_end": (["wrap", "stop"], {"default": "wrap", "tooltip": "wrap: start again at the first line. stop: produce nothing once every line has been used, and set the seed control to 'fixed' so 'Run (on change)' stops."})}, "hidden": {"unique_id": "UNIQUE_ID"}}
    RETURN_TYPES = ("STRING","INT","INT","STRING",)
    RETURN_NAMES = ("TEXT", "SEED", "ITEM_COUNT","FILENAME",)
    FUNCTION = "action"

    CATEGORY = "text"

    # Re-run whenever the file on disk changes, not just when a widget changes.
    @classmethod
    def IS_CHANGED(s, file, **kwargs):
        return file_fingerprint(os.path.join(text_dir, file))

    def action(self, file, seed, divisor, prefix, suffix, at_end="wrap", unique_id=None):
        # Load file contents
        path = os.path.join(text_dir, file)        
        items = load_text(path)
        count = len(items)
        
        print(f"Number of lines in file:", count)

        # Integer division: seed / divisor loses precision for large seeds.
        position = seed // divisor

        # Past the last line in "stop" mode: produce nothing and tell the
        # front end to freeze the seed, so 'Run (on change)' goes idle.
        if at_end == "stop" and position >= count:
            print(f"Text File: all {count} lines of '{file}' processed, stopping.")
            try:
                from server import PromptServer
                PromptServer.instance.send_sync("smart-prompting.textfile-end", {"node": unique_id})
            except Exception as error:
                print(f"Text File: could not notify the front end: {error}")
            if ExecutionBlocker is None:
                raise RuntimeError(f"Text File: all {count} lines of '{file}' have been processed.")
            blocked = ExecutionBlocker(None)
            return (blocked, blocked, blocked, blocked,)
        
        # Obtain text line
        text = ""
        if( count > 0 ):
            index = position % count
            text = items[index].strip()

        # Prefix and suffix
        if( prefix.strip() ):
            text = prefix.strip() + ", " + text
        
        if( suffix.strip() ):
            text = text + ", " + suffix.strip() 

        print(f"Text line from file:", text)

        stem = file.split(".")[0]

        return (text, seed, len(items), stem,)
    
class TextCapture:
    """Passes a prompt through and saves it to a file in the 'text' folder.

    In 'manual' mode nothing is written until the "Save to file" button on the
    node is clicked; the node only shows the prompt it received. In 'auto' mode
    every new prompt is appended as soon as the node runs.
    """
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "text": ("STRING", {"forceInput": True}),
            "file": ("STRING", {"default": "captured.txt", "tooltip": "File in the 'text' folder to add prompts to. Created when it does not exist yet."}),
            "mode": (["manual", "auto"], {"default": "manual", "tooltip": "manual: save with the button. auto: save every new prompt when the node runs."})}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("TEXT",)
    FUNCTION = "capture"

    # Runs even when nothing is connected to its output.
    OUTPUT_NODE = True

    CATEGORY = "text"

    def capture(self, text, file, mode):
        ui = {"text": [prompt_to_line(text)]}

        if mode == "auto":
            result = save_prompt(file, text)
            ui["saved"] = [result]
            print(f"[smart-prompting] Text Capture: {result['status']} '{result['file']}'", result["message"])

        return {"ui": ui, "result": (text,)}

class TextNegatives:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"clip": ("CLIP", )}}
    RETURN_TYPES = ("CONDITIONING","STRING","STRING",)
    FUNCTION = "encode"

    CATEGORY = "text"

    def encode(self, clip):
        text = "ugly, boring, lowres, bad quality, bad anatomy, extra limbs, extra legs, extra arms, deformed, mutilated, black and white, red eyes"
        tokens = clip.tokenize(text)
        cond, pooled = clip.encode_from_tokens(tokens, return_pooled=True)
        return ([[cond, {"pooled_output": pooled}]], text, text,)
    
class TextStyleSelector:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        # NOTE: 'style' stays the first widget so existing workflows keep loading
        # their saved style. 'category' only filters the list shown in the UI, so
        # the style combo must contain every style name for validation to pass.
        return {"required": {
            "style": (get_all_style_names(), ),
            "category": (get_category_names(), ),
            "positive": ("STRING", {"forceInput": True})},
            "optional": {
            "negative": ("STRING", {"forceInput": True, "tooltip": "Optional. Left unconnected, only the style's own negative prompt is output."})}}
    RETURN_TYPES = ("STRING","STRING",)
    RETURN_NAMES = ("POSITIVE", "NEGATIVE",)
    FUNCTION = "action"

    CATEGORY = "text"

    def action(self, style, positive, negative="", category=ALL_CATEGORIES):
        item = find_style(style, category)
        if item is None:
            print(f"[smart-prompting] Warning: style '{style}' not found, passing prompts through")

        prompt, negative_prompt = apply_style(item, positive, negative)

        print(f"Prompt:", prompt)
        print(f"Negative Prompt:", negative_prompt)

        return (prompt, negative_prompt, )
    
class TextCharacterSelector:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "prefix": ("STRING", {"multiline": True}),
            "type": (["feminine", "masculine"], ),
            "age": (["very young", "young", "teen", "young adult", "adult", "middle-aged", "old"], ),
            "color": (load_text('hair_colors.txt'), ),
            "style": (load_text('female_hair_styles.txt'), ),
            "camera": (load_text('camera_angles.txt'), ),
            "ethnic": (load_text('ethnic_styles.txt'), ),
            "postfix": ("STRING", {"multiline": True}),
             }}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("PROMPT",)
    FUNCTION = "action"

    CATEGORY = "text"

    def action(self, prefix, type, age, color, style, camera, ethnic, postfix):
        result = "(" + camera + ":1.2), " + prefix + " " + age + " (" + ethnic + " "

        if( type == "masculine" ):
            if( age == "very young" or age == "young" or age == "teen" ):
                result = result + "boy"
            else:
                result = result + "man"
        else:
            if( age == "very young" or age == "young" or age == "teen" ):
                result = result + "girl"
            else:
                result = result + "woman"

        result = result + ":1.5), " + color + ", " + style + ", " + postfix

        print(f"Character Prompt:", result)

        return (result, )

class TextEncodeReusable:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"clip": ("CLIP", ), "positive": ("STRING", {"forceInput": True})},
                "optional": {"negative": ("STRING", {"forceInput": True, "tooltip": "Optional. Left unconnected, an empty prompt is encoded."})}}
    RETURN_TYPES = ("CONDITIONING","CONDITIONING",)
    RETURN_NAMES = ("POSITIVE","NEGATIVE")
    FUNCTION = "encode"

    CATEGORY = "text"

    def encode(self, clip, positive, negative=""):
        tokens = clip.tokenize(positive)
        cond_pos, pooled_pos = clip.encode_from_tokens(tokens, return_pooled=True)
        tokens = clip.tokenize(negative or "")
        cond_neg, pooled_neg = clip.encode_from_tokens(tokens, return_pooled=True)
        return ([[cond_pos, {"pooled_output": pooled_pos}]], [[cond_neg, {"pooled_output": pooled_neg}]])
    
class SaveImageWithPrefix:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    {"images": ("IMAGE", ),
                     "filename_prefix": ("STRING", {"forceInput": True})},
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ()
    FUNCTION = "save_images"

    OUTPUT_NODE = True

    CATEGORY = "text"

    def save_images(self, images, filename_prefix, prompt=None, extra_pnginfo=None):
        filename_prefix += self.prefix_append
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()
        for image in images:
            i = 255. * image.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for x in extra_pnginfo:
                    metadata.add_text(x, json.dumps(extra_pnginfo[x]))

            file = f"{filename}_{counter:05}.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=4)
            results.append({
                "filename": file,
                "subfolder": subfolder,
                "type": self.type
            })
            counter += 1

        return { "ui": { "images": results } }
    
# ---------------------------------------------------------------------------
# Small API endpoint so the front end can filter the style list by category.
# ---------------------------------------------------------------------------
try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/smart-prompting/styles")
    async def _smart_prompting_styles(request):
        catalog = get_style_catalog(refresh=request.query.get("refresh") == "1")
        return web.json_response({
            "all": ALL_CATEGORIES,
            "categories": {name: [s.get("name") for s in styles] for name, styles in catalog.items()},
        })

    # Lets the front end notice that a text file was edited on disk. The
    # frontend's "Run (on change)" only diffs the serialized graph, so a file
    # edit is invisible to it until something reports that it happened.
    @PromptServer.instance.routes.get("/smart-prompting/text-file-hash")
    async def _smart_prompting_text_file_hash(request):
        name = request.query.get("file", "")
        if not name:
            return web.json_response({"error": "missing 'file' parameter"}, status=400)

        root = os.path.abspath(text_dir)
        path = os.path.abspath(os.path.join(root, name))
        if os.path.normcase(path) != os.path.normcase(root) and not os.path.normcase(path).startswith(os.path.normcase(root) + os.sep):
            return web.json_response({"error": "path outside the text folder"}, status=400)

        value = file_fingerprint(path)
        if not isinstance(value, str):
            # NaN is not valid JSON; report the file as unreadable instead.
            value = None
        return web.json_response({"file": name, "hash": value})
    # Saves a prompt for the "Save to file" button on the Text Capture node.
    @PromptServer.instance.routes.post("/smart-prompting/capture")
    async def _smart_prompting_capture(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "invalid request"}, status=400)

        result = save_prompt(str(body.get("file", "")), str(body.get("text", "")))
        if result["status"] == "saved":
            print(f"[smart-prompting] Text Capture: saved to '{result['file']}' ({result['count']} prompts)")
        return web.json_response(result, status=400 if result["status"] == "error" else 200)
except Exception as error:
    print(f"[smart-prompting] Style category endpoint not registered: {error}")


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "TextString": TextString,
    "TextAppend": TextAppend,
    "TextSearchReplace": TextSearchReplace,
    "TextFile": TextFile,
    "TextCapture": TextCapture,
    "TextNegatives": TextNegatives,
    "TextStyleSelector": TextStyleSelector,
    "TextCharacterSelector": TextCharacterSelector,
    "TextEncodeReusable": TextEncodeReusable,
    "SaveImageWithPrefix": SaveImageWithPrefix
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "TextString": "Text String",
    "TextAppend": "Text Append",
    "TextSearchReplace": "Text Search & Replace",
    "TextFile": "Text File",
    "TextCapture": "Text Capture",
    "TextNegatives": "Text Negatives",
    "TextStyleSelector": "Text Style Selector",
    "TextCharacterSelector": "Text Character Selector",
    "TextEncodeReusable": "Text Encode",
    "SaveImageWithPrefix": "Save Image with Prefix"
}