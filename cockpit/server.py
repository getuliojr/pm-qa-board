"""
cockpit/server.py - Backward-compatibility shim.
The application package was renamed from 'cockpit' to 'board'.
This shim delegates to 'board.server'.
"""

from __future__ import annotations

from pathlib import Path
import runpy
import sys

_board_dir = str(Path(__file__).resolve().parent.parent / "board")
if _board_dir not in sys.path:
    sys.path.insert(0, _board_dir)

_BOARD_SERVER = Path(_board_dir) / "server.py"

if __name__ == "__main__":
    runpy.run_path(str(_BOARD_SERVER), run_name="__main__")
else:
    from server import *  # noqa: F401, F403
