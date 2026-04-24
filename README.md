# bifrost
Standalone VPN CLI extracted from `superopn`.

## Requirements
- macOS
- Python 3.10+
- `openvpn` and/or `sstpc` installed

## Install
`pipx`:
```bash
pipx install /absolute/path/to/bifrost
```

`pip`:
```bash
cd /absolute/path/to/bifrost
python3 -m pip install .
```

## Quick setup
1. Create local config root:
```bash
mkdir -p ~/.bifrost/configs
```
2. Copy sample config and edit DNS/paths only if needed:
```bash
cp /absolute/path/to/bifrost/config.example.toml ~/.bifrost/config.toml
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
