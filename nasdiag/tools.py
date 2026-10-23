from __future__ import annotations

import re
import json
import os
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


def pick_reachable_addr(host: str, port: int, timeout: float = 1.5) -> str | None:
    """Resolve host and return the first address that accepts a TCP
    connection on port, IPv4 candidates first. Multi-homed NAS boxes
    advertise every NIC over mDNS — including link-local IPv6 without a
    zone id, which tools like iperf3 try first and fail with 'No route
    to host'. Returns None if nothing connects."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    addrs: list[str] = []
    for fam, _, _, _, sa in sorted(infos, key=lambda i: i[0] != socket.AF_INET):
        if sa[0] not in addrs:
            addrs.append(sa[0])
    for addr in addrs:
        try:
            with socket.create_connection((addr, port), timeout=timeout):
                return addr
        except OSError:
            continue
    return None


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


def fio_engine() -> str:
    """fio's async I/O engine for this platform (posixaio does not exist on Windows)."""
    return "windowsaio" if sys.platform == "win32" else "posixaio"


def fio_platform_args() -> list[str]:
    """Windows fio has no process-shared mutexes and prints a warning (on
    stdout, ahead of the JSON) unless told to use threads."""
    return ["--thread"] if sys.platform == "win32" else []


def prefill_for_fio(path, size_gb: int) -> None:
    """Windows only: write fio's test file ourselves before fio runs. fio's own
    layout step crawled over SMB on Windows (1 GB took 2-3.5 min, so a 15 s
    test took minutes); big sequential writes take seconds. fio then finds a
    file of the right size and skips its layout. Random data, so nothing on
    the way can compress it."""
    if sys.platform != "win32":
        return
    want = size_gb << 30
    try:
        if os.path.getsize(path) >= want:
            return
    except OSError:
        pass
    block = os.urandom(8 << 20)
    with open(path, "wb") as f:
        for _ in range(want // len(block)):
            f.write(block)


def fio_json(stdout: str):
    """fio's JSON, ignoring any warning lines printed before it."""
    start = stdout.find("{")
    return json.loads(stdout[start:] if start >= 0 else stdout)


def fio_filename(path) -> str:
    """fio treats ':' as a separator between several files, so a Windows drive
    letter ("E:\\x") must be escaped ("E\\:\\x")."""
    s = str(path)
    return s.replace(":", "\\:") if sys.platform == "win32" else s


def require(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    plat = sys.platform if sys.platform in HINTS[name] else "linux"
    print(f"ERROR: '{name}' not found on PATH. Install: {HINTS[name][plat]}")
    sys.exit(2)


def have(name: str) -> bool:
    return shutil.which(name) is not None
