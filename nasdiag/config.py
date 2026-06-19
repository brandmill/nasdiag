from __future__ import annotations

import os
from dataclasses import dataclass

NAS_RAM_GB = 8  # QNAP RAM — test files must exceed this × 2 to defeat NAS cache

DEFAULTS = {
    "host": os.environ.get("NASDIAG_HOST", ""),
    "share_path": os.environ.get("NASDIAG_SHARE", ""),
    "local_path": os.environ.get("NASDIAG_LOCAL", "/tmp/nasdiag"),
    "external_path": os.environ.get("NASDIAG_EXTERNAL", ""),
    "nas_user": os.environ.get("NASDIAG_NAS_USER", ""),
    "nas_key": os.environ.get("NASDIAG_NAS_KEY", ""),
    "nas_nic": os.environ.get("NASDIAG_NAS_NIC", "bond0"),
}

POLITE = {"size_gb": 1, "duration_s": 15, "concurrent_max": 2}
FULL = {"size_gb": 16, "duration_s": 30, "concurrent_max": 8}


@dataclass
class RunConfig:
    host: str
    share_paths: list[str]
    local_path: str
    external_path: str
    nas_user: str
    nas_key: str
    nas_nic: str
    size_gb: int
    duration_s: int
    concurrent_max: int

    @classmethod
    def from_args(cls, args):
        mode = FULL if getattr(args, "mode", "polite") == "full" else POLITE
        env_shares = [s for s in DEFAULTS["share_path"].split(":") if s]
        arg_shares = list(args.share_path or [])
        return cls(
            host=args.host or DEFAULTS["host"],
            share_paths=arg_shares or env_shares,
            local_path=args.local_path or DEFAULTS["local_path"],
            external_path=args.external_path or DEFAULTS["external_path"],
            nas_user=getattr(args, "nas_user", "") or DEFAULTS["nas_user"],
            nas_key=getattr(args, "nas_key", "") or DEFAULTS["nas_key"],
            nas_nic=getattr(args, "nas_nic", "") or DEFAULTS["nas_nic"],
            size_gb=args.size_gb or mode["size_gb"],
            duration_s=args.duration_s or mode["duration_s"],
            concurrent_max=mode["concurrent_max"],
        )

    def cache_warning(self) -> str | None:
        if self.size_gb < 2 * NAS_RAM_GB:
            return (
                f"WARNING: test file is {self.size_gb} GB but NAS has {NAS_RAM_GB} GB RAM. "
                f"Results may include NAS cache hits. Use --mode full (16 GB) for real disk numbers."
            )
        return None
