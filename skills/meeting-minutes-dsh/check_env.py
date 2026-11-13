#!/usr/bin/env python
"""Preflight for the meeting-minutes DSH skill.

Checks the Python environment and ffmpeg. It never downloads or installs
anything; it only reports what is missing and how to install it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys

CHECKS = [
    ("torch", "torch", "torch"),
    ("funasr", "funasr", "funasr>=1.2"),
    ("sklearn", "scikit-learn", "scikit-learn"),
    ("soundfile", "soundfile", "soundfile"),
    ("numpy", "numpy", "numpy"),
]


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        os.path.join(sys.prefix, "bin", "ffmpeg"),
        os.path.join(sys.prefix, "Library", "bin", "ffmpeg.exe"),
        os.path.join(sys.prefix, "Scripts", "ffmpeg.exe"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def main() -> int:
    print(f"Python: {sys.executable}")
    print(f"Version: {sys.version.split()[0]}")
    print()

    missing = []
    for module, package, pip_name in CHECKS:
        ok = has_module(module)
        print(f"{'OK  ' if ok else 'MISS'}  {package}")
        if not ok:
            missing.append(pip_name)

    ffmpeg = find_ffmpeg()
    print(f"{'OK  ' if ffmpeg else 'MISS'}  ffmpeg" + (f"  ({ffmpeg})" if ffmpeg else ""))

    print()
    if not missing and ffmpeg:
        print("Result: ready")
        return 0

    print("Result: not ready")
    if missing:
        quoted = " ".join(f'"{package}"' for package in missing)
        venv_python = ".venv/bin/python" if os.name != "nt" else ".venv\\Scripts\\python.exe"
        print("\nInstall the Python packages into the same interpreter:")
        print(f'  "{sys.executable}" -m pip install {quoted}')
        print("\nOr with uv (from the one-mic-minutes repo):")
        print(f'  uv pip install --python "{venv_python}" {quoted}')
    if not ffmpeg:
        print("\nInstall ffmpeg and put it on PATH:")
        if os.name == "nt":
            print("  winget install Gyan.FFmpeg")
            print("  # or put ffmpeg.exe under the venv's Library\\bin")
        else:
            print("  sudo apt install ffmpeg        # Debian / Ubuntu")
            print("  brew install ffmpeg            # macOS")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
