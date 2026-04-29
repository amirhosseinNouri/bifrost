import os
import re
import subprocess
import sys
import tempfile
import time
import json
import shutil
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from bifrost.config import (
    CONNECT_TIMEOUT,
    EXTERNAL_DNS,
    IDLE_COOLDOWN,
    IDLE_RX_TIMEOUT,
    OPENVPN_BIN,
    PROTO_OPENVPN,
    PROTO_SSTP,
    PROTO_V2RAY,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
    SSTPC_BIN,
    XRAY_BIN,
    GroupCredentials,
    VPNServer,
)
from bifrost.direct import (
    apply_direct_routes,
    capture_default_gateway,
    get_original_gateway,
    load_direct_list,
    remove_direct_routes,
)
from bifrost.blocklist import apply_blocklist, load_blocklist, remove_blocklist
from bifrost import connections as conn_sampler
from bifrost.display import GREEN, RED, RESET, log_debug, log_err, log_info, log_ok, log_warn
from bifrost.prober import rank_by_reliability, rank_servers
from bifrost.stats import (
    MIN_GOOD_SESSION_SECONDS,
    fmt_bytes,
    get_interface_bytes,
    get_stats,
    record_outcome,
    record_traffic,
)

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

# Sudo keepalive interval (seconds) — must be well under the default 5-min timeout
_SUDO_KEEPALIVE_INTERVAL = 120
_FAST_EXIT_PROC_WAIT = 0.8
_FAST_EXIT_CMD_TIMEOUT = 1.5
_FAST_EXIT_SETUP_JOIN = 0.2
_INTERACTIVE_SUDO_TIMEOUT = 30.0


def _run_shutdown_step(name: str, action) -> None:
    started = time.monotonic()
    try:
        action()
        elapsed = time.monotonic() - started
        log_info(f"Shutdown: {name} ({elapsed:.2f}s)")
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        log_warn(f"Shutdown: {name} timed out ({elapsed:.2f}s)")
    except Exception as e:
        elapsed = time.monotonic() - started
        log_warn(f"Shutdown: {name} failed ({elapsed:.2f}s): {e}")


def _sudo_keepalive_loop(stop_event: Event):
    """Refresh sudo credentials in the background so reconnects never prompt."""
    while not stop_event.is_set():
        subprocess.run(["sudo", "-v"], capture_output=True, timeout=10)
        stop_event.wait(_SUDO_KEEPALIVE_INTERVAL)


def _create_temp_file(content: str, prefix: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", prefix=f"bifrost_{prefix}_", suffix=".txt", delete=False
    )
    tmp.write(content)
    tmp.close()
    os.chmod(tmp.name, 0o600)
    return Path(tmp.name)


def _kill_proc(proc: subprocess.Popen, wait_timeout: float = _FAST_EXIT_PROC_WAIT):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=wait_timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=wait_timeout)
        except Exception:
            try:
                subprocess.run(
                    ["sudo", "kill", "-9", str(proc.pid)],
                    capture_output=True,
                    timeout=_FAST_EXIT_CMD_TIMEOUT,
                )
            except Exception:
                pass


def full_cleanup(servers: list[VPNServer] | None = None) -> None:
    """Restore the system to pre-VPN state.

    Kills every client we might have spawned, drops direct/blocklist routes,
    unpins SSTP host routes, and restores the original DNS.
    Safe to run repeatedly — every step is best-effort and idempotent.
    """
    # Kill any client processes we could have spawned. sstpc-spawned pppd
    # shows up with argv[0]=/dev/ttysNNN (so `killall pppd` misses it); match
    # on the /tmp/sstp-pppd.<rand> option file via pkill instead.
    for name in ("openvpn", "sstpc", "xray"):
        subprocess.run(["sudo", "killall", name], capture_output=True, timeout=5)
    subprocess.run(
        ["sudo", "pkill", "-f", "sstp-pppd\\."],
        capture_output=True, timeout=5,
    )
    # Give them a moment to release routes/ttys, then hard-kill stragglers.
    time.sleep(0.5)
    subprocess.run(["sudo", "killall", "-9", "sstpc"], capture_output=True, timeout=5)
    subprocess.run(
        ["sudo", "pkill", "-KILL", "-f", "sstp-pppd\\."],
        capture_output=True, timeout=5,
    )

    # Drop split-tunnel routes and blocklist blackholes.
    remove_direct_routes()
    remove_blocklist()

    # Unpin any SSTP server host routes we installed.
    if servers:
        for s in servers:
            if s.protocol == PROTO_SSTP:
                _unpin_host_route(s.remote)
            elif s.protocol == PROTO_V2RAY:
                _teardown_v2ray_routing(s)

    # Restore DNS last (it depends on /tmp backup files that survive crashes).
    try:
        _cleanup_dns(cmd_timeout=_INTERACTIVE_SUDO_TIMEOUT)
    except subprocess.TimeoutExpired:
        log_warn("DNS cleanup timed out waiting for sudo; continuing.")
    log_ok("Cleanup complete: DNS restored, routes removed, clients stopped.")


def _cleanup_dns(cmd_timeout: float = _FAST_EXIT_CMD_TIMEOUT):
    """Restore pre-VPN DNS in case down.sh didn't run (crash/kill)."""
    script = r"""
SERVICE=$(cat /tmp/bifrost_dns_service 2>/dev/null)
if [ -n "$SERVICE" ]; then
  if [ -s /tmp/bifrost_dns_backup ] \
     && ! grep -q "There aren't any DNS Servers" /tmp/bifrost_dns_backup; then
    networksetup -setdnsservers "$SERVICE" $(cat /tmp/bifrost_dns_backup)
  else
    networksetup -setdnsservers "$SERVICE" Empty
  fi
  rm -f /tmp/bifrost_dns_service /tmp/bifrost_dns_backup
fi
scutil <<EOF
remove State:/Network/Service/bifrost/DNS
remove State:/Network/Service/bifrost/OriginalDNS
remove State:/Network/Service/bifrost/PrimaryServiceID
EOF
rm -f /tmp/bifrost_psid
"""
    subprocess.run(
        ["sudo", "bash", "-c", script],
        capture_output=True, text=True,
        timeout=cmd_timeout,
    )
    subprocess.run(
        ["sudo", "dscacheutil", "-flushcache"],
        capture_output=True,
        timeout=cmd_timeout,
    )
    subprocess.run(
        ["sudo", "killall", "-HUP", "mDNSResponder"],
        capture_output=True,
        timeout=cmd_timeout,
    )
    log_debug("DNS cleanup done")


def set_system_dns(servers: list[str]) -> str | None:
    """Point the primary hardware service at the given DNS servers.

    Returns the network service name on success, None on failure. Does not
    touch the /tmp backup files used by VPN teardown.
    """
    if not servers:
        return None
    script = r"""
PRIMARY_IFACE=$(/usr/sbin/scutil <<< "show State:/Network/Global/IPv4" \
  | /usr/bin/awk '/PrimaryInterface/ {print $3}')

SERVICE=$(/usr/sbin/networksetup -listallhardwareports \
  | /usr/bin/awk -v iface="$PRIMARY_IFACE" '
      /^Hardware Port:/ { port = substr($0, index($0, ":") + 2); sub(/^ +/, "", port) }
      /^Device:/ { if ($2 == iface) { print port; exit } }
  ')

if [ -z "$SERVICE" ]; then
  echo "no-service" >&2
  exit 1
fi

/usr/sbin/networksetup -setdnsservers "$SERVICE" __DNS__ || exit 1
/usr/bin/dscacheutil -flushcache
/usr/bin/killall -HUP mDNSResponder 2>/dev/null
printf '%s' "$SERVICE"
"""
    script = script.replace("__DNS__", " ".join(servers))
    result = subprocess.run(
        ["sudo", "bash", "-c", script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log_err(f"Failed to set DNS: {result.stderr.strip() or 'unknown error'}")
        return None
    return result.stdout.strip() or None


def connect_to_server(
    server: VPNServer,
    credentials: GroupCredentials | None,
    auth_file: Path | None,
    askpass_file: Path | None,
    stop_event: Event,
) -> tuple[subprocess.Popen, str | None] | tuple[None, None]:
    """Dispatch to the appropriate client based on the server's protocol."""
    if server.protocol == PROTO_V2RAY:
        return _connect_v2ray(server, stop_event)
    if server.protocol == PROTO_SSTP:
        if credentials is None:
            log_err("Missing cred.json for SSTP group")
            return None, None
        return _connect_sstp(server, credentials, stop_event)
    return _connect_openvpn(server, auth_file, askpass_file, stop_event)


def _list_utun_ifaces() -> set[str]:
    try:
        out = subprocess.check_output(
            ["ifconfig", "-l"], text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return set()
    return {i for i in out.split() if i.startswith("utun")}


def _setup_v2ray_routing(server: VPNServer, tun_iface: str) -> None:
    """Steer all traffic through the xray tun, except for xray's own uplink.

    xray's `autoRoute: true` is unreliable on macOS — the tun interface comes
    up but the kernel keeps sending packets out the physical NIC, so the user
    sees 0 B/s. Replicate what OpenVPN's `redirect-gateway def1` does:

      1. Pin the proxy server IP to the *original* default gateway. Otherwise
         xray's outbound TLS to the VLESS server would be matched by the new
         tun-default and loop back into its own tun.
      2. Install two `/1` routes (0.0.0.0/1 and 128.0.0.0/1) via the tun.
         These cover the entire IPv4 space but are *more specific* than the
         existing default route, so the kernel prefers them without us
         having to remove and later restore the original default — which
         survives untouched and resumes serving traffic the moment our /1s
         are deleted on shutdown.
      3. Point system DNS at external resolvers (queries flow via the tun).
    """
    gw = get_original_gateway()
    if not gw:
        log_warn("V2Ray: original gateway unknown; skipping route install")
        return

    subprocess.run(
        ["sudo", "route", "delete", "-host", server.remote],
        capture_output=True, timeout=5,
    )
    subprocess.run(
        ["sudo", "route", "add", "-host", server.remote, gw],
        capture_output=True, timeout=5,
    )
    for net in ("0.0.0.0/1", "128.0.0.0/1"):
        subprocess.run(
            ["sudo", "route", "delete", "-net", net],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["sudo", "route", "add", "-net", net, "-interface", tun_iface],
            capture_output=True, timeout=5,
        )
    log_debug(f"V2Ray routes installed: {server.remote} via {gw}, default via {tun_iface}")
    _configure_sstp_dns()


def _teardown_v2ray_routing(server: VPNServer) -> None:
    """Undo what _setup_v2ray_routing did. Idempotent and best-effort."""
    for net in ("0.0.0.0/1", "128.0.0.0/1"):
        subprocess.run(
            ["sudo", "route", "delete", "-net", net],
            capture_output=True, timeout=5,
        )
    subprocess.run(
        ["sudo", "route", "delete", "-host", server.remote],
        capture_output=True, timeout=5,
    )


def _next_free_utun(existing: set[str]) -> str:
    """Pick the lowest utunN name not currently in use.

    xray's tun inbound requires `settings.name` to be `utunN` (literal `utun`
    + integer); it does not auto-allocate. Skipping `utun0..utun3` avoids the
    range macOS reserves for system services (Back to My Mac, FaceTime, etc.).
    """
    used = set()
    for name in existing:
        m = re.match(r"utun(\d+)$", name)
        if m:
            used.add(int(m.group(1)))
    n = 4
    while n in used:
        n += 1
    return f"utun{n}"


def _connect_v2ray(
    server: VPNServer,
    stop_event: Event,
) -> tuple[subprocess.Popen, str | None] | tuple[None, None]:
    """Start xray for a VLESS server and return (proc, tun_iface)."""
    log_info(f"Connecting to {server.name} ({server.remote}:{server.port}) via V2Ray...")
    if not server.vless_id:
        log_err("Invalid VLESS config: missing UUID")
        return None, None

    xray_bin = XRAY_BIN
    if not Path(xray_bin).exists():
        detected = shutil.which("xray")
        if detected:
            xray_bin = detected
        else:
            for candidate in ("/opt/homebrew/bin/xray", "/usr/local/bin/xray"):
                if Path(candidate).exists():
                    xray_bin = candidate
                    break
            else:
                log_err(
                    "xray binary not found. Install it (e.g. `brew install xray`) "
                    "or set XRAY_BIN in bifrost.config."
                )
                return None, None

    pre_utun = _list_utun_ifaces()
    tun_name = _next_free_utun(pre_utun)
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "tun-in",
                "protocol": "tun",
                "settings": {
                    "name": tun_name,
                    "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
                    "mtu": 1400,
                    "autoRoute": True,
                    "strictRoute": False,
                },
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "tag": "proxy",
                "settings": {
                    "vnext": [
                        {
                            "address": server.remote,
                            "port": server.port,
                            "users": [
                                {
                                    "id": server.vless_id,
                                    "encryption": "none",
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": server.vless_network or "tcp",
                    "security": "tls" if server.vless_tls else "none",
                },
            },
            {"protocol": "freedom", "tag": "direct"},
        ],
        "routing": {"domainStrategy": "AsIs", "rules": []},
    }
    if server.vless_network == "ws":
        cfg["outbounds"][0]["streamSettings"]["wsSettings"] = {
            "path": server.vless_path or "/",
            "headers": {"Host": server.vless_host or server.remote},
        }
    if server.vless_tls:
        cfg["outbounds"][0]["streamSettings"]["tlsSettings"] = {
            "serverName": server.vless_sni or server.remote,
            "allowInsecure": server.vless_allow_insecure,
        }

    config_file = _create_temp_file(json.dumps(cfg), "xray")
    # `-format json` is required because _create_temp_file uses a .txt suffix;
    # xray infers the config format from the file extension and otherwise
    # refuses to load it ("Failed to get format of ...").
    cmd = ["sudo", xray_bin, "run", "-format", "json", "-c", str(config_file)]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except OSError as e:
        log_err(f"Failed to start xray: {e}")
        try:
            config_file.unlink()
        except OSError:
            pass
        return None, None

    # Drain xray output via a background thread so we never block on readline
    # while the process is dying. Without this, the loop reads xray's banner,
    # sees proc.poll() != None on the next iteration, and breaks before the
    # actual error line ("Failed to start: ...") arrives — making startup
    # failures completely silent.
    line_queue: Queue = Queue()
    reader = Thread(target=_stdout_reader, args=(proc, line_queue), daemon=True)
    reader.start()

    deadline = time.monotonic() + CONNECT_TIMEOUT
    captured: list[str] = []
    tun_up = False
    proc_exited = False
    while time.monotonic() < deadline and not stop_event.is_set():
        try:
            line = line_queue.get(timeout=0.2)
        except Empty:
            line = ""
        if line is None:
            proc_exited = True
        elif line:
            captured.append(line)
            log_debug(f"[{server.name}] {line}")

        if not tun_up and tun_name in _list_utun_ifaces():
            tun_up = True

        if tun_up and proc.poll() is None:
            _setup_v2ray_routing(server, tun_name)
            log_ok(f"{GREEN}✓{RESET} Connected to {server.name} on {tun_name}")
            return proc, tun_name

        if proc_exited or proc.poll() is not None:
            # Drain any remaining output before reporting.
            while True:
                try:
                    extra = line_queue.get(timeout=0.1)
                except Empty:
                    break
                if extra is None:
                    break
                captured.append(extra)
                log_debug(f"[{server.name}] {extra}")
            break

    if proc.poll() is None:
        _kill_proc(proc)
    if captured:
        tail = " | ".join(captured[-4:])
        log_warn(f"[{server.name}] xray output: {tail}")
    try:
        config_file.unlink()
    except OSError:
        pass
    return None, None


def _connect_openvpn(
    server: VPNServer,
    auth_file: Path | None,
    askpass_file: Path | None,
    stop_event: Event,
) -> tuple[subprocess.Popen, str | None] | tuple[None, None]:
    """Returns (process, tun_iface) on success."""
    log_info(f"Connecting to {server.name} ({server.remote}:{server.port})...")

    up_script = str(SCRIPTS_DIR / "up.sh")
    down_script = str(SCRIPTS_DIR / "down.sh")

    cmd = [
        "sudo", OPENVPN_BIN,
        "--config", str(server.config_path),
    ]
    if auth_file is not None:
        cmd += ["--auth-user-pass", str(auth_file)]
    if askpass_file is not None:
        cmd += ["--askpass", str(askpass_file)]
    cmd += [
        "--auth-nocache",
        "--connect-retry", "1", "2",
        "--connect-timeout", "8",
        "--resolv-retry", "2",
        "--connect-retry-max", "2",
        "--data-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM",
        "--script-security", "2",
        "--up", up_script,
        "--down", down_script,
        "--verb", "3",
        "--mute", "0",
    ]

    try:
        env = os.environ.copy()
        env["BIFROST_EXTERNAL_DNS"] = " ".join(EXTERNAL_DNS)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
    except OSError as e:
        log_err(f"Failed to start openvpn: {e}")
        return None, None

    deadline = time.monotonic() + CONNECT_TIMEOUT
    reset_count = 0
    tun_iface = None

    while time.monotonic() < deadline and not stop_event.is_set():
        if proc.poll() is not None:
            log_debug(f"openvpn exited with code {proc.returncode}")
            break
        try:
            line = proc.stdout.readline()
        except Exception:
            break
        if not line:
            time.sleep(0.05)
            continue

        line = line.strip()
        if not line:
            continue

        log_debug(f"[{server.name}] {line}")

        # Capture utun device
        if "utun" in line and ("Opened" in line or "opened" in line):
            tun_match = re.search(r"(utun\d+)", line)
            if tun_match:
                tun_iface = tun_match.group(1)

        if "Initialization Sequence Completed" in line:
            log_ok(f"{GREEN}✓{RESET} Connected to {server.name}")
            return proc, tun_iface
        if "AUTH_FAILED" in line:
            log_err(f"Auth failed on {server.name}")
            break
        if "Connection refused" in line:
            log_err(f"Connection refused by {server.name}")
            break
        if "SIGTERM" in line or "process exiting" in line:
            break
        if "Connection reset" in line or "Restart pause" in line:
            reset_count += 1
            if reset_count >= 2:
                log_debug(f"[{server.name}] too many resets, giving up")
                break

    if proc.poll() is None:
        _kill_proc(proc)
    return None, None


def _interface_up(name: str) -> bool:
    try:
        subprocess.check_call(
            ["ifconfig", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _detect_new_ppp_iface(existing: set[str]) -> str | None:
    """Return a ppp* interface name that wasn't present before connect began."""
    try:
        out = subprocess.check_output(
            ["ifconfig", "-l"], text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    for iface in out.split():
        if iface.startswith("ppp") and iface not in existing:
            return iface
    return None


def _list_ppp_ifaces() -> set[str]:
    try:
        out = subprocess.check_output(
            ["ifconfig", "-l"], text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return set()
    return {i for i in out.split() if i.startswith("ppp")}


def _sstp_log_path(server_name: str) -> Path:
    return Path(f"/tmp/bifrost_sstpc_{server_name}.log")


def _pppd_log_path(server_name: str) -> Path:
    return Path(f"/tmp/bifrost_pppd_{server_name}.log")


def _pppd_opts_path(server_name: str) -> Path:
    return Path(f"/tmp/bifrost_pppd_{server_name}.opts")


def _sudo_prefix() -> list[str]:
    """sudo prefix only when not already root — avoids a redundant re-exec."""
    return [] if os.geteuid() == 0 else ["sudo"]


def _kill_sstp_orphans():
    """Kill any lingering sstpc/pppd so we start each attempt from a clean slate.

    macOS pppd orphans from a killed sstpc get reparented to launchd and hang
    on their pty forever, occupying ttys and confusing the next attempt.

    Note on pppd matching: sstp-client invokes pppd with argv[0] set to the tty
    path (e.g. `/dev/ttys013`), so the kernel's `comm` field becomes `ttys013`
    and `killall pppd` matches nothing. Match on the full command line via
    pkill against the `/tmp/sstp-pppd.<rand>` option file that sstpc always
    passes — unique to sstpc-spawned pppd and present in every invocation.
    """
    subprocess.run(["sudo", "killall", "-9", "sstpc"], capture_output=True, timeout=5)
    subprocess.run(
        ["sudo", "pkill", "-KILL", "-f", "sstp-pppd\\."],
        capture_output=True, timeout=5,
    )


def _pin_host_route(ip: str) -> None:
    """Pin the SSTP server to the current default gateway so pppd's default
    route can't send SSTP packets back into the tunnel (routing loop)."""
    try:
        gw_out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=5,
        )
        gw = None
        for line in gw_out.stdout.splitlines():
            if "gateway:" in line:
                gw = line.split("gateway:")[-1].strip()
                break
        if not gw:
            return
        # Replace any previous pin (best-effort).
        subprocess.run(
            ["sudo", "route", "delete", "-host", ip],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["sudo", "route", "add", "-host", ip, gw],
            capture_output=True, timeout=5,
        )
        log_debug(f"Pinned {ip} via {gw}")
    except Exception as e:
        log_debug(f"Host-route pin for {ip} failed: {e}")


def _unpin_host_route(ip: str) -> None:
    try:
        subprocess.run(
            ["sudo", "route", "delete", "-host", ip],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


_SSTP_DNS_SETUP = r"""
PRIMARY_IFACE=$(/usr/sbin/scutil <<< "show State:/Network/Global/IPv4" \
  | /usr/bin/awk '/PrimaryInterface/ {print $3}')

SERVICE=$(/usr/sbin/networksetup -listallhardwareports \
  | /usr/bin/awk -v iface="$PRIMARY_IFACE" '
      /^Hardware Port:/ { port = substr($0, index($0, ":") + 2); sub(/^ +/, "", port) }
      /^Device:/ { if ($2 == iface) { print port; exit } }
  ')

if [ -n "$SERVICE" ]; then
  /usr/sbin/networksetup -getdnsservers "$SERVICE" > /tmp/bifrost_dns.$$ 2>/dev/null
  /bin/mv -f /tmp/bifrost_dns.$$ /tmp/bifrost_dns_backup
  /usr/bin/printf '%s' "$SERVICE" > /tmp/bifrost_dns_service
  /usr/sbin/networksetup -setdnsservers "$SERVICE" __DNS__
fi

/usr/bin/dscacheutil -flushcache
/usr/bin/killall -HUP mDNSResponder 2>/dev/null
"""


def _configure_sstp_dns():
    """Point the primary hardware service at public DNS.

    pppd on macOS doesn't apply usepeerdns system-wide, and openvpn's up.sh
    doesn't run here. Reuses the /tmp files that _cleanup_dns() reads so the
    shutdown path restores the backup unchanged.
    """
    script = _SSTP_DNS_SETUP.replace("__DNS__", " ".join(EXTERNAL_DNS))
    subprocess.run(["sudo", "bash", "-c", script], capture_output=True)


def _connect_sstp(
    server: VPNServer,
    credentials: GroupCredentials,
    stop_event: Event,
) -> tuple[subprocess.Popen, str | None] | tuple[None, None]:
    """Start sstpc for this server. Returns (process, ppp_iface) on success.

    sstpc output is redirected to a logfile (not a pipe) so the writer never
    blocks while the caller is busy installing routes — a stalled pipe kills
    the SSTP control loop and the server closes the TLS socket.
    """
    log_info(f"Connecting to {server.name} ({server.remote}:{server.port}) via SSTP...")

    if not credentials.username:
        log_err("SSTP requires a username in cred.json")
        return None, None

    # Clean slate: previous sstpc/pppd may be wedged holding ttys.
    _kill_sstp_orphans()

    pre_existing = _list_ppp_ifaces()

    # Pin the SSTP server to the current default gateway so pppd's default
    # route can't redirect SSTP control packets back into the tunnel.
    _pin_host_route(server.remote)

    target = f"{server.remote}:{server.port}" if server.port else server.remote
    pppd_log = _pppd_log_path(server.name)
    # Pre-truncate so we only see output from this attempt.
    try:
        pppd_log.write_text("")
    except OSError:
        pass

    # pppd options for Microsoft-style SSTP servers. Written to an options
    # file (one per line) rather than passed as argv tokens. Reason: sstpc
    # 1.0.20 sstp_pppd_start() has `const char *args[20]` on the stack and
    # no bounds check on the user-options copy loop; already-populated
    # internal slots leave ~8 for us, so passing 16 tokens smashes the
    # canary (__stack_chk_fail / Abort trap: 6). `file <path>` is 2 tokens
    # and fits easily. See sstp-client/src/sstp-pppd.c:512–592.
    #
    # MPPE note: macOS pppd 2.4.2 has no MPPE kernel support and the
    # Homebrew sstp-client build is compiled with --disable-ppp-plugin, so
    # the MPPE key-extraction plugin isn't available either. If we set
    # `require-mppe-128`, pppd kills the link with "MPPE required but not
    # available" the moment CHAP succeeds. Use `nomppe` so pppd tolerates
    # a no-encryption CCP outcome. The outer SSL/TLS tunnel still encrypts
    # all PPP frames; the server must be configured to allow no-encryption
    # or optional-encryption (MikroTik: /ppp profile ... use-encryption=no
    # or =yes; default profiles in modern RouterOS accept no-MPPE).
    pppd_opts_path = _pppd_opts_path(server.name)
    pppd_opts_path.write_text(
        "require-mschap-v2\n"
        "refuse-pap\n"
        "refuse-chap\n"
        "refuse-mschap\n"
        "refuse-eap\n"
        "nomppe\n"
        "noccp\n"
        "noauth\n"
        "noipdefault\n"
        "defaultroute\n"
        "usepeerdns\n"
        "noaccomp\n"
        "nopcomp\n"
        "novj\n"
        "novjccomp\n"
        "nobsdcomp\n"
        "nodeflate\n"
        "debug\n"
        f"logfile {pppd_log}\n"
    )

    cmd = [
        *_sudo_prefix(), SSTPC_BIN,
        # Verbose logging. Without --log-level sstpc is silent even with
        # --log-stderr, which makes SSTP failures impossible to diagnose.
        "--log-level", "4",
        "--log-lineno",
        "--log-stderr",
        "--cert-warn",
        "--save-server-route",
        "--user", credentials.username,
        "--password", credentials.password,
        target,
        "file", str(pppd_opts_path),
    ]

    log_path = _sstp_log_path(server.name)
    try:
        log_fd = open(log_path, "w")
    except OSError as e:
        log_err(f"Failed to open sstp log: {e}")
        return None, None

    try:
        proc = subprocess.Popen(
            cmd, stdout=log_fd, stderr=subprocess.STDOUT,
        )
    except OSError as e:
        log_err(f"Failed to start sstpc: {e}")
        log_fd.close()
        return None, None
    finally:
        # The child owns the fd now; our copy can be closed.
        try:
            log_fd.close()
        except OSError:
            pass

    deadline = time.monotonic() + CONNECT_TIMEOUT
    while time.monotonic() < deadline and not stop_event.is_set():
        if proc.poll() is not None:
            sstp_tail = _tail_file(log_path, 8)
            pppd_tail = _tail_file(pppd_log, 8)
            log_debug(
                f"sstpc exited with code {proc.returncode}\n"
                f"  sstpc log ({log_path}): {sstp_tail or '<empty>'}\n"
                f"  pppd log ({pppd_log}): {pppd_tail or '<empty>'}"
            )
            return None, None

        candidate = _detect_new_ppp_iface(pre_existing)
        if candidate:
            # Give pppd a moment to finish IPCP and push DNS/routes.
            time.sleep(2)
            _configure_sstp_dns()
            log_ok(f"{GREEN}✓{RESET} Connected to {server.name} on {candidate}")
            return proc, candidate

        time.sleep(0.3)

    if proc.poll() is None:
        _kill_proc(proc)
    return None, None


def _tail_file(path: Path, n: int) -> str:
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-n:])


def _stdout_reader(proc: subprocess.Popen, queue: Queue):
    """Read openvpn stdout in a background thread so it doesn't block."""
    try:
        for line in proc.stdout:
            queue.put(line.strip())
    except Exception:
        pass
    queue.put(None)  # sentinel


def monitor_connection(
    proc: subprocess.Popen,
    server: VPNServer,
    tun_iface: str | None,
    stop_event: Event,
) -> tuple[str, int, int]:
    """Monitor connection with real-time traffic display.

    Returns (reason, session_rx, session_tx). Session totals are tracked from
    the last poll before the tun interface is torn down, so they survive
    Ctrl+C teardown where a post-mortem netstat would find the interface gone.
    """
    prev_rx, prev_tx = 0, 0
    initial = None
    session_rx, session_tx = 0, 0

    if tun_iface:
        initial = get_interface_bytes(tun_iface)
        if initial:
            prev_rx, prev_tx = initial
            log_debug(f"[traffic] initial: rx={initial[0]} tx={initial[1]} iface={tun_iface}")

    # If the client exposes stdout (openvpn), read it in a background thread so
    # we don't block on readline. For sstpc we route stdout to a logfile to
    # avoid pipe-stall, so there's nothing to read here.
    line_queue: Queue | None = None
    if proc.stdout is not None:
        line_queue = Queue()
        reader = Thread(target=_stdout_reader, args=(proc, line_queue), daemon=True)
        reader.start()

    last_display = time.monotonic()
    last_rx_activity = time.monotonic()

    while not stop_event.is_set():
        if proc.poll() is not None:
            _clear_status_line()
            return "process_exited", session_rx, session_tx

        # Without a stdout pipe (sstpc), detect link loss via the interface.
        if line_queue is None and tun_iface and not _interface_up(tun_iface):
            _clear_status_line()
            return "interface_down", session_rx, session_tx

        # Drain all available lines (non-blocking)
        while line_queue is not None:
            try:
                line = line_queue.get_nowait()
            except Empty:
                break
            if line is None:
                _clear_status_line()
                return "process_exited", session_rx, session_tx
            log_debug(f"[monitor] {line}")
            if "SIGTERM" in line or "process exiting" in line:
                _clear_status_line()
                return "terminated", session_rx, session_tx
            if "Restart pause" in line or "SIGUSR1" in line:
                _clear_status_line()
                return "connection_reset", session_rx, session_tx

        # Update traffic display every second
        now = time.monotonic()
        if tun_iface and now - last_display >= 1.0:
            current = get_interface_bytes(tun_iface)
            if current:
                cur_rx, cur_tx = current
                speed_rx = cur_rx - prev_rx
                speed_tx = cur_tx - prev_tx
                session_rx = cur_rx - (initial[0] if initial else 0)
                session_tx = cur_tx - (initial[1] if initial else 0)
                log_debug(f"[traffic] cur: rx={cur_rx} tx={cur_tx} | speed: rx={speed_rx} tx={speed_tx}")
                prev_rx, prev_tx = cur_rx, cur_tx
                _print_status(server.name, speed_rx, speed_tx, session_rx, session_tx)

                # Server-unresponsive detection: tx is flowing but rx is flat
                if speed_rx > 0:
                    last_rx_activity = now
                elif speed_tx > 0 and now - last_rx_activity > IDLE_RX_TIMEOUT:
                    _clear_status_line()
                    log_warn(
                        f"[{server.name}] no download for {now - last_rx_activity:.0f}s "
                        f"while uploading — server unresponsive"
                    )
                    return "idle_timeout", session_rx, session_tx
            last_display = now

        time.sleep(0.5)

    _clear_status_line()
    return "stopped", session_rx, session_tx


def _print_status(name: str, speed_rx: int, speed_tx: int, total_rx: int, total_tx: int):
    line = (
        f"\r\033[K\033[36m{name}\033[0m  "
        f"\033[32m▼\033[0m {fmt_bytes(speed_rx)}/s  "
        f"\033[33m▲\033[0m {fmt_bytes(speed_tx)}/s  "
        f"\033[2m│\033[0m  "
        f"total \033[32m▼\033[0m {fmt_bytes(total_rx)}  "
        f"\033[33m▲\033[0m {fmt_bytes(total_tx)}"
    )
    sys.stdout.write(line)
    sys.stdout.flush()


def _clear_status_line():
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def run_vpn_loop(
    servers: list[VPNServer],
    credentials: GroupCredentials | None,
    stop_event: Event,
    probe: bool = True,
):
    auth_file: Path | None = None
    askpass_file: Path | None = None
    if credentials is not None:
        if credentials.username:
            auth_file = _create_temp_file(
                f"{credentials.username}\n{credentials.password}\n", "auth",
            )
        askpass_value = credentials.secret if credentials.secret else credentials.password
        askpass_file = _create_temp_file(askpass_value + "\n", "askpass")
    retry_count = 0
    cooldown: dict[str, float] = {}

    # Clean stale DNS from a previous crash.
    # This can run before sudo credentials are cached, so allow password entry.
    try:
        _cleanup_dns(cmd_timeout=_INTERACTIVE_SUDO_TIMEOUT)
    except subprocess.TimeoutExpired:
        log_warn("Startup DNS cleanup timed out waiting for sudo; continuing.")
    remove_direct_routes()

    # Load direct-list (domains + CIDRs) and capture the default gateway before VPN.
    # V2Ray needs the gateway even with no direct list — it pins the proxy server
    # IP to the original gateway so xray's outbound TLS doesn't loop through its
    # own tun. Capturing unconditionally is also harmless when no VPN protocol
    # would have used it.
    direct_domains, direct_cidrs = load_direct_list()
    has_direct = bool(direct_domains or direct_cidrs)
    capture_default_gateway()
    if direct_domains:
        log_info(f"Direct domains: {', '.join('.' + d for d in direct_domains)}")

    # Load block.conf up front; routes are installed per-connection (fresh DNS)
    blocked = load_blocklist()
    if blocked:
        log_info(f"Loaded {len(blocked)} blocklist entr{'y' if len(blocked) == 1 else 'ies'}")

    # Stale blackhole routes from a previous crashed run
    remove_blocklist()

    # Keep sudo alive so reconnects never prompt for password
    keepalive_thread = Thread(
        target=_sudo_keepalive_loop, args=(stop_event,), daemon=True,
    )
    keepalive_thread.start()
    log_debug("Sudo keepalive started")

    # Background sampler logs unique outgoing TCP peers to connections.log
    conn_sampler.start(stop_event)
    log_debug("Connection sampler started")

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            cooldown = {n: t for n, t in cooldown.items() if t > now}

            if probe:
                log_info("Probing servers...")
                ranked = rank_servers(servers)
                available = [s for s in ranked if s.name not in cooldown]
                if not available and ranked:
                    log_warn("All servers on cooldown, resetting.")
                    cooldown.clear()
                    available = ranked
                reachable = [s for s in available if s.latency_ms is not None]
                reachable = rank_by_reliability(reachable, get_stats())
            else:
                available = [s for s in servers if s.name not in cooldown]
                if not available and servers:
                    log_warn("All servers on cooldown, resetting.")
                    cooldown.clear()
                    available = list(servers)
                reachable = available

            if cooldown:
                log_info(f"Skipping cooldown: {', '.join(sorted(cooldown))}")

            if not reachable:
                delay = min(RECONNECT_DELAY_INITIAL * (2 ** retry_count), RECONNECT_DELAY_MAX)
                log_err(f"No reachable servers. Retrying in {delay}s...")
                stop_event.wait(delay)
                retry_count += 1
                continue

            if probe:
                log_ok(f"{len(reachable)} server(s) reachable (best: {reachable[0].name} {reachable[0].latency_ms:.0f}ms)")
            else:
                if len(reachable) > 1:
                    log_ok(f"{len(reachable)} server(s) to try (first: {reachable[0].name})")

            connected = False
            for server in reachable:
                if stop_event.is_set():
                    break

                connect_start = time.monotonic()
                proc, tun_iface = connect_to_server(
                    server, credentials, auth_file, askpass_file, stop_event,
                )
                if proc is None:
                    if server.protocol == PROTO_SSTP:
                        _unpin_host_route(server.remote)
                    record_outcome(server.group, server.name, success=False)
                    log_warn(f"Failed {server.name}, trying next...")
                    continue

                connected = True
                retry_count = 0

                # Apply split-tunnel routes and blocklist in the background so
                # monitor_connection starts watching the process immediately.
                # Installing 861 routes synchronously blocks ~12 s, during
                # which we can't react to a disconnect or a Ctrl-C.
                def _post_connect_setup():
                    if has_direct:
                        apply_direct_routes()
                    apply_blocklist()

                setup_thread = Thread(target=_post_connect_setup, daemon=True)
                setup_thread.start()

                reason, session_rx, session_tx = monitor_connection(
                    proc, server, tun_iface, stop_event,
                )
                # On user stop, don't spend up to 2s waiting for background
                # post-connect route setup; timed shutdown cleanup will handle it.
                setup_thread.join(
                    timeout=_FAST_EXIT_SETUP_JOIN if stop_event.is_set() else 2
                )
                log_err(f"{RED}✗{RESET} Disconnected from {server.name} ({reason})")
                if server.protocol == PROTO_SSTP and reason == "process_exited":
                    tail = _tail_file(_sstp_log_path(server.name), 3)
                    if tail:
                        log_warn(f"[{server.name}] sstpc: {tail}")

                if reason == "idle_timeout":
                    cooldown[server.name] = time.monotonic() + IDLE_COOLDOWN
                    log_warn(f"Cooling down {server.name} for {IDLE_COOLDOWN}s")

                # For reconnect paths, drop routes immediately so the next server
                # starts from a clean state. For user stop/terminate, defer to the
                # timed shutdown cleanup in finally to avoid duplicate slow work.
                should_prepare_reconnect = (
                    not stop_event.is_set()
                    and reason not in ("terminated", "stopped")
                )
                if should_prepare_reconnect:
                    remove_direct_routes()
                    remove_blocklist()
                    if server.protocol == PROTO_SSTP:
                        _unpin_host_route(server.remote)
                        _kill_sstp_orphans()
                    elif server.protocol == PROTO_V2RAY:
                        _teardown_v2ray_routing(server)

                if session_rx > 0 or session_tx > 0:
                    record_traffic(server.group, server.name, session_rx, session_tx)

                # Reliability heuristic: a session only counts as a success if
                # the user got meaningful connectivity (download traffic and
                # enough time). User-initiated teardown is neutral.
                if reason not in ("terminated", "stopped"):
                    duration = time.monotonic() - connect_start
                    good = session_rx > 0 and duration >= MIN_GOOD_SESSION_SECONDS
                    record_outcome(server.group, server.name, success=good)

                _kill_proc(proc)

                if stop_event.is_set():
                    break

                log_info("Reconnecting...")
                break

            if not connected:
                delay = min(RECONNECT_DELAY_INITIAL * (2 ** retry_count), RECONNECT_DELAY_MAX)
                log_warn(f"All servers failed. Retrying in {delay}s...")
                stop_event.wait(delay)
                retry_count += 1

    finally:
        shutdown_started = time.monotonic()
        log_info("Shutdown: starting cleanup...")

        for f in (auth_file, askpass_file):
            if f is None:
                continue
            try:
                f.unlink()
            except OSError:
                pass

        _run_shutdown_step(
            "killall openvpn",
            lambda: subprocess.run(
                ["sudo", "killall", "openvpn"],
                capture_output=True,
                timeout=_FAST_EXIT_CMD_TIMEOUT,
            ),
        )
        _run_shutdown_step(
            "killall sstpc",
            lambda: subprocess.run(
                ["sudo", "killall", "sstpc"],
                capture_output=True,
                timeout=_FAST_EXIT_CMD_TIMEOUT,
            ),
        )
        _run_shutdown_step(
            "killall xray",
            lambda: subprocess.run(
                ["sudo", "killall", "xray"],
                capture_output=True,
                timeout=_FAST_EXIT_CMD_TIMEOUT,
            ),
        )
        _run_shutdown_step(
            "killall pppd",
            lambda: subprocess.run(
                ["sudo", "killall", "pppd"],
                capture_output=True,
                timeout=_FAST_EXIT_CMD_TIMEOUT,
            ),
        )

        # Best-effort: unpin every sstp server host-route we might have added.
        _run_shutdown_step(
            "unpin sstp host routes",
            lambda: [
                _unpin_host_route(s.remote)
                for s in servers
                if s.protocol == PROTO_SSTP
            ],
        )
        _run_shutdown_step(
            "remove v2ray routes",
            lambda: [
                _teardown_v2ray_routing(s)
                for s in servers
                if s.protocol == PROTO_V2RAY
            ],
        )
        _run_shutdown_step(
            "remove direct routes",
            lambda: remove_direct_routes(
                timeout=_FAST_EXIT_CMD_TIMEOUT,
                max_total=_FAST_EXIT_CMD_TIMEOUT,
            ),
        )
        _run_shutdown_step(
            "remove blocklist routes",
            lambda: remove_blocklist(
                timeout=_FAST_EXIT_CMD_TIMEOUT,
                max_total=_FAST_EXIT_CMD_TIMEOUT,
            ),
        )
        _run_shutdown_step(
            "restore DNS",
            lambda: _cleanup_dns(cmd_timeout=_FAST_EXIT_CMD_TIMEOUT),
        )

        total = time.monotonic() - shutdown_started
        log_info(f"Shutdown: complete ({total:.2f}s)")
        log_info("VPN stopped.")
