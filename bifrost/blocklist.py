"""Blocklist: hosts / IPs / CIDRs that are silently dropped via blackhole routes.

block.conf entries:
  example.com           — resolve to A records, blackhole each IP
  1.2.3.4               — raw IPv4, blackhole directly
  1.2.3.0/24            — CIDR, blackhole the whole range

More-specific host/CIDR routes win over the VPN default route, so matching
traffic is dropped regardless of which interface it would otherwise use.
"""

import ipaddress
import socket
import subprocess
import time

from bifrost.config import BLOCK_FILE
from bifrost.display import log_debug, log_info

_patterns: list[str] = []           # raw entries as written (for display)
_applied_hosts: set[str] = set()    # IPs we installed host-routes for
_applied_cidrs: set[str] = set()    # CIDRs we installed net-routes for


def _classify(entry: str) -> str:
    """Return 'cidr', 'ip', or 'domain'."""
    if "/" in entry and entry[0].isdigit():
        return "cidr"
    try:
        ipaddress.ip_address(entry)
        return "ip"
    except ValueError:
        return "domain"


def load_blocklist() -> list[str]:
    """Parse block.conf into a list of raw entries (no resolution yet)."""
    global _patterns
    entries: list[str] = []
    try:
        with open(BLOCK_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                entries.append(line.lstrip(".").lower())
    except FileNotFoundError:
        pass
    _patterns = entries
    return entries


def get_patterns() -> list[str]:
    return list(_patterns)


def _resolve(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        return {info[4][0] for info in infos}
    except socket.gaierror:
        return set()


def apply_blocklist() -> None:
    """Resolve block.conf entries and install blackhole routes."""
    if not _patterns:
        return

    hosts: set[str] = set()
    cidrs: set[str] = set()
    unresolved: list[str] = []

    for entry in _patterns:
        kind = _classify(entry)
        if kind == "cidr":
            cidrs.add(entry)
        elif kind == "ip":
            hosts.add(entry)
        else:
            ips = _resolve(entry)
            if not ips:
                unresolved.append(entry)
                continue
            hosts.update(ips)

    if unresolved:
        log_debug(f"Block: could not resolve {', '.join(unresolved)}")

    if not hosts and not cidrs:
        return

    log_info(f"Blocking {len(hosts)} host(s) and {len(cidrs)} CIDR(s)")

    for ip in hosts:
        result = subprocess.run(
            ["sudo", "route", "add", "-host", ip, "127.0.0.1", "-blackhole"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            _applied_hosts.add(ip)
        else:
            log_debug(f"Block route {ip}: {result.stderr.strip()}")

    for cidr in cidrs:
        result = subprocess.run(
            ["sudo", "route", "add", "-net", cidr, "127.0.0.1", "-blackhole"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            _applied_cidrs.add(cidr)
        else:
            log_debug(f"Block route {cidr}: {result.stderr.strip()}")


def remove_blocklist(timeout: float = 5.0, max_total: float = 3.0) -> None:
    """Remove every blackhole route we installed."""
    deadline = time.monotonic() + max_total

    def _remaining() -> float:
        return max(0.05, min(timeout, deadline - time.monotonic()))

    for ip in list(_applied_hosts):
        if time.monotonic() >= deadline:
            break
        subprocess.run(
            ["sudo", "route", "delete", "-host", ip],
            capture_output=True, timeout=_remaining(),
        )
    _applied_hosts.clear()

    for cidr in list(_applied_cidrs):
        if time.monotonic() >= deadline:
            break
        subprocess.run(
            ["sudo", "route", "delete", "-net", cidr],
            capture_output=True, timeout=_remaining(),
        )
    _applied_cidrs.clear()
