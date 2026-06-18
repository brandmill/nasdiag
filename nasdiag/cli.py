import argparse
import logging
import sys

from . import concurrent, discover, log, network, profile, report, storage
from .config import RunConfig


def _add_common(p):
    p.add_argument("--host", default="", help="NAS hostname/IP (auto-detected from share if omitted)")
    p.add_argument("--share-path", action="append", default=None,
                   help="NAS share path (repeatable; interactive picker if omitted)")
    p.add_argument("--local-path", default="", help="local internal SSD temp dir")
    p.add_argument("--external-path", default="", help="external SSD path (interactive picker if omitted)")
    p.add_argument("--skip-external", action="store_true", help="skip the external SSD test entirely")
    p.add_argument("--size-gb", type=int, default=0, help="test file size (override mode default)")
    p.add_argument("--duration-s", type=int, default=0, help="per-test seconds (override mode default)")
    p.add_argument("--mode", choices=["polite", "full"], default="polite",
                   help="polite=1GB/15s, full=16GB/30s (defeats NAS cache)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to console")
    p.add_argument("--nas-user", default="", help="SSH user for NAS telemetry (e.g. brandy)")
    p.add_argument("--nas-key", default="", help="SSH private key path for NAS")
    p.add_argument("--nas-nic", default="", help="NAS NIC to sample (default: bond0)")


def _resolve_shares(cfg: RunConfig, need_host: bool, need_share: bool):
    if need_share and not cfg.share_paths:
        picked = discover.pick_many(discover.list_shares(), "Pick NAS share(s) to test:")
        if not picked:
            raise SystemExit("ERROR: no NAS share mounted, and none provided.")
        cfg.share_paths = [m.path for m in picked]
        if not cfg.host and picked[0].host:
            cfg.host = picked[0].host
    if need_host and not cfg.host:
        shares = discover.list_shares()
        if shares and shares[0].host:
            cfg.host = shares[0].host
            print(f"using NAS host from mounted share: {cfg.host}")
        else:
            raise SystemExit("ERROR: --host required (or mount the NAS share first).")


def _resolve_external(cfg: RunConfig, required: bool, skip: bool):
    if skip or cfg.external_path:
        return
    m = discover.pick(discover.list_externals(),
                      "Pick the external SSD (e.g. Resolve cache disk):",
                      allow_skip=not required)
    if m is None and required:
        raise SystemExit("ERROR: no external SSD selected.")
    if m is not None:
        cfg.external_path = m.path


def main(argv=None):
    p = argparse.ArgumentParser(prog="nasdiag", description="NAS storage/network bisection")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("network", "storage", "local", "external", "concurrent", "suite"):
        _add_common(sub.add_parser(name))
    gp = sub.add_parser("gui", help="launch local web UI")
    gp.add_argument("--port", type=int, default=8765)
    gp.add_argument("--no-browser", action="store_true")
    gp.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "gui":
        from . import gui, log as _log
        _log.setup(verbose=args.verbose)
        gui.serve(port=args.port, open_browser=not args.no_browser)
        return 0

    logfile = log.setup(verbose=args.verbose)
    logging.info("nasdiag %s starting (mode=%s)", args.cmd, args.mode)

    try:
        cfg = RunConfig.from_args(args)

        if args.cmd == "network":
            _resolve_shares(cfg, need_host=True, need_share=False)
        elif args.cmd in ("storage", "concurrent"):
            _resolve_shares(cfg, need_host=False, need_share=True)
        elif args.cmd == "external":
            _resolve_external(cfg, required=True, skip=False)
        elif args.cmd == "suite":
            _resolve_shares(cfg, need_host=True, need_share=True)
            _resolve_external(cfg, required=False, skip=args.skip_external)

        nas_kw = dict(nas_user=cfg.nas_user, nas_key=cfg.nas_key, nas_nic=cfg.nas_nic)

        if args.cmd == "network":
            network.run(cfg.host, cfg.duration_s, **nas_kw)
        elif args.cmd == "storage":
            for share in cfg.share_paths:
                storage.run(share, cfg.size_gb, cfg.duration_s, label=f"NAS {share}",
                            telemetry_host=cfg.host, **nas_kw)
        elif args.cmd == "local":
            storage.run(cfg.local_path, cfg.size_gb, cfg.duration_s, label="local SSD")
        elif args.cmd == "external":
            storage.run(cfg.external_path, cfg.size_gb, cfg.duration_s, label="external SSD")
        elif args.cmd == "concurrent":
            for share in cfg.share_paths:
                concurrent.run(share, cfg.size_gb, cfg.duration_s,
                               cfg.concurrent_max, label=f"NAS {share}",
                               telemetry_host=cfg.host, **nas_kw)
        elif args.cmd == "suite":
            rpt = report.Report(mode=args.mode, host=cfg.host,
                                local_path=cfg.local_path,
                                external_path=cfg.external_path)
            print("MAC PROFILE — collecting (1s)…")
            rpt.profile = profile.collect(share_paths=cfg.share_paths)
            for line in profile.profile_passes(rpt.profile):
                print(line)
            print()
            warn = cfg.cache_warning()
            if warn:
                print(warn + "\n")
            net_results, client_nic = network.run(cfg.host, cfg.duration_s, **nas_kw)
            rpt.network = net_results
            rpt.nic = client_nic
            print()
            rpt.local_storage = storage.run(cfg.local_path, cfg.size_gb, cfg.duration_s,
                                            label="local SSD")
            print()
            if cfg.external_path:
                rpt.external_storage = storage.run(cfg.external_path, cfg.size_gb,
                                                   cfg.duration_s, label="external SSD")
                print()
            else:
                print("(skipping external SSD)\n")
            for share in cfg.share_paths:
                v = report.VolumeResults(path=share)
                v.storage = storage.run(share, cfg.size_gb, cfg.duration_s,
                                        label=f"NAS {share}",
                                        telemetry_host=cfg.host, **nas_kw)
                print()
                v.concurrent = concurrent.run(share, cfg.size_gb, cfg.duration_s,
                                              cfg.concurrent_max, label=f"NAS {share}",
                                              telemetry_host=cfg.host, **nas_kw)
                print()
                rpt.volumes.append(v)
            print(report.to_console(rpt))
            html_path = report.write_html(rpt)
            print(f"\nhtml report: {html_path}")
    except SystemExit:
        raise
    except Exception as e:
        logging.exception("nasdiag failed")
        print(f"\nERROR: {e}", file=sys.stderr)
        print(f"Full traceback logged to: {logfile}", file=sys.stderr)
        return 1
    finally:
        logging.info("log file: %s", logfile)
        print(f"\nlog: {logfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
