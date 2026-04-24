"""Outgoing-connection sampler.

Periodically runs `lsof` to capture unique remote TCP peers the system is
talking to while the VPN is up, and appends new ones to connections.log so
the user can review and curate block.conf afterwards.
"""

import ipaddress
import re
import subprocess
import threading
from threading import Event

from bifrost.config import CONNECTIONS_FILE, CONNECTION_SAMPLE_INTERVAL
from bifrost.display import log_debug

_lock = threading.Lock()
_seen: set[str] = set()
_loaded = False

# Skip local / private destinations — we only care about internet peers.
_SKIP_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
    )
]

_LSOF_RE = re.compile(r"->\s*(\[?[0-9a-fA-F:.]+\]?):(\d+)")


def _is_public_v4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not isinstance(addr, ipaddress.IPv4Address):
        return False
    return not any(addr in net for net in _SKIP_NETS)


def _load_existing() -> None:
    global _loaded
    if _loaded:
        return
    try:
        with open(CONNECTIONS_FILE) as f:
            for line in f:
                first = line.strip().split(None, 1)[0] if line.strip() else ""
                if first and not first.startswith("#"):
                    _seen.add(first)
    except FileNotFoundError:
        pass
    _loaded = True


def _run_lsof() -> str:
    """Try sudo lsof (all processes); fall back to unprivileged lsof."""
    for argv in (
        ["sudo", "-n", "lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"],
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"],
    ):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except Exception as e:
            log_debug(f"[connections] lsof exec failed ({argv[0]}): {e}")
            continue
        if out.returncode == 0 and out.stdout:
            return out.stdout
    return ""


def _sample() -> set[tuple[str, str]]:
    """Return set of (addr, process) for established TCP peers."""
    stdout = _run_lsof()
    peers: set[tuple[str, str]] = set()
    for line in stdout.splitlines()[1:]:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        process = parts[0]
        name_field = parts[8]
        m = _LSOF_RE.search(name_field)
        if not m:
            continue
        ip = m.group(1).strip("[]")
        port = m.group(2)
        if not _is_public_v4(ip):
            continue
        peers.add((f"{ip}:{port}", process))
    return peers


def _record(addr: str, process: str) -> None:
    with _lock:
        _load_existing()
        if addr in _seen:
            return
        _seen.add(addr)
        try:
            with open(CONNECTIONS_FILE, "a") as f:
                f.write(f"{addr}\t{process}\n")
        except OSError as e:
            log_debug(f"[connections] write failed: {e}")


def sampler_loop(stop_event: Event) -> None:
    _load_existing()
    while not stop_event.is_set():
        for addr, process in _sample():
            _record(addr, process)
        stop_event.wait(CONNECTION_SAMPLE_INTERVAL)


def start(stop_event: Event) -> None:
    t = threading.Thread(target=sampler_loop, args=(stop_event,), daemon=True)
    t.start()


def get_entries() -> list[str]:
    with _lock:
        _load_existing()
        return sorted(_seen)
