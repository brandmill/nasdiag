# nasdiag

Bisection diagnostics for shared-NAS video editing. Localizes a bottleneck to network, NAS, client SSD (internal or external), or concurrent NAS load.

## Install (Mac)

```
brew install iperf3 fio
pip3 install psutil          # for client telemetry (Phase 3)
# nasdiag itself is pure stdlib Python — no pip install needed
```

On the NAS (QNAP):
```
ssh admin@<nas>
opkg install iperf3      # via Entware; or run iperf3 in Container Station
iperf3 -s -D             # daemonize the iperf3 server (port 5201)
```

## Commands

```
python3 -m nasdiag gui          # web UI on http://127.0.0.1:8765 (default)
python3 -m nasdiag network
python3 -m nasdiag storage
python3 -m nasdiag local
python3 -m nasdiag external
python3 -m nasdiag concurrent
python3 -m nasdiag suite
```

The `gui` subcommand opens your browser to a local form: pick host, tick the NAS volumes you want to test, optionally pick an external SSD, choose polite/full mode, click Run. Output streams live; the HTML report embeds inline when finished.

No flags needed for the common case — nasdiag scans your mounted volumes and prompts you to pick the NAS share and the external SSD interactively. The NAS host is auto-extracted from the SMB share's mount source, so `--host` is usually unnecessary.

```
Pick the NAS share to test:
  1. /Volumes/Projects  (smbfs → 10.0.0.10)
  2. /Volumes/Media     (nfs   → 10.0.0.20)
  > 1
Pick the external SSD (e.g. Resolve cache disk):
  1. /Volumes/Cache-SSD  (apfs)
  0. skip
  > 1
```

## Flags (override the picker, useful for scripts)

- `--host`, `--share-path`, `--local-path`, `--external-path` — or env: `NASDIAG_HOST`, `NASDIAG_SHARE`, `NASDIAG_LOCAL`, `NASDIAG_EXTERNAL`
- `--mode polite` (default) / `--mode full` — polite=1GB/15s/≤2 workers, full=16GB/30s/≤8 workers
- `--size-gb N`, `--duration-s N` — override mode defaults
- `-v` / `--verbose` — stream debug logs to console

## What each test measures

- **network**: iperf3 both directions, Gbit/s vs 10GbE theoretical, retransmits
- **storage**: seq read (1M qd1), seq write (1M qd1), rand read (4K qd16) on the NAS, `direct=1`
- **local**: same tests on internal SSD — known-fast baseline
- **external**: same tests on an external SSD — Mac Studios use external SSDs as the Resolve cache disk, so this measures whether the cache disk itself is a bottleneck
- **concurrent**: ramps random readers 1→2→4→8 against the NAS, reports aggregate MB/s and p99 latency per level, plus a plateau/spike verdict
- **suite**: network → local → external (if available) → NAS → concurrent NAS

## Logging

Every run writes a timestamped log to `~/.nasdiag/logs/nasdiag-YYYYMMDD-HHMMSS.log` with full debug output (fio commands, stderr, timings, stack traces). Console shows clean results; `-v` streams debug to terminal too. On failure, stderr prints the log path.

## Phases

- [x] **Phase 1** — network, single-stream storage, internal SSD baseline
- [x] **Phase 2** — concurrent ramp, external SSD support, logging, error reporting
- [x] **Phase 2.5** — interactive volume picker (no hardcoded paths), NAS host auto-detection
- [ ] **Phase 3** — client telemetry (CPU/memory/NIC throughput/thermal throttling sampled during tests)
- [x] **Phase 4** — NAS telemetry via SSH (QNAP CPU, memory, NIC, busiest md disk util), sampled in parallel with each client test
- [x] **Phase 5** — self-contained HTML report with inline-SVG charts and plain-language verdict

## Report (Phase 5)

`nasdiag suite` collects every result, prints a summary table + plain-language verdict to the console, and writes a self-contained HTML report (inline CSS + inline SVG, no external assets) to `~/.nasdiag/reports/report-<timestamp>.html`. Open it in any browser, AirDrop it, or attach it to a ticket — single file.

Example verdict output:
```
✓ NETWORK fine: 9.3 Gbit/s avg (vs 10 GbE theoretical).
✓ EXTERNAL CACHE SSD fine: 2100 MB/s seq read.
✓ NAS single-stream fine: 1100 MB/s seq read solo.
⚠ NAS CONCURRENT: throughput plateaus at 1 worker(s) (collapses from 1100 → 220 MB/s)
⚠ NAS CONCURRENT: p99 latency > 100 ms at 8 worker(s) — stalls likely under load

→ BOTTLENECK: NAS under concurrent load — single-stream is fine but the pool/NIC can't sustain multiple editors.
```

The HTML report adds: bar chart for network direction, line chart for concurrent ramp throughput, and line chart for p99 latency vs workers.

## NAS telemetry (Phase 4)

Pass `--nas-user <user>` (e.g. `--nas-user brandy`) to enable NAS-side sampling. Telemetry is collected over a single SSH connection running a shell loop on the NAS — no agent or daemon installed remotely. Requires passwordless SSH (key-based auth).

```
nasdiag suite --nas-user brandy --nas-nic bond0
```

What you'll see under each test result:

```
  seq_read    1100.5 MB/s   1080 IOPS  lat mean  0.91 ms  p99   3.20 ms
            client: cpu avg  18% / peak  35%   rx peak  9200 Mbps   tx peak   12 Mbps   on en0
            nas:    cpu avg  22% / peak  41%   rx peak    14 Mbps   tx peak 9180 Mbps   md1 util peak  87%
```

Set up passwordless SSH from each Mac to the QNAP:
```
ssh-keygen -t ed25519 -C "nasdiag@$(hostname)"
ssh-copy-id -i ~/.ssh/id_ed25519.pub brandy@<nas-ip>
```

Caveats: QNAP uses mdadm RAID, not ZFS — so there's no ARC cache hit-rate. The Linux page cache hit-rate is implicit in the memory line (cached/total) but isn't broken out per-mount.
