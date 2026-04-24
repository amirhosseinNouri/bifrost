import argparse
import os
import signal
import sys
from pathlib import Path
from threading import Event

from bifrost.blocklist import BLOCK_FILE, load_blocklist
from bifrost.config import (
    CONFIGS_DIR,
    CONNECTIONS_FILE,
    EXTERNAL_DNS,
    INTERNAL_DNS,
    LOG_FILE,
    Group,
    load_groups,
)
from bifrost.connections import get_entries as get_connection_entries
from bifrost.direct import DIRECT_FILE, load_direct_list
from bifrost.display import close_log_file, log_err, log_info, log_ok, log_warn, set_log_file
from bifrost.prober import rank_servers
from bifrost.stats import clear_stats, fmt_bytes, get_stats
from bifrost.vpn import full_cleanup, run_vpn_loop, set_system_dns


def _require_sudo():
    if os.geteuid() == 0:
        return
    log_err("This command must be run with sudo.")
    log_info("Usage: sudo bifrost <command> [options]")
    sys.exit(1)


def _resolve_group(args) -> Group:
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else CONFIGS_DIR
    groups = load_groups(config_dir)
    if not groups:
        log_err(f"No groups found in {config_dir} (expect subdirs with cred.json + *.ovpn)")
        sys.exit(1)
    name = args.group
    if not name:
        if len(groups) == 1:
            name = next(iter(groups))
        else:
            log_err(f"--group required. Available: {', '.join(sorted(groups))}")
            sys.exit(1)
    if name not in groups:
        log_err(f"Group '{name}' not found. Available: {', '.join(sorted(groups))}")
        sys.exit(1)
    return groups[name]


def cmd_run(args):
    """Main run command - connect to best VPN server with auto-reconnect."""
    group = _resolve_group(args)

    set_log_file(LOG_FILE)
    log_info(f"Group: {group.name} ({len(group.servers)} config(s))")

    stop_event = Event()

    def handle_signal(signum, frame):
        log_warn("Shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        run_vpn_loop(
            group.servers,
            group.credentials,
            stop_event,
            probe=not args.no_probe,
        )
    finally:
        close_log_file()
    log_ok("Goodbye.")


def cmd_probe(args):
    """Probe all servers and display reachability."""
    group = _resolve_group(args)

    log_info(f"Probing {len(group.servers)} server(s) in group '{group.name}'...")
    ranked = rank_servers(group.servers)

    print()
    for i, s in enumerate(ranked, 1):
        if s.latency_ms is not None:
            status = f"\033[32m{s.latency_ms:.0f}ms\033[0m"
        else:
            status = "\033[31munreachable\033[0m"
        print(f"  {i}. {s.name:8s}  {s.remote:30s}  :{s.port:<6d}  {status}")
    print()


def cmd_stats(args):
    """Show traffic stats per group and per config."""
    if args.clear:
        clear_stats()
        log_ok("Traffic stats cleared.")
        return

    data = get_stats()
    if not data:
        log_warn("No traffic stats recorded yet. Run 'bifrost run' first.")
        return

    grand_rx, grand_tx, grand_sessions = 0, 0, 0

    for group_name in sorted(data.keys()):
        group_entry = data[group_name]
        servers = group_entry.get("servers", {}) if isinstance(group_entry, dict) else {}
        if not servers:
            continue

        g_rx, g_tx, g_sessions = 0, 0, 0

        print()
        print(f"  \033[1m{group_name}\033[0m")
        print(f"  {'Config':<10s}  {'Download':>12s}  {'Upload':>12s}  {'Sessions':>8s}  {'OK':>5s}  {'Fail':>5s}  {'Reliab.':>7s}")
        print(f"  {'─' * 10}  {'─' * 12}  {'─' * 12}  {'─' * 8}  {'─' * 5}  {'─' * 5}  {'─' * 7}")

        for name in sorted(servers.keys()):
            entry = servers[name]
            rx, tx, sessions = entry["rx"], entry["tx"], entry["sessions"]
            successes = entry.get("successes", 0)
            failures = entry.get("failures", 0)
            reliability = (successes + 1) / (successes + failures + 2)
            g_rx += rx
            g_tx += tx
            g_sessions += sessions
            print(
                f"  {name:<10s}  {fmt_bytes(rx):>12s}  {fmt_bytes(tx):>12s}  "
                f"{sessions:>8d}  {successes:>5d}  {failures:>5d}  "
                f"{reliability * 100:>6.0f}%"
            )

        print(f"  {'─' * 10}  {'─' * 12}  {'─' * 12}  {'─' * 8}  {'─' * 5}  {'─' * 5}  {'─' * 7}")
        print(f"  {'subtotal':<10s}  {fmt_bytes(g_rx):>12s}  {fmt_bytes(g_tx):>12s}  {g_sessions:>8d}")

        grand_rx += g_rx
        grand_tx += g_tx
        grand_sessions += g_sessions

    print()
    print(f"  {'TOTAL':<10s}  {fmt_bytes(grand_rx):>12s}  {fmt_bytes(grand_tx):>12s}  {grand_sessions:>8d}")
    print()


def cmd_direct():
    """Show domains and CIDRs from the direct list."""
    domains, cidrs = load_direct_list()
    if not domains and not cidrs:
        log_warn(f"No direct entries configured. Edit {DIRECT_FILE}")
        return
    print()
    print(f"  Direct list (bypass VPN) — {DIRECT_FILE}")
    if domains:
        print(f"\n  Domains ({len(domains)}):")
        for d in domains:
            print(f"    .{d}")
    if cidrs:
        print(f"\n  CIDR ranges ({len(cidrs)}):")
        for c in cidrs:
            print(f"    {c}")
    print()


def cmd_blocks():
    """Show entries from block.conf."""
    entries = load_blocklist()
    print()
    print(f"  Blocklist (dropped via blackhole routes) — {BLOCK_FILE}")
    if not entries:
        log_warn(f"No block entries configured. Edit {BLOCK_FILE}")
        return
    print(f"\n  Entries ({len(entries)}):")
    for e in entries:
        print(f"    {e}")
    print()


def cmd_cleanup(args):
    """Restore system to pre-VPN state: DNS, routes, and any running clients."""
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else CONFIGS_DIR
    groups = load_groups(config_dir)
    all_servers = [s for g in groups.values() for s in g.servers]
    log_info("Cleaning up VPN state...")
    full_cleanup(all_servers)


def cmd_dns(args):
    """Set system DNS to internal or external resolvers from config.toml."""
    if args.internal == args.external:
        log_err("Specify exactly one of --internal or --external.")
        sys.exit(1)
    servers = INTERNAL_DNS if args.internal else EXTERNAL_DNS
    label = "internal" if args.internal else "external"
    log_info(f"Setting {label} DNS: {', '.join(servers)}")
    service = set_system_dns(servers)
    if not service:
        sys.exit(1)
    log_ok(f"DNS set on '{service}'.")


def cmd_connections():
    """Show unique outgoing TCP peers observed while VPN was running."""
    entries = get_connection_entries()
    print()
    print(f"  Observed connections — {CONNECTIONS_FILE}")
    if not entries:
        log_warn("No connections recorded yet. Start 'bifrost run' and wait.")
        return
    print(f"\n  Unique peers ({len(entries)}):")
    for e in entries:
        print(f"    {e}")
    print()


def main():
    _require_sudo()

    parser = argparse.ArgumentParser(
        prog="bifrost",
        description="Auto-connecting OpenVPN manager - finds the best server and keeps you connected.",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Connect to the best VPN server")
    run_p.add_argument(
        "-d",
        "--config-dir",
        help=f"Root configs directory (default: {CONFIGS_DIR})",
    )
    run_p.add_argument(
        "-g",
        "--group",
        help="Group name (subdirectory under configs/). Required when multiple groups exist.",
    )
    run_p.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip reachability probe; iterate configs in order (for servers that don't respond to probes but connect fine).",
    )

    probe_p = sub.add_parser("probe", help="Test reachability of all servers in a group")
    probe_p.add_argument(
        "-d",
        "--config-dir",
        help=f"Root configs directory (default: {CONFIGS_DIR})",
    )
    probe_p.add_argument(
        "-g",
        "--group",
        help="Group name (subdirectory under configs/). Required when multiple groups exist.",
    )

    stats_p = sub.add_parser("stats", help="Show download/upload traffic per group")
    stats_p.add_argument(
        "--clear",
        action="store_true",
        help="Reset all traffic stats",
    )

    sub.add_parser("direct", help="Show direct-list domains (bypass VPN)")
    sub.add_parser("blocks", help="Show blocklist entries (block.conf)")
    sub.add_parser("connections", help="Show unique outgoing peers (connections.log)")

    dns_p = sub.add_parser("dns", help="Set system DNS servers")
    dns_grp = dns_p.add_mutually_exclusive_group(required=True)
    dns_grp.add_argument(
        "--internal",
        action="store_true",
        help=f"Use internal DNS ({', '.join(INTERNAL_DNS)})",
    )
    dns_grp.add_argument(
        "--external",
        action="store_true",
        help=f"Use external DNS ({', '.join(EXTERNAL_DNS)})",
    )

    cleanup_p = sub.add_parser(
        "cleanup",
        help="Restore DNS, remove VPN routes, kill stray openvpn/sstpc/pppd",
    )
    cleanup_p.add_argument(
        "-d",
        "--config-dir",
        help=f"Root configs directory (default: {CONFIGS_DIR})",
    )

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "probe":
        cmd_probe(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "direct":
        cmd_direct()
    elif args.command == "blocks":
        cmd_blocks()
    elif args.command == "connections":
        cmd_connections()
    elif args.command == "dns":
        cmd_dns(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
