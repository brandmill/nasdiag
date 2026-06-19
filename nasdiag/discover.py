from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MOUNT_RE = re.compile(r"^(.+?)\s+on\s+(.+?)\s+\((\w+)[,\s)]")
_SMB_SRC = re.compile(r"^//(?:[^@/]+@)?([^/]+)/")
_MDNS_SERVICE = re.compile(r"\._[a-z]+\._tcp\.local$", re.IGNORECASE)
_NFS_SRC = re.compile(r"^([^:]+):")

REMOTE_TYPES = {"smbfs", "nfs"}
LOCAL_TYPES = {"apfs", "hfs", "exfat", "msdos"}
SYSTEM_PATHS = ("/System/", "/private/", "/dev/", "/Volumes/Recovery")


@dataclass
class Mount:
    path: str
    type: str
    source: str
    host: str | None = None

    def label(self) -> str:
        host = f" → {self.host}" if self.host else ""
        return f"{self.path}  ({self.type}{host})"


def parse_mount(text: str | None = None) -> list[Mount]:
    if text is None:
        text = subprocess.check_output(["mount"], text=True)
    out = []
    for line in text.splitlines():
        m = _MOUNT_RE.match(line)
        if not m:
            continue
        source, path, fstype = m.group(1), m.group(2), m.group(3)
        host = None
        if fstype == "smbfs":
            h = _SMB_SRC.match(source)
            if h:
                host = _MDNS_SERVICE.sub(".local", h.group(1))
        elif fstype == "nfs":
            h = _NFS_SRC.match(source)
            if h:
                host = h.group(1)
        out.append(Mount(path=path, type=fstype, source=source, host=host))
    return out


def _boot_source() -> str:
    try:
        out = subprocess.check_output(["df", "/"], text=True).strip().splitlines()
        return out[-1].split()[0]
    except Exception:
        return ""


def list_shares() -> list[Mount]:
    return [m for m in parse_mount() if m.type in REMOTE_TYPES]


def list_externals() -> list[Mount]:
    boot = _boot_source()
    out = []
    for m in parse_mount():
        if m.type not in LOCAL_TYPES:
            continue
        if m.source == boot:
            continue
        if not m.path.startswith("/Volumes/"):
            continue
        if any(m.path.startswith(p) for p in SYSTEM_PATHS):
            continue
        out.append(m)
    return out


def pick(mounts: list[Mount], prompt: str, allow_skip: bool = False) -> Mount | None:
    if not mounts:
        print(f"{prompt}\n  (none found)")
        return None
    if not sys.stdin.isatty():
        log.warning("non-interactive shell and no path passed; defaulting to first match: %s",
                    mounts[0].path)
        return mounts[0]
    print(prompt)
    for i, m in enumerate(mounts, 1):
        print(f"  {i}. {m.label()}")
    if allow_skip:
        print("  0. skip")
    while True:
        choice = input("  > ").strip()
        if allow_skip and choice == "0":
            return None
        try:
            n = int(choice)
            if 1 <= n <= len(mounts):
                return mounts[n - 1]
        except ValueError:
            pass
        print("  invalid choice")


def pick_many(mounts: list[Mount], prompt: str, allow_skip: bool = False) -> list[Mount]:
    if not mounts:
        print(f"{prompt}\n  (none found)")
        return []
    if not sys.stdin.isatty():
        log.warning("non-interactive shell and no paths passed; selecting all: %s",
                    [m.path for m in mounts])
        return mounts
    print(prompt + "  (comma-separated, or 'a' for all)")
    for i, m in enumerate(mounts, 1):
        print(f"  {i}. {m.label()}")
    if allow_skip:
        print("  0. skip")
    while True:
        choice = input("  > ").strip().lower()
        if allow_skip and choice == "0":
            return []
        if choice in ("a", "all"):
            return mounts
        try:
            ids = [int(x) for x in choice.replace(" ", "").split(",") if x]
            picked = [mounts[n - 1] for n in ids if 1 <= n <= len(mounts)]
            if picked:
                return picked
        except (ValueError, IndexError):
            pass
        print("  invalid choice")
