import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import telemetry, tools

log = logging.getLogger(__name__)


@dataclass
class StorageResult:
    test: str
    mb_per_sec: float
    iops: float
    latency_ms_mean: float
    latency_ms_p99: float


TEST_SPECS = {
    "seq_read":  ("read",     "1M",  1),
    "seq_write": ("write",    "1M",  1),
    "rand_read": ("randread", "4k",  16),
}


def _fio(target_dir: Path, size_gb: int, duration_s: int, test: str) -> StorageResult:
    fio = tools.require("fio")
    target_dir.mkdir(parents=True, exist_ok=True)
    test_file = target_dir / f"nasdiag_{test}.bin"
    rw, bs, iodepth = TEST_SPECS[test]
    cmd = [
        fio,
        f"--name={test}",
        f"--filename={test_file}",
        f"--size={size_gb}G",
        f"--rw={rw}",
        f"--bs={bs}",
        f"--iodepth={iodepth}",
        "--ioengine=posixaio",
        "--direct=1",
        "--fadvise_hint=0",
        f"--runtime={duration_s}",
        "--time_based",
        "--group_reporting",
        "--output-format=json",
    ]
    log.debug("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("fio stderr: %s", proc.stderr.strip())
        raise RuntimeError(f"fio failed ({test}): {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout)
    job = data["jobs"][0]
    side = "read" if "read" in rw else "write"
    s = job[side]
    return StorageResult(
        test=test,
        mb_per_sec=s["bw_bytes"] / 1e6,
        iops=s["iops"],
        latency_ms_mean=s["lat_ns"]["mean"] / 1e6,
        latency_ms_p99=s["clat_ns"]["percentile"].get("99.000000", 0) / 1e6,
    )


def _cleanup(target_dir: Path):
    for f in target_dir.glob("nasdiag_*.bin"):
        try:
            f.unlink()
        except OSError as e:
            log.warning("cleanup failed for %s: %s", f, e)


def _disk_free_gb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def run(target_dir: str, size_gb: int, duration_s: int, label: str,
        tests: list[str] | None = None, telemetry_host: str = "",
        nas_user: str = "", nas_key: str = "", nas_nic: str = "bond0") -> list[StorageResult]:
    target = Path(target_dir) / ".nasdiag-tmp"
    tests = tests or ["seq_read", "seq_write", "rand_read"]
    print(f"STORAGE ({label}) — {target}, {size_gb} GB, {duration_s}s/test")
    free_gb = _disk_free_gb(target.parent if target.parent.exists() else Path(target_dir))
    if free_gb is not None and free_gb < size_gb + 2:
        raise SystemExit(f"ERROR: only {free_gb:.1f} GB free at {target_dir}, need ~{size_gb + 2} GB")
    results = []
    try:
        for t in tests:
            with telemetry.measure(host=telemetry_host, nas_user=nas_user,
                                   nas_key=nas_key, nas_nic=nas_nic) as m:
                r = _fio(target, size_gb, duration_s, t)
            results.append(r)
            print(f"  {t:9s}  {r.mb_per_sec:8.1f} MB/s   "
                  f"{r.iops:8.0f} IOPS   "
                  f"lat mean {r.latency_ms_mean:6.2f} ms   "
                  f"p99 {r.latency_ms_p99:6.2f} ms")
            for line in m.summary_lines():
                print(f"            {line}")
    finally:
        _cleanup(target)
    return results
