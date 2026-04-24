# bifrost
Standalone VPN CLI for OpenVPN/SSTP with configurable DNS and split routing.

## Features
- OpenVPN and SSTP support in one CLI.
- Automatic server probing and best-server selection.
- Auto-reconnect loop with reliability-aware ranking.
- Group-based config loading (`cred.json` + `*.ovpn`/`*.sstp`).
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
- Installs required system tools (`openvpn`, `sstp-client`) and Python dependencies.
- Uses mirror `https://mirror-pypi.runflare.com/simple` by default.
- Override mirror: `PIP_INDEX_URL=<url> ./bootstrap.sh`
- Skips already-installed tools automatically.
- Reinstall package: `./bootstrap.sh --force`

## Groups and config layout
A **group** is a provider/account/profile folder under `~/.bifrost/configs`.

Each group contains:
- one `cred.json` (shared credentials for that group)
- one or more VPN config files

You can put **multiple configs in one group** (for example several servers/regions).
Bifrost will probe/select among them when you run that group.

Protocol mapping:
- `*.ovpn` files are treated as **OpenVPN** configs.
- `*.sstp` files are treated as **SSTP** configs.

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
OpenVPN/SSTP group commands:
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
