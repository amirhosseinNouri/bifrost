# bifrost
Standalone VPN CLI for OpenVPN/SSTP/V2Ray (VLESS) with configurable DNS and split routing.

## Features
- OpenVPN, SSTP, and V2Ray (VLESS URI) support in one CLI.
- Automatic server probing and best-server selection.
- Auto-reconnect loop with reliability-aware ranking.
- Group-based config loading (`*.ovpn`/`*.sstp`/`*.vless` + optional `cred.json`).
- Configurable internal/external DNS profiles via `~/.bifrost/config.toml`.
- DNS switching command: `bifrost dns --internal|--external`.
- Direct-route list support (`direct.conf`) for domain/CIDR VPN bypass.
- Blocklist support (`block.conf`) using blackhole routes.
- Per-server traffic/session stats (`bifrost stats`, `--clear`).
- Outgoing connection sampling to help curate blocking rules.
- Full cleanup command to restore DNS/routes and stop VPN clients.

## Requirements
- macOS
- Python 3.10+
- Homebrew

## Clone
```bash
git clone git@github.com:amirhosseinNouri/bifrost.git
cd bifrost
```

## Bootstrap setup (recommended)
From the project root:
```bash
./bootstrap.sh
```

Notes:
- Installs required system tools (`openvpn`, `sstp-client`, `xray`) and Python dependencies.
- Uses mirror `https://mirror-pypi.runflare.com/simple` by default.
- Override mirror: `PIP_INDEX_URL=<url> ./bootstrap.sh`
- Skips already-installed tools automatically.
- Reinstall package: `./bootstrap.sh --force`

## Groups and config layout
A **group** is a provider/account/profile folder under `~/.bifrost/configs`.

Each group contains one or more VPN config files and optionally `cred.json`.

`cred.json` is required for OpenVPN/SSTP entries and optional for VLESS-only groups.

You can put **multiple configs in one group** (for example several servers/regions).
Bifrost will probe/select among them when you run that group.

Protocol mapping:
- `*.ovpn` files are treated as **OpenVPN** configs.
- `*.sstp` files are treated as **SSTP** configs.
- `*.vless` files are treated as **V2Ray VLESS URI** configs (one URI per file).

Example tree:
```text
~/.bifrost/
├── config.toml
├── direct.conf
├── block.conf
├── configs/
│   ├── work/
│   │   ├── cred.json
│   │   ├── us1.ovpn
│   │   ├── us2.ovpn
│   │   └── eu1.sstp
│   └── work2/
│       └── node1.vless
│   └── personal/
│       ├── cred.json
│       ├── fast1.ovpn
│       └── backup1.sstp
├── connections.log
├── stats.json
└── bifrost.log
```

`cred.json` example:
```json
{
  "username": "your-user",
  "password": "your-pass",
  "secret": "optional"
}
```

## Usage
VPN group commands:
```bash
sudo bifrost probe --group work
sudo bifrost run --group work
```

Other commands:
```bash
sudo bifrost stats
sudo bifrost dns --internal
sudo bifrost dns --external
sudo bifrost cleanup
```

If you have only one group, `--group` can be omitted.
`--config-dir` overrides `configs_dir` from `~/.bifrost/config.toml`.

## Direct and block rules
`direct.conf` and `block.conf` are plain text files (one entry per line, `#` comments supported).

`direct.conf` (bypass VPN):
- Domain entry (`example.com`) matches both `example.com` and subdomains.
- CIDR entry (`1.2.3.0/24`) is routed via your pre-VPN gateway.
- Domain entries are applied as split-DNS rules using `internal_dns`.

`block.conf` (drop traffic):
- Domain entry resolves to IPv4 and each address is blackholed.
- IP entry (`1.2.3.4`) is blackholed as a host route.
- CIDR entry (`1.2.3.0/24`) is blackholed as a network route.

Rules are installed while `bifrost run` is active and removed by `bifrost cleanup` (or on clean shutdown).

## Find your direct/block file paths
Bifrost reads these from `~/.bifrost/config.toml`:
- `direct_file`
- `block_file`

If `config.toml` is missing, defaults are used:
- `~/.bifrost/direct.conf`
- `~/.bifrost/block.conf`

Show active paths from CLI:
```bash
sudo bifrost direct
sudo bifrost blocks
```

Create missing files:
```bash
mkdir -p ~/.bifrost
touch ~/.bifrost/direct.conf ~/.bifrost/block.conf
```

Optional override in `~/.bifrost/config.toml`:
```toml
direct_file = "/absolute/path/to/direct.conf"
block_file = "/absolute/path/to/block.conf"
```
