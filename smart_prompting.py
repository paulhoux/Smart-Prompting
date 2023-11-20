import os
import json
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


class TextString:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"text": ("STRING", {"multiline": True})}}
    RETURN_TYPES = ("TEXT","INT",)
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
        return {"required": {"text": ("TEXT",), "append": ("TEXT",)}}
    RETURN_TYPES = ("TEXT",)
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
        return {"required": {"text": ("TEXT", ), "search_replace": ("TEXT", ), "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}), "divisor": ("INT", {"default": 1, "min": 1, "max": 0xffffffffffffffff})}}
    RETURN_TYPES = ("TEXT","INT",)
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
    RETURN_TYPES = ("TEXT","INT","INT","STRING",)
    RETURN_NAMES = ("TEXT", "SEED", "ITEM_COUNT","FILENAME",)
    FUNCTION = "action"

    CATEGORY = "text"

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
    RETURN_TYPES = ("CONDITIONING","TEXT","TEXT",)
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
        return {"required": {"style": (get_style_names('sdxl_styles.json'), ), "positive": ("TEXT", ), "negative": ("TEXT", )}}
    RETURN_TYPES = ("TEXT","TEXT",)
    RETURN_NAMES = ("POSITIVE", "NEGATIVE",)
    FUNCTION = "action"

    CATEGORY = "text"

    def action(self, style, positive, negative):
        # Obtain styles
        styles = load_styles('sdxl_styles.json')

        # Select style (zero based)
        prompt = positive
        negative_prompt = negative

        for item in styles:
            if( item["name"] == style ):
                # Replace prompt
                prompt = item["prompt"]
                negative_prompt = item["negative_prompt"]

                prompt = prompt.replace("{prompt}",positive)
                negative_prompt = negative + ", " + negative_prompt
                
                break

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
    RETURN_TYPES = ("TEXT",)
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
        return {"required": {"clip": ("CLIP", ), "positive": ("TEXT",), "negative": ("TEXT",)}}
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
                     "filename_prefix": ("TEXT", )},
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