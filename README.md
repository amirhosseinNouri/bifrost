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
- Config/state paths centralized under `~/.bifrost` with overrides.

## Requirements
- macOS
- Python 3.10+
- Homebrew

## Clone
```bash
git clone git@github.com:amirhosseinNouri/bifrost.git
cd bifrost
```

## One-command bootstrap (recommended)
From the project root:
```bash
./bootstrap.sh
```
This installs required system tools (`openvpn`, `sstp-client`) and Python dependencies.

## Install alternatives
`pipx`:
```bash
pipx install /absolute/path/to/bifrost
```

`pip`:
```bash
python3 -m pip install .
```

## Quick setup
1. Create local config root:
```bash
mkdir -p ~/.bifrost/configs
```
2. Copy sample config and edit DNS/paths only if needed:
```bash
cp ./config.example.toml ~/.bifrost/config.toml
```
3. Add your VPN group(s):
```text
~/.bifrost/configs/<group>/cred.json
~/.bifrost/configs/<group>/*.ovpn
~/.bifrost/configs/<group>/*.sstp
```
4. Optional files:
```text
~/.bifrost/direct.conf
~/.bifrost/block.conf
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
```bash
sudo bifrost probe --group <group>
sudo bifrost run --group <group>
sudo bifrost dns --internal
sudo bifrost dns --external
sudo bifrost cleanup
```

`--config-dir` overrides `configs_dir` from `~/.bifrost/config.toml`.
