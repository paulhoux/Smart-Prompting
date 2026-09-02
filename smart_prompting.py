import os
import re
import json
import hashlib
import folder_paths

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import numpy as np

# TextFile input path is the 'text' folder inside ComfyUI's root folder.
supported_text_extensions = set(['.txt'])
text_dir = os.path.join(folder_paths.base_path, "text")

folder_paths.folder_names_and_paths["texts"] = ([text_dir], supported_text_extensions)

# Our base path.
base_dir = os.path.dirname( os.path.realpath(__file__) )

# Helper function to load a text file and split each line into a separate item.
def load_text(filename):
    path = os.path.join(base_dir, filename)

    # Load file contents
    stream = open( path, 'r')
    lines = stream.readlines()

    # Remove empty lines
    items = list(filter(lambda x: x.strip() != '', lines))

    # Trim white space
    items = [item.strip() for item in items]

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
        return {"required": {"file": (folder_paths.get_filename_list("texts"), ), "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}), "divisor": ("INT", {"default": 1, "min": 1, "max": 0xffffffffffffffff}), "prefix": ("STRING", {"multiline": True}), "suffix": ("STRING", {"multiline": True})}}
    RETURN_TYPES = ("STRING","INT","INT","STRING",)
    RETURN_NAMES = ("TEXT", "SEED", "ITEM_COUNT","FILENAME",)
    FUNCTION = "action"

    CATEGORY = "text"

    # Re-run whenever the file on disk changes, not just when a widget changes.
    @classmethod
    def IS_CHANGED(s, file, **kwargs):
        return file_fingerprint(os.path.join(text_dir, file))

    def action(self, file, seed, divisor, prefix, suffix):
        # Load file contents
        path = os.path.join(text_dir, file)        
        items = load_text(path)
        
        print(f"Number of lines in file:", len(items))
        
        # Obtain text line
        text = ""
        if( len(items) > 0 ):
            index = int( seed / divisor ) % len( items )
            text = items[index].strip()

        # Prefix and suffix
        if( prefix.strip() ):
            text = prefix.strip() + ", " + text
        
        if( suffix.strip() ):
            text = text + ", " + suffix.strip() 

        print(f"Text line from file:", text)

        stem = file.split(".")[0]

        return (text, seed, len(items), stem,)
    
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
            "positive": ("STRING", {"forceInput": True}),
            "negative": ("STRING", {"forceInput": True})}}
    RETURN_TYPES = ("STRING","STRING",)
    RETURN_NAMES = ("POSITIVE", "NEGATIVE",)
    FUNCTION = "action"

    CATEGORY = "text"

    def action(self, style, positive, negative, category=ALL_CATEGORIES):
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
        return {"required": {"clip": ("CLIP", ), "positive": ("STRING", {"forceInput": True}), "negative": ("STRING", {"forceInput": True})}}
    RETURN_TYPES = ("CONDITIONING","CONDITIONING",)
    RETURN_NAMES = ("POSITIVE","NEGATIVE")
    FUNCTION = "encode"

    CATEGORY = "text"

    def encode(self, clip, positive, negative):
        tokens = clip.tokenize(positive)
        cond_pos, pooled_pos = clip.encode_from_tokens(tokens, return_pooled=True)
        tokens = clip.tokenize(negative)
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
except Exception as error:
    print(f"[smart-prompting] Style category endpoint not registered: {error}")


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "TextString": TextString,
    "TextAppend": TextAppend,
    "TextSearchReplace": TextSearchReplace,
    "TextFile": TextFile,
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
    "TextNegatives": "Text Negatives",
    "TextStyleSelector": "Text Style Selector",
    "TextCharacterSelector": "Text Character Selector",
    "TextEncodeReusable": "Text Encode",
    "SaveImageWithPrefix": "Save Image with Prefix"
}