from contextlib import contextmanager
from dataclasses import dataclass

from .client import Sampler as ClientSampler, sample_during as client_sample_during
from .nas import NasSampler


@dataclass
class Combined:
    client: ClientSampler
    nas: NasSampler | None = None

    def summary_lines(self) -> list[str]:
        out = []
        if line := self.client.summary_line():
            out.append(line)
        if self.nas and (line := self.nas.summary_line()):
            out.append(line)
        return out


@contextmanager
def measure(host: str = "", nas_user: str = "", nas_key: str = "",
            nas_nic: str = "bond0", interval_s: float = 1.0):
    """Sample client (always-on if psutil present) and NAS (if user provided)."""
    cs = ClientSampler(host=host, interval_s=interval_s)
    ns = NasSampler(host=host, user=nas_user, key_file=nas_key, nic=nas_nic) if (nas_user and host) else None
    cs.start()
    if ns:
        ns.start()
    try:
        yield Combined(client=cs, nas=ns)
    finally:
        cs.stop()
        if ns:
            ns.stop()


# Backwards-compat: existing code uses `sample_during`. Keep it as client-only.
sample_during = client_sample_during

__all__ = ["ClientSampler", "NasSampler", "Combined", "measure", "sample_during"]
