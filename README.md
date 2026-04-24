# bifrost
Standalone VPN CLI for OpenVPN/SSTP with configurable DNS and split routing.

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
Run directly from GitHub:
```bash
curl -fsSL https://raw.githubusercontent.com/amirhosseinNouri/bifrost/main/bootstrap.sh | bash
```

Or from a local clone:
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
