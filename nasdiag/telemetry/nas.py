import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Remote sampler — busybox-sh-safe, prints one key=val line per second.
# Emits raw cumulative counters; Python computes deltas / rates.
_REMOTE_SAMPLER = r"""
NIC=${NASDIAG_NIC:-bond0}
emit() {
  RX=$(cat /sys/class/net/$NIC/statistics/rx_bytes 2>/dev/null || echo 0)
  TX=$(cat /sys/class/net/$NIC/statistics/tx_bytes 2>/dev/null || echo 0)
  CPU=$(awk 'NR==1{print $2+$3+$4+$5+$6+$7+$8+$9+$10+$11}' /proc/stat)
  IDLE=$(awk 'NR==1{print $5+$6}' /proc/stat)
  MT=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo)
  MA=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo)
  DISKS=$(awk '/ md[0-9]+ /{printf "%s:%s,",$3,$13}' /proc/diskstats)
  echo "T=$(date +%s) NIC=$NIC RX=$RX TX=$TX CPU=$CPU IDLE=$IDLE MT=$MT MA=$MA DISKS=$DISKS"
}
emit
while sleep 1; do emit; done
"""


@dataclass
class NasSample:
    t: int
    rx_mbps: float
    tx_mbps: float
    cpu_pct: float
    mem_pct: float
    disk_util_pct: float  # busiest md* device
    busiest_md: str


@dataclass
class _Raw:
    t: int
    rx: int
    tx: int
    cpu: int
    idle: int
    mt: int
    ma: int
    disks: dict[str, int] = field(default_factory=dict)


def _parse(line: str) -> _Raw | None:
    try:
        d = dict(re.findall(r"(\w+)=([^\s]+)", line))
        disks = {}
        if d.get("DISKS"):
            for pair in d["DISKS"].rstrip(",").split(","):
                if ":" in pair:
                    name, val = pair.split(":", 1)
                    disks[name] = int(val)
        return _Raw(
            t=int(d["T"]), rx=int(d["RX"]), tx=int(d["TX"]),
            cpu=int(d["CPU"]), idle=int(d["IDLE"]),
            mt=int(d["MT"]), ma=int(d["MA"]), disks=disks,
        )
    except (KeyError, ValueError) as e:
        log.debug("nas line parse failed: %s | %s", e, line.strip()[:200])
        return None


def _delta_to_sample(prev: _Raw, cur: _Raw) -> NasSample | None:
    dt = max(cur.t - prev.t, 1)
    dcpu = cur.cpu - prev.cpu
    didle = cur.idle - prev.idle
    cpu_pct = 100.0 * (dcpu - didle) / dcpu if dcpu > 0 else 0.0
    mem_pct = 100.0 * (cur.mt - cur.ma) / cur.mt if cur.mt > 0 else 0.0
    rx_mbps = 8.0 * (cur.rx - prev.rx) / dt / 1e6
    tx_mbps = 8.0 * (cur.tx - prev.tx) / dt / 1e6
    busiest_name = ""
    busiest_util = 0.0
    for name, ms in cur.disks.items():
        if name not in prev.disks:
            continue
        d_ms = ms - prev.disks[name]
        util = 100.0 * d_ms / (dt * 1000)
        if util > busiest_util:
            busiest_util = util
            busiest_name = name
    return NasSample(
        t=cur.t,
        rx_mbps=max(rx_mbps, 0.0),
        tx_mbps=max(tx_mbps, 0.0),
        cpu_pct=max(cpu_pct, 0.0),
        mem_pct=mem_pct,
        disk_util_pct=min(busiest_util, 100.0),
        busiest_md=busiest_name,
    )


class NasSampler:
    def __init__(self, host: str, user: str = "", key_file: str = "", nic: str = "bond0"):
        self.host = host
        self.user = user
        self.key_file = key_file
        self.nic = nic
        self.samples: list[NasSample] = []
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None

    def start(self):
        if not self.user or not self.host:
            log.debug("nas telemetry disabled (user/host missing)")
            return
        target = f"{self.user}@{self.host}"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
        if self.key_file:
            cmd += ["-i", self.key_file]
        cmd += [target, f"NASDIAG_NIC={self.nic} sh -s"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert self._proc.stdin is not None
            self._proc.stdin.write(_REMOTE_SAMPLER)
            self._proc.stdin.close()
        except (OSError, BrokenPipeError) as e:
            self.error = f"ssh launch failed: {e}"
            log.error(self.error)
            return
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        log.debug("nas sampler started: %s nic=%s", target, self.nic)

    def _reader(self):
        assert self._proc and self._proc.stdout
        prev: _Raw | None = None
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            raw = _parse(line)
            if not raw:
                continue
            if prev is not None:
                s = _delta_to_sample(prev, raw)
                if s:
                    self.samples.append(s)
            prev = raw

    def stop(self):
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                self._proc.kill()
            if not self.samples and self._proc.stderr:
                err = self._proc.stderr.read().strip()
                if err and not self.error:
                    self.error = err
        if self._thread:
            self._thread.join(timeout=2)

    def summary_line(self) -> str | None:
        if self.error and not self.samples:
            return f"nas:    (telemetry unavailable: {self.error[:120]})"
        if not self.samples:
            return None
        cpu_avg = sum(s.cpu_pct for s in self.samples) / len(self.samples)
        cpu_max = max(s.cpu_pct for s in self.samples)
        rx_max = max(s.rx_mbps for s in self.samples)
        tx_max = max(s.tx_mbps for s in self.samples)
        disk_max = max(s.disk_util_pct for s in self.samples)
        busiest = max(self.samples, key=lambda s: s.disk_util_pct).busiest_md or "—"
        return (f"nas:    cpu avg {cpu_avg:4.0f}% / peak {cpu_max:3.0f}%   "
                f"rx peak {rx_max:6.0f} Mbps   tx peak {tx_max:6.0f} Mbps   "
                f"{busiest} util peak {disk_max:3.0f}%")
