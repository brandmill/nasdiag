from __future__ import annotations

import re
import shutil
import socket
import sys


_HOSTNAME_OK = re.compile(r"^[a-zA-Z0-9._-]+$")


def resolve_host(host: str) -> tuple[str, str | None]:
    """Resolve a host, falling back to <host>.local if it has no dots.
    Returns (host_to_use, error_message). error_message is None on success.
    """
    if not host:
        return host, "host is empty"
    if not _HOSTNAME_OK.match(host):
        return host, f"invalid hostname: {host!r}"
    try:
        socket.gethostbyname(host)
        return host, None
    except socket.gaierror:
        pass
    if "." not in host:
        local = host + ".local"
        try:
            socket.gethostbyname(local)
            return local, None
        except socket.gaierror:
            return host, f"could not resolve {host!r} or {local!r}"
    return host, f"could not resolve {host!r}"


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> str | None:
    """Returns None if a TCP connection succeeds, else an error string."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except (socket.gaierror, socket.timeout, ConnectionRefusedError, OSError) as e:
        return str(e)


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
