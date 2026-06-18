import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import telemetry, tools

log = logging.getLogger(__name__)

LATENCY_SPIKE_MS = 100.0       # p99 above this = stall territory
THROUGHPUT_PLATEAU_RATIO = 1.2 # if N+1 doesn't gain ≥20%, we've plateaued at N


@dataclass
class RampPoint:
    n_workers: int
    mb_per_sec: float
    iops: float
    latency_ms_p99: float


def _fio_ramp(target_dir: Path, n: int, size_gb: int, duration_s: int) -> RampPoint:
    fio = tools.require("fio")
    test_file = target_dir / "nasdiag_concurrent.bin"
    cmd = [
        fio,
        f"--name=ramp_{n}",
        f"--filename={test_file}",
        f"--size={size_gb}G",
        "--rw=randread",
        "--bs=1M",
        "--iodepth=4",
        "--direct=1",
        "--fadvise_hint=0",
        "--ioengine=posixaio",
        f"--numjobs={n}",
        f"--runtime={duration_s}",
        "--time_based",
        "--group_reporting",
        "--output-format=json",
    ]
    log.debug("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("fio stderr: %s", proc.stderr.strip())
        raise RuntimeError(f"fio failed at N={n}: {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout)
    job = data["jobs"][0]
    r = job["read"]
    return RampPoint(
        n_workers=n,
        mb_per_sec=r["bw_bytes"] / 1e6,
        iops=r["iops"],
        latency_ms_p99=r["clat_ns"]["percentile"].get("99.000000", 0) / 1e6,
    )


def _verdict(points: list[RampPoint]) -> str:
    plateau_n = None
    for i in range(1, len(points)):
        if points[i].mb_per_sec < points[i - 1].mb_per_sec * THROUGHPUT_PLATEAU_RATIO:
            plateau_n = points[i - 1].n_workers
            break
    spike_n = next((p.n_workers for p in points if p.latency_ms_p99 > LATENCY_SPIKE_MS), None)
    parts = []
    if plateau_n is not None:
        parts.append(f"throughput plateaus around {plateau_n} worker(s)")
    else:
        parts.append("throughput scales across the tested range")
    if spike_n is not None:
        parts.append(f"p99 latency exceeds {LATENCY_SPIKE_MS:.0f} ms at {spike_n} worker(s)")
    return "→ " + "; ".join(parts)


def run(target_dir: str, size_gb: int, duration_s: int, max_workers: int,
        label: str, telemetry_host: str = "", nas_user: str = "",
        nas_key: str = "", nas_nic: str = "bond0") -> list[RampPoint]:
    target = Path(target_dir) / ".nasdiag-tmp"
    target.mkdir(parents=True, exist_ok=True)
    levels = [n for n in [1, 2, 4, 8, 16] if n <= max_workers]
    print(f"CONCURRENT ({label}) — randread 1M, ramping workers on {target}")
    points = []
    try:
        for n in levels:
            with telemetry.measure(host=telemetry_host, nas_user=nas_user,
                                   nas_key=nas_key, nas_nic=nas_nic) as m:
                p = _fio_ramp(target, n, size_gb, duration_s)
            points.append(p)
            print(f"  {n:2d} worker(s)  {p.mb_per_sec:8.1f} MB/s   "
                  f"{p.iops:8.0f} IOPS   p99 {p.latency_ms_p99:7.2f} ms")
            for line in m.summary_lines():
                print(f"            {line}")
    finally:
        for f in target.glob("nasdiag_*.bin"):
            try:
                f.unlink()
            except OSError as e:
                log.warning("cleanup failed for %s: %s", f, e)
    print("  " + _verdict(points))
    return points
