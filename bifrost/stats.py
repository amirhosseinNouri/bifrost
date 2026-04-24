import json
import re
import subprocess
from pathlib import Path

from bifrost.config import STATS_FILE

# A session is only counted as a "success" if it lasted at least this long
# AND received real download traffic — shorter sessions mean the user got
# no usable connectivity, even if the tunnel technically came up.
MIN_GOOD_SESSION_SECONDS = 60


def _default_server_entry() -> dict:
    return {"rx": 0, "tx": 0, "sessions": 0, "successes": 0, "failures": 0}


def _load() -> dict:
    """Load stats from disk. Migrates flat legacy format into grouped format."""
    if not STATS_FILE.exists():
        return {}
    try:
        raw = json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    # Legacy flat format: {"o4": {"rx":..,"tx":..,"sessions":..}, ...}.
    # Group entries under the inferred "o" group if none of the values carry "servers".
    if raw and not any(isinstance(v, dict) and "servers" in v for v in raw.values()):
        migrated: dict = {"o": {"servers": {}}}
        for name, entry in raw.items():
            if isinstance(entry, dict) and {"rx", "tx", "sessions"} <= entry.keys():
                migrated["o"]["servers"][name] = entry
        return migrated

    return raw


def _save(data: dict):
    """Persist stats to disk."""
    STATS_FILE.write_text(json.dumps(data, indent=2))


def record_traffic(group: str, server_name: str, rx_bytes: int, tx_bytes: int):
    """Add traffic bytes for a server session under its group."""
    data = _load()
    group_entry = data.get(group)
    if not isinstance(group_entry, dict) or "servers" not in group_entry:
        group_entry = {"servers": {}}
    servers = group_entry["servers"]
    entry = servers.get(server_name, _default_server_entry())
    entry.setdefault("successes", 0)
    entry.setdefault("failures", 0)
    entry["rx"] += rx_bytes
    entry["tx"] += tx_bytes
    entry["sessions"] += 1
    servers[server_name] = entry
    data[group] = group_entry
    _save(data)


def record_outcome(group: str, server_name: str, success: bool):
    """Increment the success or failure counter for a server.

    Used by the ranker to bias future selection toward servers that reliably
    stay connected.
    """
    data = _load()
    group_entry = data.get(group)
    if not isinstance(group_entry, dict) or "servers" not in group_entry:
        group_entry = {"servers": {}}
    servers = group_entry["servers"]
    entry = servers.get(server_name, _default_server_entry())
    entry.setdefault("rx", 0)
    entry.setdefault("tx", 0)
    entry.setdefault("sessions", 0)
    entry.setdefault("successes", 0)
    entry.setdefault("failures", 0)
    if success:
        entry["successes"] += 1
    else:
        entry["failures"] += 1
    servers[server_name] = entry
    data[group] = group_entry
    _save(data)


def clear_stats():
    """Reset all traffic stats."""
    if STATS_FILE.exists():
        STATS_FILE.unlink()


def get_stats() -> dict:
    """Return current stats dict."""
    return _load()


def fmt_bytes(n: int) -> str:
    """Human-readable byte formatting."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def parse_openvpn_stats(log_line: str) -> tuple[int, int] | None:
    """Parse openvpn bytecount line. Returns (rx, tx) or None."""
    # OpenVPN outputs: "TUN/TAP read bytes,TUN/TAP write bytes" or
    # status lines like "TCP/UDP read bytes = X"
    # With --bytecount we get: ">BYTECOUNT:rx,tx"
    m = re.match(r">BYTECOUNT:(\d+),(\d+)", log_line)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def get_interface_bytes(interface: str = "utun") -> tuple[int, int] | None:
    """Get current RX/TX bytes from a tun/utun interface via netstat.

    netstat outputs multiple rows per interface (Link, IPv6, IPv4).
    The <Link#N> row is MISSING the Address column (10 cols instead of 11),
    so column indices are shifted. Use the IPv4 row with an actual address.

    Header: Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
    IPv4:   utun4 1500 192.168.29 192.168.29.157 3168 - 1055478 2565 - 2191568 -
    Index:  0     1    2          3               4    5 6       7    8 9       10
    """
    try:
        result = subprocess.run(
            ["netstat", "-I", interface, "-b"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return None
        # Use the row with an IPv4 address (has all 11 columns)
        for line in lines[1:]:
            if "<Link#" in line:
                continue  # Skip — missing Address column shifts indices
            parts = line.split()
            if len(parts) >= 10:
                try:
                    ibytes = int(parts[6])
                    obytes = int(parts[9])
                    return ibytes, obytes
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def find_tun_interface() -> str | None:
    """Find the active tun/utun interface used by openvpn."""
    try:
        result = subprocess.run(
            ["ifconfig"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line and not line[0].isspace() and ":" in line:
                iface = line.split(":")[0]
                if iface.startswith(("utun", "tun")):
                    # Check if it has an inet address (active VPN)
                    idx = result.stdout.find(line)
                    block = result.stdout[idx:idx + 500]
                    if "inet " in block:
                        return iface
    except Exception:
        pass
    return None
