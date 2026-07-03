from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass

from . import telemetry, tools

log = logging.getLogger(__name__)

THEORETICAL_GBIT = 10.0


@dataclass
class NetResult:
    direction: str
    gbit_per_sec: float
    retransmits: int

    def vs_theoretical(self) -> float:
        return 100.0 * self.gbit_per_sec / THEORETICAL_GBIT


def _iperf_error(proc: subprocess.CompletedProcess) -> str:
    """iperf3 -J reports errors in the stdout JSON, not stderr."""
    try:
        return json.loads(proc.stdout).get("error", "") or proc.stderr.strip()
    except (json.JSONDecodeError, TypeError):
        return proc.stderr.strip()


def _run(host: str, duration: int, reverse: bool) -> NetResult:
    iperf3 = tools.require("iperf3")
    cmd = [iperf3, "-c", host, "-t", str(duration), "-J"]
    if reverse:
        cmd.append("-R")
    log.debug("running: %s", " ".join(cmd))
    for attempt in (1, 2):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            break
        msg = _iperf_error(proc)
        # A recent control connection (e.g. our own reachability probe)
        # leaves the server busy for a moment; that clears on its own.
        if attempt == 1 and "busy" in msg.lower():
            log.debug("iperf3 server busy, retrying in 2s")
            time.sleep(2.0)
            continue
        raise RuntimeError(
            f"iperf3 failed (exit {proc.returncode}): {msg or 'no error output'}. "
            f"Is iperf3 -s running on {host}?"
        )
    data = json.loads(proc.stdout)
    end = data["end"]
    bps = end["sum_received"]["bits_per_second"]
    retx = end.get("sum_sent", {}).get("retransmits", 0)
    return NetResult(
        direction="download" if reverse else "upload",
        gbit_per_sec=bps / 1e9,
        retransmits=retx,
    )


def run(host: str, duration_s: int = 10, nas_user: str = "", nas_key: str = "",
        nas_nic: str = "bond0") -> tuple[list[NetResult], str]:
    if not host:
        raise SystemExit("ERROR: --host required (or set NASDIAG_HOST).")

    # Pre-flight: DNS resolve (with auto .local fallback for bare hostnames)
    resolved, err = tools.resolve_host(host)
    if err:
        raise SystemExit(
            f"NETWORK ERROR: {err}\n"
            f"  Tip: try the IP directly (e.g. 192.168.8.112) or check that the "
            f"host is on this network."
        )
    if resolved != host:
        print(f"  resolved {host} → {resolved}")
        host = resolved

    # Pin iperf3 to a concrete, reachable address: iperf3 tries only the
    # resolver's first answer, and multi-homed mDNS hosts put zone-less
    # link-local IPv6 first ('No route to host').
    addr = tools.pick_reachable_addr(host, 5201)
    if addr and addr != host:
        print(f"  using {host} at {addr}")
        host = addr

    # Pre-flight: TCP connect to iperf3 port — clearer error than 'exit 1'
    tcp_err = tools.tcp_reachable(host, 5201, timeout=3)
    if tcp_err:
        raise SystemExit(
            f"NETWORK ERROR: cannot reach {host}:5201 ({tcp_err})\n"
            f"  iperf3 server is probably not running on the NAS.\n"
            f"  Start it with:  ssh {host} 'iperf3 -s -D'"
        )
    # The probe above occupies the server's single control slot for a
    # moment after closing; let it settle before the real client.
    time.sleep(1.0)

    print(f"NETWORK — iperf3 to {host}, {duration_s}s each direction")
    results = []
    client_nic = ""
    for reverse, label in [(False, "client → NAS  "), (True, "NAS → client  ")]:
        with telemetry.measure(host=host, nas_user=nas_user, nas_key=nas_key,
                               nas_nic=nas_nic) as m:
            r = _run(host, duration_s, reverse)
        results.append(r)
        if m.client.nic:
            client_nic = m.client.nic
        print(f"  {label} {r.gbit_per_sec:5.2f} Gbit/s   "
              f"({r.vs_theoretical():4.1f}% of 10GbE)   "
              f"retx={r.retransmits}")
        for line in m.summary_lines():
            print(f"            {line}")
    return results, client_nic
