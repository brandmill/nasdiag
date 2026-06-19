from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False
    log.warning("psutil not installed — client telemetry disabled. Run: pip3 install psutil")


@dataclass
class Sample:
    t: float
    cpu_pct: float
    mem_pct: float
    net_rx_mbps: float
    net_tx_mbps: float
    thermal_limit: int  # 100 = nominal, <100 = throttled


def _nic_for_host(host: str) -> str | None:
    if not host:
        return None
    try:
        out = subprocess.check_output(["route", "-n", "get", host], text=True, timeout=2)
        m = re.search(r"interface:\s+(\S+)", out)
        return m.group(1) if m else None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _thermal_limit() -> int:
    if sys.platform != "darwin":
        return 100
    try:
        out = subprocess.check_output(["pmset", "-g", "therm"], text=True, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return 100
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else 100


class Sampler:
    def __init__(self, host: str = "", interval_s: float = 1.0):
        self.host = host
        self.interval_s = interval_s
        self.samples: list[Sample] = []
        self.nic: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def start(self):
        if not HAVE_PSUTIL:
            return
        self.nic = _nic_for_host(self.host) if self.host else None
        log.debug("client telemetry: nic=%s interval=%ss", self.nic, self.interval_s)
        self._t0 = time.monotonic()
        psutil.cpu_percent(interval=None)  # prime
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 1)

    def _loop(self):
        prev = None
        if self.nic:
            counters = psutil.net_io_counters(pernic=True).get(self.nic)
            prev = counters
        prev_t = time.monotonic()
        while not self._stop.wait(self.interval_s):
            now = time.monotonic()
            dt = max(now - prev_t, 1e-6)
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            rx_mbps = tx_mbps = 0.0
            if self.nic:
                cur = psutil.net_io_counters(pernic=True).get(self.nic)
                if cur and prev:
                    rx_mbps = 8 * (cur.bytes_recv - prev.bytes_recv) / dt / 1e6
                    tx_mbps = 8 * (cur.bytes_sent - prev.bytes_sent) / dt / 1e6
                prev = cur
            self.samples.append(Sample(
                t=now - self._t0,
                cpu_pct=cpu, mem_pct=mem,
                net_rx_mbps=rx_mbps, net_tx_mbps=tx_mbps,
                thermal_limit=_thermal_limit(),
            ))
            prev_t = now

    def summary_line(self) -> str | None:
        if not self.samples:
            return None
        cpu_avg = sum(s.cpu_pct for s in self.samples) / len(self.samples)
        cpu_max = max(s.cpu_pct for s in self.samples)
        rx_max = max(s.net_rx_mbps for s in self.samples)
        tx_max = max(s.net_tx_mbps for s in self.samples)
        therm_min = min(s.thermal_limit for s in self.samples)
        therm = "" if therm_min == 100 else f"  THERMAL-THROTTLE (CPU speed → {therm_min}%)"
        nic = f"on {self.nic}" if self.nic else "(NIC unknown)"
        return (f"client: cpu avg {cpu_avg:4.0f}% / peak {cpu_max:3.0f}%   "
                f"rx peak {rx_max:6.0f} Mbps   tx peak {tx_max:6.0f} Mbps   {nic}{therm}")


@contextmanager
def sample_during(host: str = "", interval_s: float = 1.0):
    s = Sampler(host=host, interval_s=interval_s)
    s.start()
    try:
        yield s
    finally:
        s.stop()
