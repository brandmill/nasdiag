from __future__ import annotations

"""Mac-side system profile — catches client-side bottlenecks that look like NAS issues.

Each parser is its own small function so any one broken parser is easy to find/fix.
On non-macOS, collect() returns MacProfile(available=False) and the rest of the
pipeline silently skips this section.
"""
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# (display label, process basename to look for in `ps`)
SYNC_APPS = [
    ("Dropbox", "Dropbox"),
    ("OneDrive", "OneDrive"),
    ("Google Drive", "Google Drive"),
    ("iCloud (bird)", "bird"),
    ("Backblaze (bzbmenu)", "bzbmenu"),
    ("Backblaze (bzfilelist)", "bzfilelist"),
    ("Carbon Copy Cloner", "ccc"),
]


@dataclass
class MacProfile:
    available: bool = True
    macos_version: str = ""
    chip: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    free_ram_pct: float = 0.0
    swap_used_gb: float = 0.0
    internal_ssd_total_gb: float = 0.0
    internal_ssd_free_gb: float = 0.0
    internal_ssd_pct_used: float = 0.0
    thermal_speed_limit: int = 100
    on_battery: bool = False
    low_power_mode: bool = False
    active_syncs: list[str] = field(default_factory=list)
    time_machine_running: bool = False
    top_cpu: list[tuple[str, float]] = field(default_factory=list)
    spotlight_share_state: dict[str, str] = field(default_factory=dict)


def _run(cmd: list[str], timeout: float = 3) -> str:
    try:
        return subprocess.check_output(
            cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        log.debug("profile: %s failed: %s", cmd[0], e)
        return ""


def _macos_version() -> str:
    return _run(["sw_vers", "-productVersion"])


def _chip() -> str:
    return _run(["sysctl", "-n", "machdep.cpu.brand_string"])


def _cpu_cores() -> int:
    try:
        return int(_run(["sysctl", "-n", "hw.ncpu"]) or "0")
    except ValueError:
        return 0


def _ram_gb() -> float:
    try:
        return int(_run(["sysctl", "-n", "hw.memsize"]) or "0") / 1e9
    except ValueError:
        return 0.0


def _free_ram_pct() -> float:
    out = _run(["memory_pressure"], timeout=4)
    m = re.search(r"System-wide memory free percentage:\s*(\d+)%", out)
    return float(m.group(1)) if m else 0.0


def _swap_used_gb() -> float:
    out = _run(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)([MG])", out)
    if not m:
        return 0.0
    n = float(m.group(1))
    return n / 1024.0 if m.group(2) == "M" else n


def _internal_ssd() -> tuple[float, float, float]:
    """returns (total_gb, free_gb, pct_used). Data volume preferred."""
    for path in ("/System/Volumes/Data", "/"):
        out = _run(["df", "-k", path])
        if not out:
            continue
        lines = out.splitlines()
        if len(lines) < 2:
            continue
        parts = lines[-1].split()
        try:
            total_kb = int(parts[1])
            avail_kb = int(parts[3])
            total_gb = total_kb / 1e6
            free_gb = avail_kb / 1e6
            pct_used = 100.0 * (1 - avail_kb / total_kb) if total_kb else 0.0
            return total_gb, free_gb, pct_used
        except (ValueError, IndexError):
            continue
    return 0.0, 0.0, 0.0


def _thermal_speed_limit() -> int:
    out = _run(["pmset", "-g", "therm"])
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else 100


def _on_battery() -> bool:
    return "Battery Power" in _run(["pmset", "-g", "batt"])


def _low_power_mode() -> bool:
    out = _run(["pmset", "-g"])
    m = re.search(r"lowpowermode\s+(\d+)", out)
    return bool(m and m.group(1) == "1")


def _active_syncs() -> list[str]:
    out = _run(["ps", "-Aco", "comm"], timeout=4)
    running = set(line.strip() for line in out.splitlines())
    return [label for (label, proc) in SYNC_APPS if proc in running]


def _time_machine_running() -> bool:
    out = _run(["tmutil", "status"], timeout=4)
    return "Running = 1" in out or '"Running" = 1' in out


def _top_cpu(limit: int = 5) -> list[tuple[str, float]]:
    out = _run(["ps", "-Aceo", "pcpu,comm", "-r"], timeout=4)
    results = []
    for line in out.splitlines()[1:]:  # skip header
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pct = float(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if pct < 1.0:
            continue
        if cmd in ("kernel_task", "WindowServer"):
            continue
        results.append((cmd, pct))
        if len(results) >= limit:
            break
    return results


def _spotlight_state(paths: list[str]) -> dict[str, str]:
    out = {}
    for p in paths:
        s = _run(["mdutil", "-s", p])
        m = re.search(r"Indexing (enabled|disabled)", s)
        if m:
            out[p] = m.group(1)
    return out


def collect(share_paths: list[str] | None = None) -> MacProfile:
    if sys.platform != "darwin":
        return MacProfile(available=False)
    p = MacProfile()
    p.macos_version = _macos_version()
    p.chip = _chip()
    p.cpu_cores = _cpu_cores()
    p.ram_gb = _ram_gb()
    p.free_ram_pct = _free_ram_pct()
    p.swap_used_gb = _swap_used_gb()
    p.internal_ssd_total_gb, p.internal_ssd_free_gb, p.internal_ssd_pct_used = _internal_ssd()
    p.thermal_speed_limit = _thermal_speed_limit()
    p.on_battery = _on_battery()
    p.low_power_mode = _low_power_mode()
    p.active_syncs = _active_syncs()
    p.time_machine_running = _time_machine_running()
    p.top_cpu = _top_cpu()
    p.spotlight_share_state = _spotlight_state(share_paths or [])
    return p


# ---- verdict helpers used by report.py -------------------------------------

def profile_warnings(p: MacProfile) -> list[str]:
    """Lines starting with ⚠ that the report verdict will use."""
    if not p.available:
        return []
    out = []
    if p.internal_ssd_pct_used > 85:
        out.append(f"⚠ MAC: internal SSD {p.internal_ssd_pct_used:.0f}% full — "
                   "APFS slows above 85%; clear space.")
    if p.free_ram_pct and p.free_ram_pct < 10:
        out.append(f"⚠ MAC: only {p.free_ram_pct:.0f}% RAM free — memory pressure likely.")
    if p.swap_used_gb > 1.0:
        out.append(f"⚠ MAC: {p.swap_used_gb:.1f} GB swap in use — swapping kills latency.")
    if p.thermal_speed_limit < 100:
        out.append(f"⚠ MAC: thermal throttling active (CPU speed limit {p.thermal_speed_limit}%).")
    if p.on_battery and not p.low_power_mode:
        out.append("⚠ MAC: running on battery — CPU/GPU may be capped, plug into AC.")
    if p.low_power_mode:
        out.append("⚠ MAC: Low Power Mode enabled — CPU is capped, disable for edits.")
    if p.active_syncs:
        out.append(f"⚠ MAC: cloud sync running: {', '.join(p.active_syncs)} — pause and re-test.")
    if p.time_machine_running:
        out.append("⚠ MAC: Time Machine backup in progress — eats disk + NIC bandwidth.")
    for share, state in p.spotlight_share_state.items():
        if state == "enabled":
            out.append(f"⚠ MAC: Spotlight indexing on {share} — disable for project shares: "
                       f"`sudo mdutil -i off \"{share}\"`")
    return out


def profile_passes(p: MacProfile) -> list[str]:
    """The healthy/info lines for the profile section."""
    if not p.available:
        return []
    out = []
    out.append(f"  macOS:       {p.macos_version or '?'}")
    out.append(f"  chip:        {p.chip or '?'}  ({p.cpu_cores} cores)")
    out.append(f"  RAM:         {p.ram_gb:.0f} GB   free: {p.free_ram_pct:.0f}%   swap: {p.swap_used_gb:.1f} GB")
    out.append(f"  internal SSD: {p.internal_ssd_free_gb:.0f} GB free / {p.internal_ssd_total_gb:.0f} GB "
               f"({p.internal_ssd_pct_used:.0f}% used)")
    therm_state = "nominal" if p.thermal_speed_limit == 100 else f"throttled to {p.thermal_speed_limit}%"
    power_state = "battery" if p.on_battery else "AC"
    if p.low_power_mode:
        power_state += " · LOW POWER"
    out.append(f"  thermal:     {therm_state}    power: {power_state}")
    if p.active_syncs:
        out.append(f"  cloud sync:  {', '.join(p.active_syncs)}")
    if p.top_cpu:
        top = ", ".join(f"{c} {pct:.0f}%" for c, pct in p.top_cpu[:3])
        out.append(f"  top CPU:     {top}")
    return out
