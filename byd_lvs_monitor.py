#!/usr/bin/env python3
"""
BYD Battery-Box LVS/HVS/HVM — Cell-Level Monitor
==================================================
Reads per-module cell voltages, temperatures, SOC, balancing status
via Modbus RTU over TCP from BYD Battery-Box BMU.

Protocol based on redpomodoro/byd_battery_box Home Assistant integration.
See: https://github.com/redpomodoro/byd_battery_box

Usage:
  python3 byd_lvs_monitor.py                          # auto-detect on default IP
  python3 byd_lvs_monitor.py --host 192.168.1.155     # custom IP
  python3 byd_lvs_monitor.py --modules 4              # override module count

License: MIT
"""

import argparse
import sys
import time
from datetime import datetime

try:
    from pymodbus.client import ModbusTcpClient
    # pymodbus >= 3.7 moved framers
    try:
        from pymodbus.framer.rtu_framer import ModbusRtuFramer
    except ImportError:
        try:
            from pymodbus.framer import FramerType
            ModbusRtuFramer = FramerType.RTU
        except ImportError:
            from pymodbus.transaction import ModbusRtuFramer
except ImportError:
    print("ERROR: pymodbus not found. Install with: pip install pymodbus")
    sys.exit(1)

# ── Protocol Constants ────────────────────────────────────
DEFAULT_HOST = "192.168.16.254"
DEFAULT_PORT = 8080
SLAVE_ID = 1
CELLS_PER_MODULE = 16       # LFP cells per module
TEMPS_PER_MODULE = 8        # NTC sensors per module
POLL_TIMEOUT = 10           # seconds to wait for 0x8801
POLL_INTERVAL = 0.25        # seconds between polls

# ── Warranty & Energy Constants ───────────────────────────
MODULE_USABLE_KWH = 3.6    # usable capacity per cycle (4.0 kWh × 0.9, min SOC ~10%)
# BYD LVS warranty: Minimum Throughput Energy (discharge) per tower model
# Source: BYD Battery-Box Premium LVS Limited Warranty (Europe, V1.1)
WARRANTY_MWH = {
    1: 11.88,   # LVS 4.0
    2: 23.76,   # LVS 8.0
    3: 35.64,   # LVS 12.0
    4: 47.53,   # LVS 16.0
    5: 59.41,   # LVS 20.0
    6: 71.29,   # LVS 24.0
}

# Modbus register addresses
REG_SUMMARY      = 0x0500   # System summary (FC3, 25 regs)
REG_CONFIG        = 0x0000   # Configuration (FC3, 0x66 regs)
REG_BMS_CMD       = 0x0550   # BMS command write (FC16)
REG_BMS_STATUS    = 0x0551   # BMS ready poll (FC3)
REG_BMS_DATA      = 0x0558   # BMS data FIFO read (FC3)
CMD_BMS_READ      = 0x8100   # "request BMS status" command
RESP_BMS_READY    = 0x8801   # "data ready" response
BMS_DATA_CHUNKS   = 4        # 4 × 65 registers = 260 total
BMS_CHUNK_SIZE    = 65       # max registers per read
# ──────────────────────────────────────────────────────────


def signed16(val):
    """Convert unsigned 16-bit to signed."""
    return val if val < 32768 else val - 65536


def connect(host, port):
    """Connect to BMU via Modbus RTU over TCP."""
    client = ModbusTcpClient(host=host, port=port, framer=ModbusRtuFramer)
    if not client.connect():
        print(f"ERROR: Cannot connect to {host}:{port}", file=sys.stderr)
        print(f"  - Check that BMU is powered on and LAN cable connected", file=sys.stderr)
        print(f"  - Check that no other application (BE Connect) is connected", file=sys.stderr)
        sys.exit(1)
    return client


def read_bmu_serial(client):
    """Read BMU serial number from config registers 0x0000 (10 regs = 20 bytes ASCII)."""
    result = client.read_holding_registers(0x0000, 10, slave=SLAVE_ID)
    if result.isError():
        return None
    serial = ""
    for v in result.registers:
        ch1 = (v >> 8) & 0xFF
        ch2 = v & 0xFF
        if 32 <= ch1 < 127: serial += chr(ch1)
        if 32 <= ch2 < 127: serial += chr(ch2)
    return serial.rstrip('x \x00') or None


def detect_modules(client):
    """Auto-detect number of BMS modules from config register 0x0010."""
    result = client.read_holding_registers(0x0010, 1, slave=SLAVE_ID)
    if result.isError():
        return None
    module_count = result.registers[0] & 0x0F
    return module_count if module_count > 0 else None


def read_summary(client):
    """Read system summary from 0x0500."""
    result = client.read_holding_registers(REG_SUMMARY, 25, slave=SLAVE_ID)
    if result.isError():
        return None

    r = result.registers
    # Lifetime energy: UINT32 little-endian word order × 0.001 = kWh
    charge_kwh = (r[18] * 65536 + r[17]) * 0.001 if len(r) > 18 else 0
    discharge_kwh = (r[20] * 65536 + r[19]) * 0.001 if len(r) > 20 else 0
    return {
        'soc': r[0],
        'max_cell_v': r[1] / 100,
        'min_cell_v': r[2] / 100,
        'soh': r[3],
        'current': -(signed16(r[4]) / 10),
        'pack_voltage': r[5] / 100,
        'max_temp': r[6],
        'min_temp': r[7],
        'pack_voltage_2': r[16] / 100 if len(r) > 16 else 0,
        'charge_energy_kwh': charge_kwh,
        'discharge_energy_kwh': discharge_kwh,
    }


def query_module(client, bms_id):
    """
    Query single BMS module for cell-level data.

    Protocol:
      1. Write [bms_id, 0x8100] to register 0x0550 (FC16)
      2. Poll register 0x0551 until value == 0x8801 (data ready)
      3. Read 65 registers from 0x0558 (FC3) × 4 chunks

    Returns dict with decoded values or None on failure.
    """
    # Step 1: Write command
    wr = client.write_registers(REG_BMS_CMD, [bms_id, CMD_BMS_READ], slave=SLAVE_ID)
    if wr.isError():
        return None

    # Step 2: Wait for ready response
    elapsed = 0
    while elapsed < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        result = client.read_holding_registers(REG_BMS_STATUS, 1, slave=SLAVE_ID)
        if not result.isError() and result.registers[0] == RESP_BMS_READY:
            break
    else:
        return None

    # Step 3: Read 4 chunks of 65 registers from FIFO buffer
    r = []
    for chunk in range(BMS_DATA_CHUNKS):
        time.sleep(0.1)
        result = client.read_holding_registers(REG_BMS_DATA, BMS_CHUNK_SIZE, slave=SLAVE_ID)
        if result.isError():
            r.extend([0] * BMS_CHUNK_SIZE)
        else:
            r.extend(result.registers)

    if len(r) < BMS_DATA_CHUNKS * BMS_CHUNK_SIZE:
        return None

    # ── Decode from 260 registers ──
    data = {}
    data['bms_id'] = bms_id
    data['payload_size'] = r[0]
    data['max_cell_v'] = signed16(r[1])             # mV
    data['min_cell_v'] = signed16(r[2])             # mV
    data['max_v_cell'] = (r[3] >> 8) & 0xFF
    data['max_v_module'] = r[3] & 0xFF
    data['max_temp'] = signed16(r[4])               # °C
    data['min_temp'] = signed16(r[5])               # °C
    data['max_t_cell'] = (r[6] >> 8) & 0xFF
    data['max_t_module'] = r[6] & 0xFF

    # Balancing flags — reg[7] is 16-bit mask, 1 bit per cell
    bal_flags = r[7]
    data['balancing_raw'] = bal_flags
    data['balancing'] = [(bal_flags >> bit) & 1 for bit in range(CELLS_PER_MODULE)]
    data['balancing_active'] = sum(data['balancing'])

    # Lifetime energy (UINT32 little-endian word order)
    data['charge_energy_kwh'] = (r[16] * 65536 + r[15]) * 0.001
    data['discharge_energy_kwh'] = (r[18] * 65536 + r[17]) * 0.001

    data['bat_voltage'] = signed16(r[21]) * 0.1     # V
    data['output_voltage'] = signed16(r[24]) * 0.1  # V
    data['soc'] = signed16(r[25]) * 0.1              # %
    data['soh'] = signed16(r[26])                    # %
    data['current'] = -(signed16(r[27]) * 0.1)          # A (inverted: positive = charging)

    # Warnings & errors
    data['warnings1'] = r[28] if len(r) > 28 else 0
    data['warnings2'] = r[29] if len(r) > 29 else 0
    data['errors'] = r[48] if len(r) > 48 else 0

    # Module serial number at positions 34-45 (12 regs = 24 bytes ASCII)
    serial = ""
    for i in range(34, 46):
        if i < len(r):
            ch1 = (r[i] >> 8) & 0xFF
            ch2 = r[i] & 0xFF
            if 32 <= ch1 < 127: serial += chr(ch1)
            if 32 <= ch2 < 127: serial += chr(ch2)
    data['serial'] = serial.rstrip('x \x00') or None

    # 16 cell voltages (mV) at positions 49-64
    data['cell_voltages'] = [signed16(r[49 + i]) for i in range(CELLS_PER_MODULE)]

    # 8 temperatures (°C) at positions 180-183 (chunk 2)
    # Packed as INT8 pairs: hi_byte=T_odd, lo_byte=T_even
    data['cell_temps'] = []
    for i in range(4):
        reg_pos = 180 + i
        if reg_pos < len(r):
            val = r[reg_pos]
            t_hi = (val >> 8) & 0xFF
            t_lo = val & 0xFF
            if t_hi > 127: t_hi -= 256
            if t_lo > 127: t_lo -= 256
            data['cell_temps'].extend([t_hi, t_lo])
    # Strip trailing zeros (some modules report fewer sensors)
    while data['cell_temps'] and data['cell_temps'][-1] == 0:
        data['cell_temps'].pop()

    data['power'] = round(data['current'] * data['output_voltage'], 1)

    return data


# ── Display Functions ─────────────────────────────────────

def color(text, code):
    """Wrap text in ANSI color."""
    return f"\033[{code}m{text}\033[0m"


def rpad(visible_text, ansi_text, width):
    """Right-pad line accounting for invisible ANSI codes."""
    pad = max(0, width - len(visible_text))
    return ansi_text + " " * pad


def print_header():
    """Print report header."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "Battery Monitor — BYD LVS Premium"
    TW = 122  # total width between leading spaces (matches box ┌─┐ width)
    # Title indented +3 from left edge, datetime ends 3 before right edge
    inner = f"   {title}{now:>{TW - len(title) - 6}}   "
    print(f"\n  {'═' * TW}")
    print(f"  {inner}")
    print(f"  {'═' * TW}")


def print_summary(summary, host=None, port=None, bmu_serial=None):
    """Print system summary box."""
    if not summary:
        print("  [Summary unavailable]")
        return
    s = summary
    SW = 120  # inner width (┌ + ─×120 + ┐ = 122 total, matches header ═×122)
    print(f"\n  ┌{'─' * SW}┐")

    # Line 0: Connection info
    conn = f"  Connected: {host}:{port}"
    sn_str = f"  BMU SN: {bmu_serial}" if bmu_serial else ""
    modules = f"  Modules: {s.get('_num_modules', '?')}"
    vis_l0 = f"{conn}{modules}{sn_str}"
    ansi_l0 = f"  {color('Connected:', '1;32')} {host}:{port}{modules}{sn_str}"
    pad0 = max(0, SW - len(vis_l0))
    print(f"  │{ansi_l0}{' ' * pad0}│")

    # Line 1: Electrical
    pwr = s['current'] * s['pack_voltage']
    l1 = (f"  SoC: {s['soc']:3d}%  SoH: {s['soh']:3d}%"
          f"  Voltage: {s['pack_voltage']:6.2f}V"
          f"  Current: {s['current']:+.1f}A"
          f"  Power: {pwr:+.0f}W")
    print(f"  │{l1:<{SW}}│")

    # Line 2: Cells, temp, energy
    ch = s['charge_energy_kwh']
    dch = s['discharge_energy_kwh']
    eff = (dch / ch * 100) if ch > 0 else 0
    l2 = (f"  Cells: {s['min_cell_v']:.2f} - {s['max_cell_v']:.2f}V"
          f"  Temp: {s['min_temp']:2d} - {s['max_temp']:2d}°C"
          f"  Energy in: {ch:.0f} kWh  Energy out: {dch:.0f} kWh"
          f"  Round trip efficiency: {eff:.1f}%")
    print(f"  │{l2:<{SW}}│")
    print(f"  └{'─' * SW}┘")


def print_tower_table(tower_data, tower_num, mods_per_tower, towers):
    """Print cell table for a single tower (or all modules if towers=1)."""

    # Warranty calculation
    warranty_kwh = WARRANTY_MWH.get(mods_per_tower, 0) * 1000
    mod_warranty_kwh = warranty_kwh / mods_per_tower if warranty_kwh > 0 else 0

    # Fixed inner width (border = IW+2, matching summary box)
    IW = 118
    CW = 6   # cell column width

    def line(text_vis, text_ansi=""):
        if not text_ansi:
            text_ansi = text_vis
        print(f"  │ {rpad(text_vis, text_ansi, IW)} │")

    def sep():
        print(f"  ├{'─' * (IW + 2)}┤")

    # Header
    title = f"Tower {tower_num}" if towers > 1 else ""
    print(f"\n  ┌{'─' * (IW + 2)}┐")
    hdr = "    "
    for i in range(1, CELLS_PER_MODULE + 1):
        hdr += f"{'C' + str(i):>{CW}s}"
    hdr += "    Avg    Drift"
    line(hdr)
    if title:
        sep()
        line(title, color(f'Tower {tower_num}', '1;37'))
    sep()

    bms_ids = sorted(tower_data.keys())
    for idx, bms_id in enumerate(bms_ids):
        d = tower_data[bms_id]

        cv = d['cell_voltages']
        cv_min, cv_max = min(cv), max(cv)
        cv_avg = sum(cv) / len(cv)
        cv_spread = cv_max - cv_min

        ct = d.get('cell_temps', [])
        ct_valid = [t for t in ct if t > 0]
        ct_min = min(ct_valid) if ct_valid else 0
        ct_max = max(ct_valid) if ct_valid else 0

        # Module info — line 1: identity, state, cycles, SoH, warranty
        mod = (bms_id - 1) % mods_per_tower + 1
        sn = d.get('serial') or ''
        sn_str = f"  SN:{sn}" if sn else ""

        bal_count = d.get('balancing_active', 0)
        if d['errors']:
            state = f"ERR:0x{d['errors']:04X}"
        elif d['warnings1'] or d['warnings2']:
            state = f"W:0x{d['warnings1']:04X}"
        else:
            state = "OK"
        bal_tag = f"Balancing: {bal_count}" if bal_count > 0 else "Balancing: OFF"
        cycles = d['discharge_energy_kwh'] / MODULE_USABLE_KWH
        ch = d['charge_energy_kwh']
        dch = d['discharge_energy_kwh']
        eff = (dch / ch * 100) if ch > 0 else 0
        warr_pct = 100 - (dch / mod_warranty_kwh * 100) if mod_warranty_kwh > 0 else 0

        l1 = (f"Module {mod}{sn_str}"
              f"  State: {state}  {bal_tag}  Cycles: ~{cycles:.0f}"
              f"  SoH={d['soh']}%  Warranty rem: {warr_pct:.1f}%")
        vis1 = f"BMS{bms_id}  {l1}"
        ansi_state = color(state, '1;31') if state != "OK" else color(state, '1;32')
        ansi_bal = color(f'Balancing: {bal_count}', '1;33') if bal_count > 0 else f"Balancing: {color('OFF', '1;32')}"
        ansi1 = (f"{color(f'BMS{bms_id}', '1;37')}  Module {mod}{sn_str}"
                 f"  State: {ansi_state}  {ansi_bal}  Cycles: ~{cycles:.0f}"
                 f"  SoH={d['soh']}%  Warranty rem: {warr_pct:.1f}%")
        line(vis1, ansi1)

        # Module info — line 2: electrical + energy
        l2 = (f"SoC: {d['soc']:.1f}%  Volt: {d['bat_voltage']:.1f}V"
              f"  Curr: {d['current']:+.1f}A  Pwr: {d['power']:+.0f}W"
              f"  Temp: {d['min_temp']}-{d['max_temp']}°C"
              f"  kWh-in: {ch:.1f}  kWh-out: {dch:.1f}  η: {eff:.0f}%")
        line(f"      {l2}")

        # Dashed separator between info and data (gray, not connected to borders)
        dashes = "-" * (IW - 1)
        line(dashes, color(dashes, '90'))

        # Voltage row — outlier detection against second nearest value
        bal = d.get('balancing', [])
        unique_v = sorted(set(cv))
        v_min_outlier = (unique_v[1] - unique_v[0] >= 5) if len(unique_v) > 1 else False
        v_max_outlier = (unique_v[-1] - unique_v[-2] >= 5) if len(unique_v) > 1 else False
        vis_parts = " mV  "
        ansi_parts = " mV  "
        for ci, v in enumerate(cv):
            cell_str = f"{v:{CW}d}"
            is_bal = ci < len(bal) and bal[ci]
            if is_bal:
                ansi_parts += color(f"{v:{CW}d}", '1;33')  # orange = balancing
            elif v == cv_min and v_min_outlier:
                ansi_parts += color(f"{v:{CW}d}", '1;36')  # cyan = lowest outlier
            elif v == cv_max and v_max_outlier:
                ansi_parts += color(f"{v:{CW}d}", '1;31')  # red = highest outlier
            else:
                ansi_parts += cell_str
            vis_parts += cell_str
        drift_str = f"{cv_spread} mV"
        stats = f"  {cv_avg:5.0f}   {drift_str:^7s}"
        line(vis_parts + stats, ansi_parts + stats)

        # Temperature row — outlier detection against second nearest value
        unique_t = sorted(set(ct_valid)) if ct_valid else []
        t_min_outlier = (unique_t[1] - unique_t[0] >= 2) if len(unique_t) > 1 else False
        t_max_outlier = (unique_t[-1] - unique_t[-2] >= 2) if len(unique_t) > 1 else False
        vis_parts = " Tmp"
        ansi_parts = " Tmp"
        for i in range(CELLS_PER_MODULE):
            if i < len(ct) and ct[i] > 0:
                t = ct[i]
                cell_str = f"{t:{CW}d}"
                if t == ct_max and t_max_outlier:
                    ansi_parts += color(f"{t:{CW}d}", '1;31')  # red = highest outlier
                elif t == ct_min and t_min_outlier:
                    ansi_parts += color(f"{t:{CW}d}", '1;34')  # light blue = lowest outlier
                else:
                    ansi_parts += cell_str
                vis_parts += cell_str
            else:
                vis_parts += " " * CW
                ansi_parts += " " * CW
        if ct_valid:
            ct_avg_val = sum(ct_valid) / len(ct_valid)
            stats = f"  {ct_avg_val:5.0f}  {ct_max - ct_min:4d}°C"
        else:
            stats = ""
        line(vis_parts + stats, ansi_parts + stats)

        if idx < len(bms_ids) - 1:
            sep()

    print(f"  └{'─' * (IW + 2)}┘")




def parse_args():
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="BYD Battery-Box LVS/HVS/HVM cell-level monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s                              # auto-detect on default IP (192.168.16.254)
  %(prog)s --host 192.168.1.155         # custom BMU IP address
  %(prog)s --modules 4                  # single tower (4 modules)
  %(prog)s --towers 2 --yes             # 2 towers, skip disclaimer prompt
""",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"BMU IP address (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"BMU TCP port (default: {DEFAULT_PORT})")
    p.add_argument("--modules", type=int, default=0,
                   help="number of BMS modules, 0=auto-detect (default: 0)")
    p.add_argument("--towers", type=int, default=1,
                   help="number of towers for display grouping (default: 1)")
    p.add_argument("--yes", "-y", action="store_true",
                   help="accept disclaimer without prompting")
    return p.parse_args()


DISCLAIMER = """
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                                                                              │
  │  DISCLAIMER                                                                  │
  │                                                                              │
  │  This software is NOT an official BYD diagnostic tool.                       │
  │  It is provided "AS IS" without warranty of any kind.                        │
  │                                                                              │
  │  By using this software, you acknowledge and agree that:                     │
  │  - The author assumes NO liability for any damages whatsoever                │
  │  - You waive all claims for compensation arising from its use                │
  │  - You accept full responsibility for any decisions made based               │
  │    on information provided by this software                                  │
  │  - Incorrect readings may occur due to communication errors                  │
  │    or firmware differences                                                   │
  │                                                                              │
  │  BYD and Battery-Box are registered trademarks of BYD Company Limited.       │
  │                                                                              │
  └──────────────────────────────────────────────────────────────────────────────┘"""


def main():
    args = parse_args()

    print(DISCLAIMER)
    if not args.yes:
        try:
            answer = input("\n  Do you accept these terms? (yes/no): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(0)
        if answer != 'yes':
            print("  You must accept the terms to use this software.")
            sys.exit(0)
    print()

    client = connect(args.host, args.port)

    try:
        # Auto-detect or use specified module count
        num_modules = args.modules
        if num_modules <= 0:
            detected = detect_modules(client)
            if detected:
                num_modules = detected
            else:
                print("WARNING: Cannot auto-detect module count, using 8",
                      file=sys.stderr)
                num_modules = 8

        # Read BMU serial number
        bmu_serial = read_bmu_serial(client)
        print_header()

        # System summary
        summary = read_summary(client)
        if summary:
            summary['towers'] = args.towers
            summary['bmu_serial'] = bmu_serial
            summary['_num_modules'] = num_modules
        print_summary(summary, args.host, args.port, bmu_serial)

        # Query and print per tower
        towers = args.towers
        mods_per_tower = num_modules // towers if towers > 1 else num_modules

        for t in range(towers):
            tower_data = {}
            start_bms = t * mods_per_tower + 1
            end_bms = start_bms + mods_per_tower
            failed = []
            print()
            for i, bms_id in enumerate(range(start_bms, end_bms), 1):
                print(f"  Reading BMS{bms_id} ({i} of {mods_per_tower})...", end="\r")
                data = query_module(client, bms_id)
                if data:
                    tower_data[bms_id] = data
                else:
                    failed.append(bms_id)
            if failed:
                print(f"  Reading BMS ... ✗ failed: {failed}")
            else:
                print(f"  Reading BMS{end_bms - 1} ({mods_per_tower} of {mods_per_tower})... {color('✓', '1;32')}")
            print_tower_table(tower_data, t + 1, mods_per_tower, towers)

    finally:
        client.close()
        print(f"\n  Connection closed.\n")


if __name__ == "__main__":
    main()
