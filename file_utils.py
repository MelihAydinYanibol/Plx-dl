import os
import re
import shutil

# Characters Windows doesn't allow in file/folder names
_SEPARATORS = re.compile(r'[/\\|]')
_INVALID = re.compile(r'[<>"?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

def safe_name(name, fallback="Unknown"):
    """Makes a string safe to use as a Windows file or folder name."""
    name = _SEPARATORS.sub("-", str(name)).replace(":", " -")
    name = _INVALID.sub("", name)
    name = " ".join(name.split())[:150].rstrip(" .")
    if not name:
        return fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name

def move_file(src, target_dir, filename):
    """Moves src into target_dir/filename, replacing a file that's already there. Returns the new path."""
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, filename)
    if os.path.exists(dest):
        print(f"♻️ Replacing existing file: {dest}")
        os.remove(dest)
    shutil.move(src, dest)
    return dest
