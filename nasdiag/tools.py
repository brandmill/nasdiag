from __future__ import annotations

import shutil
import sys


HINTS = {
    "iperf3": {
        "darwin": "brew install iperf3",
        "linux": "apt install iperf3   # or: opkg install iperf3 (QNAP via Entware)",
        "win32": "choco install iperf3",
    },
    "fio": {
        "darwin": "brew install fio",
        "linux": "apt install fio",
        "win32": "choco install fio",
    },
}


def require(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    plat = sys.platform if sys.platform in HINTS[name] else "linux"
    print(f"ERROR: '{name}' not found on PATH. Install: {HINTS[name][plat]}")
    sys.exit(2)


def have(name: str) -> bool:
    return shutil.which(name) is not None
