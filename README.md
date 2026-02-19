# BYD Battery-Box Cell Monitor

Cell-level monitoring for BYD Battery-Box Premium LVS (and HVS/HVM) systems via Modbus RTU over TCP.

Reads individual cell voltages, temperatures, SOC/SOH, balancing status and error flags from all BMS modules — the same data visible in the BYD BE Connect Plus service application.

![Terminal output](images/screenshot.png)

## Features

- **128 cell voltages** (8 modules × 16 LFP cells) with color-coded min/max highlighting
- **64 temperature sensors** (8 per module) with hot/cold coloring
- **Live balancing detection** — shows which cells are currently being balanced
- **Per-module SOC/SOH/current/power** overview
- **Auto-detection** of module count from BMU
- **JSON output** for integration with scripts, Node-RED, InfluxDB, Grafana
- **No cloud, no account** — direct LAN connection to your battery

## Requirements

- Python 3.8+
- [pymodbus](https://pypi.org/project/pymodbus/) (`pip install pymodbus`)
- BYD Battery-Box connected via Ethernet (LAN cable to BMU)
- No other application connected to the battery (BE Connect, Node-RED, etc.)

## Quick Start

```bash
# Install
git clone https://github.com/oliveres/byd-battery-monitor.git
cd byd-battery-monitor
pip install pymodbus

# Run (default IP 192.168.16.254)
python3 byd_lvs_monitor.py

# Custom IP (e.g. DHCP-assigned)
python3 byd_lvs_monitor.py --host 192.168.1.155

# Single tower only
python3 byd_lvs_monitor.py --host 192.168.1.155 --modules 4
```

## Usage

```
usage: byd_lvs_monitor.py [-h] [--host HOST] [--port PORT] [--modules MODULES] [--json]

BYD Battery-Box LVS/HVS/HVM cell-level monitor

options:
  -h, --help         show this help message and exit
  --host HOST        BMU IP address (default: 192.168.16.254)
  --port PORT        BMU TCP port (default: 8080)
  --modules MODULES  number of BMS modules, 0=auto-detect (default: 0)
  --json             output as JSON instead of table
```

### JSON Output

```bash
# Full JSON dump
python3 byd_lvs_monitor.py --host 192.168.1.155 --json

# Single module with jq
python3 byd_lvs_monitor.py --json | jq '.modules.BMS1.cell_voltages'

# Periodic logging to file
while true; do
  python3 byd_lvs_monitor.py --json >> battery_log.jsonl
  sleep 60
done
```

JSON structure:
```json
{
  "timestamp": "2026-02-19T18:04:57",
  "summary": {
    "soc": 62, "soh": 98, "pack_voltage": 54.0,
    "current": -179.7, "max_temp": 30, "min_temp": 25
  },
  "modules": {
    "BMS1": {
      "soc": 93.2, "soh": 98,
      "bat_voltage": 54.0, "current": -23.1,
      "cell_voltages": [3382, 3381, 3379, ...],
      "cell_temps": [28, 28, 28, 27, 28, 29, 30],
      "balancing": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
      "balancing_active": 0,
      "errors": 0, "warnings1": 0
    }
  }
}
```

## Output Example

```
═══════════════════════════════════════════════════════════════════════════
  BYD LVS Premium — Cell Monitor    2026-02-19 18:04:57    (192.168.1.155:8080, 8 modules)
═══════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────┐
  │  SOC:  62%   SOH:  98%   Pack:  54.00V   Current:  -179.7A       │
  │  Cell V: 3.38V - 3.37V   Temp: 25°C - 30°C                      │
  └────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────┐
  │        C1   C2   C3   C4   C5   C6  ...  C16   Avg   Δ            │
  ├──────────────────────────────────────────────────────────────────────┤
  │ BMS1 T1M1  SOC=93.2%  SOH=98%  54.0V  -23.1A  -1252W  27-30°C    │
  │   mV 3382 3381 3379 3381 3379 3380 ... 3381  3381   4             │
  │   °C   28   28   28   27   28   29 ...    ·    28   3             │
  ├──────────────────────────────────────────────────────────────────────┤
  │ ...                                                                 │
  ├──────────────────────────────────────────────────────────────────────┤
  │ TOTAL  mV: avg=3381  range=3376-3385  Δ=9mV                       │
  │        °C: avg=27  range=25-30  Δ=5°C                              │
  └──────────────────────────────────────────────────────────────────────┘
```

When cells are balancing, an additional row appears:
```
  │ BMS4 T1M4  SOC=98.2%  SOH=98%  54.2V  -2.1A  -114W  24-26°C  ⚡BAL:3  │
  │   mV 3395 3394 3412 3394 3395 3410 ...                                  │
  │   °C   25   25   25   24   25   26 ...                                  │
  │  BAL    ·    ·    ●    ·    ·    ●    ·    ·    ·    ·    ·    ●    ·  3 │
```

## How It Works

The BYD BMU exposes a Modbus RTU interface over TCP (port 8080). Cell-level data
is not available through standard register reads — it requires a three-step protocol:

1. **Write** `[bms_id, 0x8100]` to register `0x0550` (request data from module)
2. **Poll** register `0x0551` until it returns `0x8801` (data ready, ~2 seconds)
3. **Read** 65 registers from `0x0558` four times (FIFO buffer, 260 registers total)

The response contains cell voltages (16 × mV), temperatures (8 × °C), SOC, SOH,
current, balancing flags, warnings and errors.

For full protocol details see [BYD_LVS_Premium_Modbus_Protocol.md](BYD_LVS_Premium_Modbus_Protocol.md).

## Network Setup

The BMU default IP is `192.168.16.254`. If your home network uses a different subnet,
you have two options:

**Option A — Static route** (recommended, no changes to battery):
```bash
# Linux
sudo ip route add 192.168.16.254/32 dev eth0

# macOS
sudo route add 192.168.16.254 -interface en0
```

**Option B — DHCP**: Newer firmware versions support DHCP. Check your router's
DHCP leases for the BMU's assigned address. You can verify connectivity by opening
`http://<BMU_IP>` in a browser — you should see a login page.

## Tested Hardware

| System | Modules | Cells | Status |
|--------|---------|-------|--------|
| BYD LVS Premium 32 kWh (2 towers × 4 modules) | 8 | 128 | ✅ Verified |

The protocol should also work with HVS and HVM systems (same BMU firmware),
but cell count and temperature layout may differ. Contributions welcome!

## Compatibility

- **pymodbus 3.5–3.12+**: Automatic framer import detection
- **Python 3.8+**: No external dependencies beyond pymodbus
- **Tested on**: Raspberry Pi OS (arm64), Ubuntu 24.04, macOS

## Limitations

- Only **one Modbus client** can connect at a time. Close BE Connect Plus before running.
- Full scan of 8 modules takes **~20 seconds**. Not suitable for sub-second monitoring.
- The script performs **read-only** operations. It cannot change battery configuration.
- WiFi connection to BMU times out after a few minutes. Use **LAN cable** for reliable access.

## Related Projects

| Project | Description |
|---------|-------------|
| [redpomodoro/byd_battery_box](https://github.com/redpomodoro/byd_battery_box) | Home Assistant integration (protocol source) |
| [sarnau/BYD-Battery-Box-Infos](https://github.com/sarnau/BYD-Battery-Box-Infos) | Original reverse engineering, event codes |
| [christianh17/ioBroker.bydhvs](https://github.com/christianh17/ioBroker.bydhvs) | ioBroker adapter |

## License

MIT
