"""Native folder picker for local single-user runs (tkinter, runs in a worker thread)."""
from __future__ import annotations

from pathlib import Path

_TITLES = {
    'project_create': '选择新项目的存放位置（会在其中创建 <项目名>.slproj）',
    'project_open': '选择要打开的项目目录（*.slproj）',
    'import_root': '选择要导入的图片目录',
}


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


def available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False
