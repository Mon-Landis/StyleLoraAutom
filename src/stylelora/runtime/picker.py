"""Native picker for local single-user runs (tkinter, runs in a worker thread)."""
from __future__ import annotations

from pathlib import Path

FOLDER = 'folder'
FILES = 'files'

_TITLES = {
    'project_create': '选择新项目的存放位置（会在其中创建 <项目名>.slproj）',
    'project_open': '选择要打开的项目目录（*.slproj）',
    'import_root': '选择要导入的图片目录',
}
# PNG only: the dialog filter keeps other picks out of reach and matches
# the extractor's contract (non-PNG files are not processed either way).
_IMAGE_TYPES = [('PNG 图片', '*.png')]


def tk_picker(purpose: str) -> Path | None:
    """Show a modal directory dialog; returns None when the user cancels."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes('-topmost', True)  # browser window would otherwise cover it
        chosen = filedialog.askdirectory(parent=root, title=_TITLES.get(purpose, '选择目录'), mustexist=True)
    finally:
        root.destroy()
    return Path(chosen).absolute() if chosen else None


def tk_image_picker(source: str):
    """Pick images (multi-select) or a folder; returns None when canceled.

    ``source='files'`` returns a tuple of absolute image paths, ``source='folder'``
    returns one absolute directory path. If Tk cannot initialise or the dialog
    fails, the error propagates so the caller can map it to picker:unavailable.
    """
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes('-topmost', True)
        if source == FILES:
            chosen = filedialog.askopenfilenames(
                parent=root, title='选择 NAI 图片（可多选）', filetypes=_IMAGE_TYPES)
            # tkinter interns names in Tcl; an empty tuple is falsy on every platform.
            names = tuple(chosen or ())
            return tuple(Path(item).absolute() for item in names) if names else None
        chosen = filedialog.askdirectory(
            parent=root, title='选择包含 NAI 图片的目录', mustexist=True)
    finally:
        root.destroy()
    return Path(chosen).absolute() if chosen else None


def available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False
