import html
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .concurrent import RampPoint
from .network import NetResult, THEORETICAL_GBIT
from .storage import StorageResult

log = logging.getLogger(__name__)

REPORT_DIR = Path.home() / ".nasdiag" / "reports"

NET_OK_GBIT = 8.0
NET_DEGRADED_GBIT = 5.0
NAS_SEQ_OK_MBPS = 400.0
EXT_SSD_OK_MBPS = 500.0
P99_STALL_MS = 100.0
PLATEAU_RATIO = 1.2


@dataclass
class VolumeResults:
    path: str
    storage: list[StorageResult] = field(default_factory=list)
    concurrent: list[RampPoint] = field(default_factory=list)

    def by_test(self) -> dict[str, StorageResult]:
        return {r.test: r for r in self.storage}


@dataclass
class Report:
    started_at: float = field(default_factory=time.time)
    mode: str = "polite"
    host: str = ""
    local_path: str = ""
    external_path: str = ""
    nic: str = ""
    network: list[NetResult] = field(default_factory=list)
    local_storage: list[StorageResult] = field(default_factory=list)
    external_storage: list[StorageResult] = field(default_factory=list)
    volumes: list[VolumeResults] = field(default_factory=list)


def _net_avg(net: list[NetResult]) -> float:
    return sum(r.gbit_per_sec for r in net) / len(net) if net else 0.0


def _is_wifi(nic: str) -> bool:
    if sys.platform != "darwin" or not nic:
        return False
    try:
        out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"], text=True, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    blocks = out.split("\n\n")
    for block in blocks:
        if f"Device: {nic}" in block and ("Wi-Fi" in block or "AirPort" in block):
            return True
    return False


def verdict(r: Report) -> list[str]:
    lines: list[str] = []
    net_avg = _net_avg(r.network)
    wifi = _is_wifi(r.nic)
    network_is_bottleneck = net_avg > 0 and net_avg < NET_DEGRADED_GBIT

    # NETWORK
    if r.network:
        if wifi:
            lines.append(f"~ NETWORK: {net_avg:.2f} Gbit/s on Wi-Fi ({r.nic}) — "
                         f"this is your wireless link, not the NAS. Test from a wired Mac for real NAS numbers.")
        elif net_avg >= NET_OK_GBIT:
            lines.append(f"✓ NETWORK fine: {net_avg:.2f} Gbit/s (vs 10 GbE theoretical).")
        elif net_avg >= NET_DEGRADED_GBIT:
            lines.append(f"~ NETWORK acceptable: {net_avg:.2f} Gbit/s — workable but not maxing the 10 GbE link.")
        else:
            lines.append(f"⚠ NETWORK degraded: {net_avg:.2f} Gbit/s — check NIC negotiation, switch port, cable.")

    # EXTERNAL SSD
    if r.external_storage:
        m = {s.test: s for s in r.external_storage}
        sr = m.get("seq_read")
        if sr and sr.mb_per_sec < EXT_SSD_OK_MBPS:
            lines.append(f"⚠ EXTERNAL CACHE SSD slow: {sr.mb_per_sec:.0f} MB/s seq read — "
                         f"cache disk may be a bottleneck.")
        elif sr:
            lines.append(f"✓ EXTERNAL CACHE SSD fine: {sr.mb_per_sec:.0f} MB/s seq read.")

    # PER-VOLUME NAS
    if r.volumes:
        if network_is_bottleneck:
            lines.append(f"  (NAS volumes measured through {net_avg:.2f} Gbit/s link — "
                         f"numbers below are network-bound, not NAS-bound)")
        for v in r.volumes:
            _verdict_volume(v, lines, network_is_bottleneck)

    # FINAL
    final = _localize_bottleneck(r, wifi=wifi)
    if final:
        lines.append("")
        lines.append(final)
    return lines


def _verdict_volume(v: VolumeResults, lines: list[str], net_capped: bool):
    name = Path(v.path).name or v.path
    m = v.by_test()
    sr = m.get("seq_read")
    if sr:
        if net_capped:
            lines.append(f"  · {name}: {sr.mb_per_sec:.0f} MB/s seq read (link-capped)")
        elif sr.mb_per_sec < NAS_SEQ_OK_MBPS:
            lines.append(f"⚠ {name}: slow at {sr.mb_per_sec:.0f} MB/s seq read solo.")
        else:
            lines.append(f"✓ {name}: {sr.mb_per_sec:.0f} MB/s seq read solo.")
    if v.concurrent and not net_capped:
        peak = max(p.mb_per_sec for p in v.concurrent)
        plateau_n = _plateau(v.concurrent)
        spike_n = next((p.n_workers for p in v.concurrent if p.latency_ms_p99 > P99_STALL_MS), None)
        if plateau_n is not None:
            first_mb = v.concurrent[0].mb_per_sec
            last_mb = v.concurrent[-1].mb_per_sec
            collapse = f" (collapses {first_mb:.0f}→{last_mb:.0f})" if last_mb < first_mb else ""
            lines.append(f"⚠ {name} concurrent: plateaus at {plateau_n} worker(s){collapse}, peak {peak:.0f} MB/s")
        if spike_n is not None:
            lines.append(f"⚠ {name} concurrent: p99 > {P99_STALL_MS:.0f} ms at {spike_n} worker(s)")


def _plateau(points: list[RampPoint]) -> int | None:
    for i in range(1, len(points)):
        if points[i].mb_per_sec < points[i - 1].mb_per_sec * PLATEAU_RATIO:
            return points[i - 1].n_workers
    return None


def _localize_bottleneck(r: Report, wifi: bool) -> str:
    net_avg = _net_avg(r.network)
    if wifi and net_avg < NET_OK_GBIT:
        return ("→ BOTTLENECK: Wi-Fi link — the wireless connection is the limit. "
                "Move to a wired Mac to diagnose the actual NAS.")
    if net_avg and net_avg < NET_DEGRADED_GBIT:
        return f"→ BOTTLENECK: NETWORK ({net_avg:.2f} Gbit/s) — fix the link first."
    ext_slow = False
    if r.external_storage:
        m = {s.test: s for s in r.external_storage}
        sr = m.get("seq_read")
        ext_slow = bool(sr and sr.mb_per_sec < EXT_SSD_OK_MBPS)
    if ext_slow:
        return "→ BOTTLENECK: external cache SSD — playback can stutter even without touching the NAS."
    if r.volumes:
        for v in r.volumes:
            sr = v.by_test().get("seq_read")
            if sr and sr.mb_per_sec < NAS_SEQ_OK_MBPS:
                return f"→ BOTTLENECK: NAS volume {Path(v.path).name} — even one editor maxes it out."
            if v.concurrent and len(v.concurrent) >= 2 and v.concurrent[-1].mb_per_sec < v.concurrent[0].mb_per_sec:
                return f"→ BOTTLENECK: NAS volume {Path(v.path).name} collapses under concurrent load."
    if r.network and r.volumes:
        return "→ NO CLEAR BOTTLENECK in measured layers — investigate Resolve cache size, codecs, client load."
    return ""


# ---- console output -------------------------------------------------------

def to_console(r: Report) -> str:
    out = ["", "=" * 72, "SUMMARY", "=" * 72]
    if r.network:
        out.append(f"\nNetwork (iperf3 vs 10 GbE, nic={r.nic or '?'}):")
        for nr in r.network:
            out.append(f"  {nr.direction:<10s} {nr.gbit_per_sec:5.2f} Gbit/s   retx={nr.retransmits}")
    for label, results in [("Local SSD", r.local_storage),
                           ("External SSD", r.external_storage)]:
        if results:
            out.append(f"\nStorage — {label}:")
            for sr in results:
                out.append(f"  {sr.test:9s}  {sr.mb_per_sec:8.1f} MB/s   "
                           f"{sr.iops:8.0f} IOPS   p99 {sr.latency_ms_p99:6.2f} ms")
    for v in r.volumes:
        out.append(f"\nNAS volume — {v.path}:")
        for sr in v.storage:
            out.append(f"  {sr.test:9s}  {sr.mb_per_sec:8.1f} MB/s   "
                       f"{sr.iops:8.0f} IOPS   p99 {sr.latency_ms_p99:6.2f} ms")
        if v.concurrent:
            out.append(f"  concurrent:")
            for p in v.concurrent:
                out.append(f"    {p.n_workers:2d} worker(s)  {p.mb_per_sec:8.1f} MB/s   "
                           f"p99 {p.latency_ms_p99:7.2f} ms")
    out.append("\n" + "-" * 72)
    out.append("VERDICT")
    out.append("-" * 72)
    for line in verdict(r):
        out.append(line)
    out.append("=" * 72)
    return "\n".join(out)


# ---- HTML -----------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }
body { background: #0d0d0d; color: #e4e4e4; font-family: -apple-system, BlinkMacSystemFont, 'Inter', sans-serif; margin: 0; padding: 24px; }
.container { max-width: 1000px; margin: 0 auto; }
h1 { color: #fff; margin: 0 0 6px; font-size: 22px; }
h2 { color: #fff; margin: 32px 0 8px; font-size: 16px; border-bottom: 1px solid #333; padding-bottom: 6px; }
h3 { color: #fff; margin: 20px 0 6px; font-size: 14px; }
.meta { color: #888; font-size: 12px; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 12px; text-align: right; border-bottom: 1px solid #222; font-size: 13px; }
th { color: #888; font-weight: 500; text-align: right; background: #161616; }
th:first-child, td:first-child { text-align: left; }
.verdict { background: #161616; border-left: 3px solid #E37222; padding: 16px 20px; margin: 16px 0; font-size: 13px; line-height: 1.7; }
.verdict .ok { color: #6cc167; }
.verdict .warn { color: #E37222; }
.verdict .final { color: #fff; font-weight: 600; margin-top: 8px; display: block; }
svg { background: #0d0d0d; display: block; }
.chart-label { fill: #888; font-size: 11px; font-family: monospace; }
.bar { fill: #E37222; }
.bar-bg { fill: #1a1a1a; }
.gridline { stroke: #222; stroke-width: 1; }
.line { fill: none; stroke: #E37222; stroke-width: 2; }
.dot { fill: #E37222; }
"""


def _svg_bar_chart(rows, unit, max_val=None, width=600, row_h=28):
    if not rows:
        return ""
    max_val = max_val or max(v for _, v in rows) * 1.1 or 1.0
    label_w = 140
    bar_area = width - label_w - 80
    h = row_h * len(rows) + 16
    parts = [f'<svg width="{width}" height="{h}" xmlns="http://www.w3.org/2000/svg">']
    for i, (label, val) in enumerate(rows):
        y = i * row_h + 14
        bar_w = max(2, int(bar_area * val / max_val))
        parts.append(f'<text x="0" y="{y + 6}" class="chart-label">{html.escape(label)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y - 8}" width="{bar_area}" height="18" class="bar-bg"/>')
        parts.append(f'<rect x="{label_w}" y="{y - 8}" width="{bar_w}" height="18" class="bar"/>')
        parts.append(f'<text x="{label_w + bar_w + 6}" y="{y + 6}" class="chart-label">{val:.1f} {unit}</text>')
    parts.append('</svg>')
    return "".join(parts)


def _svg_line_chart(points, xlabel, ylabel, unit, width=600, height=220):
    if len(points) < 2:
        return ""
    pad_l, pad_b, pad_t, pad_r = 56, 32, 16, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_max = max(ys) * 1.15 or 1.0
    x_max = x_max if x_max > x_min else x_min + 1

    def px(x): return pad_l + plot_w * (x - x_min) / (x_max - x_min)
    def py(y): return pad_t + plot_h * (1 - y / y_max)

    parts = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">']
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h * (1 - frac)
        v = y_max * frac
        parts.append(f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" class="gridline"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 4}" text-anchor="end" class="chart-label">{v:.0f}</text>')
    for x in xs:
        parts.append(f'<text x="{px(x)}" y="{height - 12}" text-anchor="middle" class="chart-label">{int(x)}</text>')
    path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
    parts.append(f'<polyline points="{path}" class="line"/>')
    for x, y in points:
        parts.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="3" class="dot"/>')
    parts.append(f'<text x="{pad_l + plot_w / 2}" y="{height - 2}" text-anchor="middle" class="chart-label">{html.escape(xlabel)}</text>')
    parts.append(f'<text x="14" y="{pad_t + plot_h / 2}" transform="rotate(-90 14 {pad_t + plot_h / 2})" text-anchor="middle" class="chart-label">{html.escape(ylabel)} ({unit})</text>')
    parts.append('</svg>')
    return "".join(parts)


def _verdict_html(r):
    items = []
    for line in verdict(r):
        if not line:
            continue
        cls = ""
        if line.startswith("✓"):
            cls = "ok"
        elif line.startswith("⚠") or line.startswith("~"):
            cls = "warn"
        elif line.startswith("→"):
            cls = "final"
        items.append(f'<span class="{cls}">{html.escape(line)}</span><br/>')
    return "".join(items)


def _storage_table(label, results):
    if not results:
        return ""
    rows = ["<table><thead><tr><th>test</th><th>MB/s</th><th>IOPS</th><th>p99 ms</th></tr></thead><tbody>"]
    for sr in results:
        rows.append(f"<tr><td>{html.escape(sr.test)}</td>"
                    f"<td>{sr.mb_per_sec:.1f}</td>"
                    f"<td>{sr.iops:.0f}</td>"
                    f"<td>{sr.latency_ms_p99:.2f}</td></tr>")
    rows.append("</tbody></table>")
    return f"<h3>{html.escape(label)}</h3>" + "".join(rows)


def to_html(r):
    shares = ", ".join(html.escape(v.path) for v in r.volumes) or "—"
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>nasdiag report — {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r.started_at))}</title>",
        f"<style>{_CSS}</style></head><body><div class='container'>",
        "<h1>nasdiag report</h1>",
        f"<div class='meta'>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r.started_at))} · "
        f"mode={html.escape(r.mode)} · host={html.escape(r.host)} · nic={html.escape(r.nic or '?')} · "
        f"volumes={shares}</div>",
        "<div class='verdict'>", _verdict_html(r), "</div>",
    ]
    if r.network:
        rows = [(nr.direction, nr.gbit_per_sec) for nr in r.network]
        parts.append("<h2>Network (Gbit/s vs 10 GbE)</h2>")
        parts.append(_svg_bar_chart(rows, "Gbit/s", max_val=THEORETICAL_GBIT))
    if r.local_storage or r.external_storage:
        parts.append("<h2>Local disks</h2>")
        parts.append(_storage_table("Internal SSD", r.local_storage))
        parts.append(_storage_table("External SSD (Resolve cache)", r.external_storage))
    for v in r.volumes:
        parts.append(f"<h2>NAS volume — {html.escape(v.path)}</h2>")
        parts.append(_storage_table("storage", v.storage))
        if v.concurrent:
            parts.append("<h3>concurrent readers — throughput</h3>")
            parts.append(_svg_line_chart([(p.n_workers, p.mb_per_sec) for p in v.concurrent],
                                         "workers", "aggregate", "MB/s"))
            parts.append("<h3>concurrent readers — p99 latency</h3>")
            parts.append(_svg_line_chart([(p.n_workers, p.latency_ms_p99) for p in v.concurrent],
                                         "workers", "p99", "ms"))
    parts.append("</div></body></html>")
    return "".join(parts)


def write_html(r):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report-{time.strftime('%Y%m%d-%H%M%S', time.localtime(r.started_at))}.html"
    path.write_text(to_html(r))
    log.info("html report: %s", path)
    return path
