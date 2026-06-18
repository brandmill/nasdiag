import argparse
import logging
import sys

from . import concurrent, discover, log, network, report, storage
from .config import RunConfig


def _add_common(p):
    p.add_argument("--host", default="", help="NAS hostname/IP (auto-detected from share if omitted)")
    p.add_argument("--share-path", default="", help="path to mounted NAS share (interactive picker if omitted)")
    p.add_argument("--local-path", default="", help="local internal SSD temp dir")
    p.add_argument("--external-path", default="",
                   help="external SSD path — Resolve cache disk (interactive picker if omitted)")
    p.add_argument("--size-gb", type=int, default=0, help="test file size (override mode default)")
    p.add_argument("--duration-s", type=int, default=0, help="per-test seconds (override mode default)")
    p.add_argument("--mode", choices=["polite", "full"], default="polite",
                   help="polite=1GB/15s, full=16GB/30s (defeats NAS cache)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to console")
    p.add_argument("--nas-user", default="", help="SSH user for NAS telemetry (e.g. brandy)")
    p.add_argument("--nas-key", default="", help="SSH private key path for NAS (defaults to agent/~/.ssh/id_*)")
    p.add_argument("--nas-nic", default="", help="NAS NIC to sample (default: bond0)")


def _resolve_share_and_host(cfg: RunConfig, need_host: bool, need_share: bool):
    if need_share and not cfg.share_path:
        m = discover.pick(discover.list_shares(),
                          "Pick the NAS share to test:")
        if m is None:
            raise SystemExit("ERROR: no NAS share mounted, and none provided.")
        cfg.share_path = m.path
        if not cfg.host and m.host:
            cfg.host = m.host
    if need_host and not cfg.host:
        # No share picked but host still required (network-only test)
        shares = discover.list_shares()
        if shares and shares[0].host:
            cfg.host = shares[0].host
            print(f"using NAS host from mounted share: {cfg.host}")
        else:
            raise SystemExit("ERROR: --host required (or mount the NAS share first).")


def _resolve_external(cfg: RunConfig, required: bool):
    if cfg.external_path:
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
    args = p.parse_args(argv)

    logfile = log.setup(verbose=args.verbose)
    logging.info("nasdiag %s starting (mode=%s)", args.cmd, args.mode)

    try:
        cfg = RunConfig.from_args(args)

        if args.cmd == "network":
            _resolve_share_and_host(cfg, need_host=True, need_share=False)
        elif args.cmd in ("storage", "concurrent"):
            _resolve_share_and_host(cfg, need_host=False, need_share=True)
        elif args.cmd == "external":
            _resolve_external(cfg, required=True)
        elif args.cmd == "suite":
            _resolve_share_and_host(cfg, need_host=True, need_share=True)
            _resolve_external(cfg, required=False)

        warn = cfg.cache_warning()
        if warn and args.cmd in ("storage", "concurrent", "suite"):
            print(warn + "\n")

        nas_kw = dict(nas_user=cfg.nas_user, nas_key=cfg.nas_key, nas_nic=cfg.nas_nic)
        if args.cmd == "network":
            network.run(cfg.host, cfg.duration_s, **nas_kw)
        elif args.cmd == "storage":
            storage.run(cfg.share_path, cfg.size_gb, cfg.duration_s, label="NAS",
                        telemetry_host=cfg.host, **nas_kw)
        elif args.cmd == "local":
            storage.run(cfg.local_path, cfg.size_gb, cfg.duration_s, label="local SSD")
        elif args.cmd == "external":
            storage.run(cfg.external_path, cfg.size_gb, cfg.duration_s, label="external SSD")
        elif args.cmd == "concurrent":
            concurrent.run(cfg.share_path, cfg.size_gb, cfg.duration_s,
                           cfg.concurrent_max, label="NAS",
                           telemetry_host=cfg.host, **nas_kw)
        elif args.cmd == "suite":
            rpt = report.Report(mode=args.mode, host=cfg.host,
                                share_path=cfg.share_path, local_path=cfg.local_path,
                                external_path=cfg.external_path)
            rpt.network = network.run(cfg.host, cfg.duration_s, **nas_kw)
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
            rpt.nas_storage = storage.run(cfg.share_path, cfg.size_gb, cfg.duration_s,
                                          label="NAS", telemetry_host=cfg.host, **nas_kw)
            print()
            rpt.concurrent = concurrent.run(cfg.share_path, cfg.size_gb, cfg.duration_s,
                                            cfg.concurrent_max, label="NAS",
                                            telemetry_host=cfg.host, **nas_kw)
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
