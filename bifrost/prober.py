import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from bifrost.config import PROBE_TIMEOUT, VPNServer


def probe_server(server: VPNServer) -> VPNServer:
    """Test reachability of a VPN server via TCP connect, measure latency."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PROBE_TIMEOUT)
    start = time.monotonic()
    try:
        sock.connect((server.remote, server.port))
        elapsed = (time.monotonic() - start) * 1000
        server.latency_ms = elapsed
    except (socket.timeout, OSError):
        server.latency_ms = None
    finally:
        sock.close()
    return server


def rank_servers(servers: list[VPNServer]) -> list[VPNServer]:
    """Probe all servers concurrently and return sorted by latency (best first).

    Unreachable servers are placed at the end.
    """
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        futures = {pool.submit(probe_server, s): s for s in servers}
        results = []
        for fut in as_completed(futures):
            results.append(fut.result())

    reachable = sorted(
        [s for s in results if s.latency_ms is not None],
        key=lambda s: s.latency_ms,
    )
    unreachable = [s for s in results if s.latency_ms is None]
    return reachable + unreachable


def _reliability(successes: int, failures: int) -> float:
    """Laplace-smoothed success rate: new servers default to 0.5 (neutral)."""
    return (successes + 1) / (successes + failures + 2)


def rank_by_reliability(
    servers: list[VPNServer], stats_data: dict,
) -> list[VPNServer]:
    """Re-rank already-probed reachable servers by latency weighted with
    historical reliability.

    score = latency_ms / reliability   (lower is better)

    Servers with no recorded history get reliability=0.5 so they aren't
    penalised relative to well-known ones but also can't displace a proven
    reliable server purely on lower ping.
    """
    def score(s: VPNServer) -> float:
        group_entry = stats_data.get(s.group) or {}
        server_entry = (group_entry.get("servers") or {}).get(s.name) or {}
        rel = _reliability(
            server_entry.get("successes", 0), server_entry.get("failures", 0),
        )
        latency = s.latency_ms if s.latency_ms is not None else float("inf")
        return latency / rel

    return sorted(servers, key=score)
