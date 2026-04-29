import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None

APP_HOME = Path.home() / ".bifrost"
APP_CONFIG_FILE = APP_HOME / "config.toml"

DEFAULT_CONFIGS_DIR = APP_HOME / "configs"
DEFAULT_DIRECT_FILE = APP_HOME / "direct.conf"
DEFAULT_BLOCK_FILE = APP_HOME / "block.conf"
DEFAULT_STATS_FILE = APP_HOME / "stats.json"
DEFAULT_LOG_FILE = APP_HOME / "bifrost.log"
DEFAULT_CONNECTIONS_FILE = APP_HOME / "connections.log"
DEFAULT_INTERNAL_DNS = ["78.157.42.100", "78.157.42.101"]
DEFAULT_EXTERNAL_DNS = ["8.8.8.8", "1.1.1.1"]

OPENVPN_BIN = "/usr/local/opt/openvpn/sbin/openvpn"
SSTPC_BIN = "/usr/local/sbin/sstpc"
XRAY_BIN = "/usr/local/bin/xray"

# Default SSTP port (SSTP tunnels over HTTPS)
DEFAULT_SSTP_PORT = 443

PROTO_OPENVPN = "openvpn"
PROTO_SSTP = "sstp"
PROTO_V2RAY = "v2ray"

CRED_FILENAME = "cred.json"

# Reachability test timeout (seconds)
PROBE_TIMEOUT = 3

# Reconnect settings
RECONNECT_DELAY_INITIAL = 1
RECONNECT_DELAY_MAX = 15

# Per-server connect timeout before moving to next
CONNECT_TIMEOUT = 20

# If the VPN is actively uploading but receives no download bytes
# for this long, assume the server is unresponsive and switch.
IDLE_RX_TIMEOUT = 30

# After an idle-timeout switch, skip that server for this many seconds.
IDLE_COOLDOWN = 300

# Interval (seconds) between lsof polls by the connection sampler
CONNECTION_SAMPLE_INTERVAL = 3


@dataclass
class AppConfig:
    configs_dir: Path = DEFAULT_CONFIGS_DIR
    direct_file: Path = DEFAULT_DIRECT_FILE
    block_file: Path = DEFAULT_BLOCK_FILE
    stats_file: Path = DEFAULT_STATS_FILE
    log_file: Path = DEFAULT_LOG_FILE
    connections_file: Path = DEFAULT_CONNECTIONS_FILE
    internal_dns: list[str] = field(default_factory=lambda: list(DEFAULT_INTERNAL_DNS))
    external_dns: list[str] = field(default_factory=lambda: list(DEFAULT_EXTERNAL_DNS))


@dataclass
class GroupCredentials:
    password: str
    username: str | None = None
    secret: str | None = None


@dataclass
class VPNServer:
    config_path: Path
    name: str
    remote: str
    port: int
    proto: str
    group: str
    protocol: str = PROTO_OPENVPN  # openvpn | sstp — dictates the client binary
    vless_id: str | None = None
    vless_host: str | None = None
    vless_sni: str | None = None
    vless_path: str | None = None
    vless_tls: bool = False
    vless_allow_insecure: bool = False
    vless_network: str = "tcp"
    latency_ms: float | None = None

    def __str__(self):
        lat = f"{self.latency_ms:.0f}ms" if self.latency_ms is not None else "?"
        return f"{self.group}/{self.name} ({self.remote}:{self.port}) [{lat}]"


@dataclass
class Group:
    name: str
    directory: Path
    credentials: GroupCredentials | None
    servers: list[VPNServer] = field(default_factory=list)


_APP_CONFIG: AppConfig | None = None


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _to_path(raw: object, default: Path) -> Path:
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return default


def _to_dns(raw: object, default: list[str]) -> list[str]:
    if not isinstance(raw, list):
        return list(default)
    cleaned = [str(x).strip() for x in raw if str(x).strip()]
    return cleaned or list(default)


def load_app_config(config_file: Path | None = None, force: bool = False) -> AppConfig:
    global _APP_CONFIG

    if _APP_CONFIG is not None and not force:
        return _APP_CONFIG

    cfg_path = (config_file or APP_CONFIG_FILE).expanduser()
    data: dict = {}
    if cfg_path.is_file() and tomllib is not None:
        try:
            data = tomllib.loads(cfg_path.read_text())
        except OSError:
            data = {}
        except Exception:
            data = {}

    cfg = AppConfig(
        configs_dir=_to_path(data.get("configs_dir"), DEFAULT_CONFIGS_DIR),
        direct_file=_to_path(data.get("direct_file"), DEFAULT_DIRECT_FILE),
        block_file=_to_path(data.get("block_file"), DEFAULT_BLOCK_FILE),
        stats_file=_to_path(data.get("stats_file"), DEFAULT_STATS_FILE),
        log_file=_to_path(data.get("log_file"), DEFAULT_LOG_FILE),
        connections_file=_to_path(data.get("connections_file"), DEFAULT_CONNECTIONS_FILE),
        internal_dns=_to_dns(data.get("internal_dns"), DEFAULT_INTERNAL_DNS),
        external_dns=_to_dns(data.get("external_dns"), DEFAULT_EXTERNAL_DNS),
    )

    _ensure_dir(APP_HOME)
    _ensure_dir(cfg.configs_dir)
    _APP_CONFIG = cfg
    return cfg


def get_app_config() -> AppConfig:
    return load_app_config()


CONFIGS_DIR = get_app_config().configs_dir
STATS_FILE = get_app_config().stats_file
LOG_FILE = get_app_config().log_file
CONNECTIONS_FILE = get_app_config().connections_file
DIRECT_FILE = get_app_config().direct_file
BLOCK_FILE = get_app_config().block_file
INTERNAL_DNS = get_app_config().internal_dns
EXTERNAL_DNS = get_app_config().external_dns


def parse_ovpn(path: Path, group: str) -> VPNServer | None:
    """Parse an .ovpn file and extract remote, port, proto."""
    try:
        text = path.read_text()
    except OSError:
        return None

    remote_match = re.search(r"^remote\s+(\S+)(?:\s+(\d+))?", text, re.MULTILINE)
    port_match = re.search(r"^port\s+(\d+)", text, re.MULTILINE)
    proto_match = re.search(r"^proto\s+(\S+)", text, re.MULTILINE)

    if not remote_match:
        return None

    remote = remote_match.group(1)
    port: int | None = None
    if remote_match.group(2):
        port = int(remote_match.group(2))
    elif port_match:
        port = int(port_match.group(1))
    if port is None:
        return None

    return VPNServer(
        config_path=path,
        name=path.stem,
        remote=remote,
        port=port,
        proto=proto_match.group(1) if proto_match else "tcp",
        group=group,
        protocol=PROTO_OPENVPN,
    )


def parse_sstp(path: Path, group: str) -> VPNServer | None:
    """Parse a .sstp file. Format: `remote <host>` [+ optional `port <n>`]."""
    try:
        text = path.read_text()
    except OSError:
        return None

    remote_match = re.search(r"^remote\s+(\S+)", text, re.MULTILINE)
    if not remote_match:
        return None
    port_match = re.search(r"^port\s+(\d+)", text, re.MULTILINE)
    port = int(port_match.group(1)) if port_match else DEFAULT_SSTP_PORT

    return VPNServer(
        config_path=path,
        name=path.stem,
        remote=remote_match.group(1),
        port=port,
        proto="tcp",
        group=group,
        protocol=PROTO_SSTP,
    )


def parse_vless(path: Path, group: str) -> VPNServer | None:
    """Parse a .vless file containing a single vless:// URI."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None

    uri = unquote(raw)
    try:
        p = urlparse(uri)
    except ValueError:
        return None
    if p.scheme.lower() != "vless":
        return None
    if not p.hostname or not p.username:
        return None
    if p.port is None:
        return None

    q = parse_qs(p.query)
    host = (q.get("host") or [None])[0]
    sni = (q.get("sni") or [None])[0]
    network = ((q.get("type") or ["tcp"])[0] or "tcp").lower()
    path_q = (q.get("path") or ["/"])[0] or "/"
    security = ((q.get("security") or [""])[0] or "").lower()
    allow_insecure = ((q.get("allowInsecure") or q.get("insecure") or ["0"])[0] or "0").lower() in ("1", "true", "yes")

    return VPNServer(
        config_path=path,
        name=path.stem,
        remote=p.hostname,
        port=p.port,
        proto="tcp",
        group=group,
        protocol=PROTO_V2RAY,
        vless_id=p.username,
        vless_host=host,
        vless_sni=sni or p.hostname,
        vless_path=path_q,
        vless_tls=(security == "tls"),
        vless_allow_insecure=allow_insecure,
        vless_network=network,
    )


def _load_credentials(group_dir: Path) -> GroupCredentials | None:
    cred_file = group_dir / CRED_FILENAME
    if not cred_file.is_file():
        return None
    try:
        data = json.loads(cred_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    password = data.get("password")
    if not password:
        return None
    return GroupCredentials(
        password=password,
        username=data.get("username"),
        secret=data.get("secret"),
    )


def load_group(group_dir: Path) -> Group | None:
    """Load a single group directory."""
    if not group_dir.is_dir():
        return None
    servers: list[VPNServer] = []
    for f in sorted(group_dir.glob("*.ovpn")):
        server = parse_ovpn(f, group=group_dir.name)
        if server:
            servers.append(server)
    for f in sorted(group_dir.glob("*.sstp")):
        server = parse_sstp(f, group=group_dir.name)
        if server:
            servers.append(server)
    for f in sorted(group_dir.glob("*.vless")):
        server = parse_vless(f, group=group_dir.name)
        if server:
            servers.append(server)
    if not servers:
        return None
    creds = _load_credentials(group_dir)
    needs_creds = any(s.protocol in (PROTO_OPENVPN, PROTO_SSTP) for s in servers)
    if needs_creds and creds is None:
        return None
    return Group(name=group_dir.name, directory=group_dir, credentials=creds, servers=servers)


def load_groups(directory: Path | None = None) -> dict[str, Group]:
    """Discover all group subdirectories under the configs dir."""
    root = directory or get_app_config().configs_dir
    if not root.is_dir():
        return {}
    groups: dict[str, Group] = {}
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        group = load_group(sub)
        if group:
            groups[group.name] = group
    return groups
