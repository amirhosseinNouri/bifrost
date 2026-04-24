"""Direct-list: domains and IP ranges that bypass the VPN tunnel."""

import subprocess
import time

from bifrost.config import DIRECT_FILE, INTERNAL_DNS
from bifrost.display import log_debug, log_warn

_domains: list[str] = []
_cidrs: list[str] = []
_original_gateway: str | None = None


def _is_cidr(entry: str) -> bool:
    """Check if an entry looks like a CIDR range (e.g. 185.0.0.0/8)."""
    return "/" in entry and entry[0].isdigit()


def load_direct_list() -> tuple[list[str], list[str]]:
    """Load domain patterns and CIDR ranges from direct.conf."""
    global _domains, _cidrs
    domains = []
    cidrs = []
    try:
        with open(DIRECT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if _is_cidr(line):
                    cidrs.append(line)
                else:
                    domains.append(line.lstrip(".").lower())
    except FileNotFoundError:
        pass
    _domains = domains
    _cidrs = cidrs
    return domains, cidrs


def get_direct_domains() -> list[str]:
    return list(_domains)


def get_direct_cidrs() -> list[str]:
    return list(_cidrs)


def is_direct(hostname: str) -> bool:
    """Check if a hostname matches the direct domain list."""
    h = hostname.lower()
    return any(h == d or h.endswith("." + d) for d in _domains)


def capture_default_gateway() -> str | None:
    """Save the default gateway before VPN connects."""
    global _original_gateway
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if "gateway:" in line:
                gw = line.split("gateway:")[-1].strip()
                _original_gateway = gw
                log_debug(f"Captured default gateway: {gw}")
                return gw
    except Exception as e:
        log_debug(f"Failed to capture gateway: {e}")
    return None


def get_original_gateway() -> str | None:
    return _original_gateway


def apply_direct_routes():
    """Set up split DNS and IP routes so direct list bypasses the VPN."""
    if not _domains and not _cidrs:
        return

    if _domains:
        _apply_split_dns()

    if _cidrs and _original_gateway:
        _apply_cidr_routes()


def _apply_split_dns():
    """Configure macOS split DNS for direct domains."""
    match_domains = " ".join(_domains)
    dns_addrs = " ".join(INTERNAL_DNS)

    scutil_cmd = (
        "d.init\n"
        f"d.add SupplementalMatchDomains * {match_domains}\n"
        "d.add SupplementalMatchDomainsNoSearch # 1\n"
        f"d.add ServerAddresses * {dns_addrs}\n"
        "set State:/Network/Service/bifrost-direct/DNS\n"
    )
    try:
        subprocess.run(
            ["sudo", "scutil"],
            input=scutil_cmd,
            capture_output=True, text=True, timeout=5,
        )
        log_debug("Split DNS configured for direct domains")
    except Exception as e:
        log_warn(f"Failed to set split DNS: {e}")

    # Route the DNS servers through the original gateway
    if _original_gateway:
        for dns in INTERNAL_DNS:
            subprocess.run(
                ["sudo", "route", "add", "-host", dns, _original_gateway],
                capture_output=True, timeout=5,
            )
        log_debug(f"Direct DNS routes added via {_original_gateway}")


def _apply_cidr_routes():
    """Add routes for CIDR ranges through the original gateway."""
    for cidr in _cidrs:
        result = subprocess.run(
            ["sudo", "route", "add", "-net", cidr, _original_gateway],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log_debug(f"Route add {cidr}: {result.stderr.strip()}")
    log_debug("All direct CIDR routes added")


def remove_direct_routes(timeout: float = 5.0, max_total: float = 3.0):
    """Remove all direct routing: split DNS + CIDR routes."""
    deadline = time.monotonic() + max_total

    def _remaining() -> float:
        return max(0.05, min(timeout, deadline - time.monotonic()))

    # Remove split DNS
    try:
        subprocess.run(
            ["sudo", "scutil"],
            input="remove State:/Network/Service/bifrost-direct/DNS\n",
            capture_output=True, text=True, timeout=_remaining(),
        )
    except Exception:
        pass

    # Remove DNS server routes
    if _original_gateway:
        for dns in INTERNAL_DNS:
            if time.monotonic() >= deadline:
                break
            subprocess.run(
                ["sudo", "route", "delete", "-host", dns],
                capture_output=True, timeout=_remaining(),
            )

    # Remove CIDR routes
    for cidr in _cidrs:
        if time.monotonic() >= deadline:
            break
        subprocess.run(
            ["sudo", "route", "delete", "-net", cidr],
            capture_output=True, timeout=_remaining(),
        )
