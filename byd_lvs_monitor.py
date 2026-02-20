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
  python3 byd_lvs_monitor.py --json                   # JSON output for integrations

License: MIT
"""

import argparse
import json
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
POLL_INTERVAL = 0.5         # seconds between polls

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
        time.sleep(0.2)
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


def print_header(host, port, num_modules, bmu_serial=None):
    """Print report header."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sn = f"    SN: {bmu_serial}" if bmu_serial else ""
    print(f"\n{'═' * 122}")
    print(f"  BYD LVS Premium — Cell Monitor    {now}    ({host}:{port}, {num_modules} modules){sn}")
    print(f"{'═' * 122}")


def print_summary(summary):
    """Print system summary box."""
    if not summary:
        print("  [Summary unavailable]")
        return
    s = summary
    SW = 120
    print(f"\n  ┌{'─' * SW}┐")
    curr_s = f"{s['current']:+.1f}A"
    pwr = s['current'] * s['pack_voltage']
    pwr_s = f"{pwr:+.0f}W"
    l1 = f"  SOC: {s['soc']:3d}%   SOH: {s['soh']:3d}%   Pack: {s['pack_voltage']:6.2f}V   {curr_s:>8s}   {pwr_s:>7s}"
    l2 = f"  Cell V: {s['max_cell_v']:.2f} - {s['min_cell_v']:.2f}V   Temp: {s['min_temp']:2d} - {s['max_temp']:2d}°C"
    # Energy throughput
    ch = s['charge_energy_kwh']
    dch = s['discharge_energy_kwh']
    eff = (dch / ch * 100) if ch > 0 else 0
    l2 += f"   Energy: {ch:.0f}⬆ {dch:.0f}⬇ kWh   η={eff:.1f}%"
    print(f"  │{l1:<{SW}}│")
    print(f"  │{l2:<{SW}}│")
    print(f"  └{'─' * SW}┘")


def print_cell_table(all_data, num_modules, towers=1):
    """Print combined cell voltage + temperature + balancing table."""
    all_v = [v for d in all_data.values() for v in d['cell_voltages']]
    g_v_min = min(all_v) if all_v else 0
    g_v_max = max(all_v) if all_v else 0

    all_t = [t for d in all_data.values() for t in d.get('cell_temps', []) if t > 0]
    g_t_min = min(all_t) if all_t else 0
    g_t_max = max(all_t) if all_t else 0

    # Fixed inner width
    IW = 118

    def line(text_vis, text_ansi=""):
        if not text_ansi:
            text_ansi = text_vis
        print(f"  │ {rpad(text_vis, text_ansi, IW)} │")

    def sep():
        print(f"  ├{'─' * (IW + 2)}┤")

    print(f"\n  ┌{'─' * (IW + 2)}┐")
    hdr = "       C1   C2   C3   C4   C5   C6   C7   C8   C9  C10  C11  C12  C13  C14  C15  C16   Avg   Δ"
    line(hdr)
    sep()

    for bms_id in range(1, num_modules + 1):
        d = all_data.get(bms_id)
        if not d:
            line(f"BMS{bms_id}  — no response —")
            sep()
            continue

        cv = d['cell_voltages']
        cv_min, cv_max = min(cv), max(cv)
        cv_avg = sum(cv) / len(cv)
        cv_spread = cv_max - cv_min

        ct = d.get('cell_temps', [])
        ct_valid = [t for t in ct if t > 0]
        ct_min = min(ct_valid) if ct_valid else 0
        ct_max = max(ct_valid) if ct_valid else 0

        # Module info line
        mods_per_tower = num_modules // towers if towers > 1 else num_modules
        tower = (bms_id - 1) // mods_per_tower + 1
        mod = (bms_id - 1) % mods_per_tower + 1
        sn = d.get('serial') or ''
        sn_str = f"  {sn}" if sn else ""
        info = (f"T{tower}M{mod}{sn_str}"
                f"  SOC={d['soc']:.1f}%  SOH={d['soh']}%"
                f"  {d['bat_voltage']:.1f}V"
                f"  {d['current']:+.1f}A"
                f"  {d['power']:+.0f}W"
                f"  {d['min_temp']}-{d['max_temp']}°C")
        err_str = ""
        if d['errors']:
            err_str += f"  ⚠ ERR:0x{d['errors']:04X}"
        if d['warnings1'] or d['warnings2']:
            err_str += f"  ⚠ W:0x{d['warnings1']:04X}"
        bal_count = d.get('balancing_active', 0)
        bal_str = f"  ⚡BAL:{bal_count}" if bal_count > 0 else ""

        vis = f"BMS{bms_id} {info}{err_str}{bal_str}"
        ansi_bal = f"  {color(f'⚡BAL:{bal_count}', '1;33')}" if bal_count > 0 else ""
        ansi = f"{color(f'BMS{bms_id}', '1;37')} {info}{err_str}{ansi_bal}"
        line(vis, ansi)

        # Voltage row
        vis_parts = "  mV"
        ansi_parts = "  mV"
        for v in cv:
            cell_str = f" {v:4d}"
            if v == g_v_max:
                ansi_parts += f" {color(f'{v:4d}', '1;32')}"
            elif v == g_v_min:
                ansi_parts += f" {color(f'{v:4d}', '1;31')}"
            elif v == cv_max and cv_spread > 2:
                ansi_parts += f" {color(f'{v:4d}', '92')}"
            elif v == cv_min and cv_spread > 2:
                ansi_parts += f" {color(f'{v:4d}', '91')}"
            else:
                ansi_parts += cell_str
            vis_parts += cell_str
        stats = f"  {cv_avg:4.0f} {cv_spread:3d}"
        line(vis_parts + stats, ansi_parts + stats)

        # Temperature row
        vis_parts = "  °C"
        ansi_parts = "  °C"
        for i in range(CELLS_PER_MODULE):
            if i < len(ct) and ct[i] > 0:
                t = ct[i]
                cell_str = f" {t:4d}"
                if t == g_t_max:
                    ansi_parts += f" {color(f'{t:4d}', '1;31')}"
                elif t == g_t_min:
                    ansi_parts += f" {color(f'{t:4d}', '1;34')}"
                elif t == ct_max and ct_max > ct_min:
                    ansi_parts += f" {color(f'{t:4d}', '91')}"
                elif t == ct_min and ct_max > ct_min:
                    ansi_parts += f" {color(f'{t:4d}', '94')}"
                else:
                    ansi_parts += cell_str
                vis_parts += cell_str
            else:
                vis_parts += "    ·"
                ansi_parts += "    ·"
        if ct_valid:
            ct_avg_val = sum(ct_valid) / len(ct_valid)
            stats = f"  {ct_avg_val:4.0f} {ct_max - ct_min:3d}"
        else:
            stats = ""
        line(vis_parts + stats, ansi_parts + stats)

        # Balancing row (only when active)
        bal = d.get('balancing', [])
        bal_count = d.get('balancing_active', 0)
        if bal_count > 0:
            vis_parts = " BAL"
            ansi_parts = " BAL"
            for i in range(CELLS_PER_MODULE):
                if i < len(bal) and bal[i]:
                    vis_parts += "    ●"
                    ansi_parts += f"    {color('●', '1;33')}"
                else:
                    vis_parts += "    ·"
                    ansi_parts += "    ·"
            stats = f"     {bal_count:3d}"
            line(vis_parts + stats, ansi_parts + stats)

        sep()

    # Global summary
    total_bal = sum(d.get('balancing_active', 0) for d in all_data.values())
    if all_v:
        g_avg = sum(all_v) / len(all_v)
        bal_str = f"  BAL: {total_bal} cells" if total_bal > 0 else ""
        txt = f"TOTAL  mV: avg={g_avg:.0f}  range={g_v_min}-{g_v_max}  Δ={g_v_max - g_v_min}mV{bal_str}"
        line(txt)
    if all_t:
        g_t_avg = sum(all_t) / len(all_t)
        txt = f"       °C: avg={g_t_avg:.0f}  range={g_t_min}-{g_t_max}  Δ={g_t_max - g_t_min}°C"
        line(txt)

    print(f"  └{'─' * (IW + 2)}┘")


def print_soc_overview(all_data, num_modules):
    """Print SOC/SOH overview table."""
    OW = 80
    print(f"\n  ┌{'─' * OW}┐")
    hdr = f" {'BMS':>4s} {'SOC%':>6s} {'SOH%':>5s} {'BatV':>6s} {'Vout':>6s} {'Curr':>7s} {'Power':>8s} {'Tmin':>4s} {'Tmax':>4s} {'BAL':>4s} {'Errors':>8s}"
    print(f"  │{hdr:<{OW}}│")
    print(f"  ├{'─' * OW}┤")

    for bms_id in range(1, num_modules + 1):
        d = all_data.get(bms_id)
        if not d:
            print(f"  │{f' BMS{bms_id}  — no data —':<{OW}}│")
            continue

        err = "OK" if d['errors'] == 0 else f"0x{d['errors']:04X}"
        bal_n = d.get('balancing_active', 0)
        bal_s = f"{bal_n}" if bal_n > 0 else "-"
        curr_s = f"{d['current']:+.1f}"
        pwr_s = f"{d['power']:+.0f}W"
        row = (f" BMS{bms_id}"
               f" {d['soc']:6.1f}"
               f" {d['soh']:5d}"
               f" {d['bat_voltage']:6.1f}"
               f" {d['output_voltage']:6.1f}"
               f" {curr_s:>7s}"
               f" {pwr_s:>8s}"
               f" {d['min_temp']:4d}"
               f" {d['max_temp']:4d}"
               f" {bal_s:>4s}"
               f" {err:>8s}")
        print(f"  │{row:<{OW}}│")

    print(f"  └{'─' * OW}┘")


def print_energy_overview(all_data, num_modules, towers=1):
    """Print energy throughput, estimated cycles, efficiency, warranty usage."""
    mods_per_tower = num_modules // towers if towers > 1 else num_modules
    warranty_per_tower_kwh = WARRANTY_MWH.get(mods_per_tower, 0) * 1000  # kWh
    sys_warr_kwh = warranty_per_tower_kwh * towers

    EW = 72
    print(f"\n  ┌{'─' * EW}┐")
    hdr = (f" {'BMS':>4s} {'Ch kWh':>8s} {'Dch kWh':>8s} {'η%':>6s}"
           f" {'Cycles':>7s} {'Warranty%':>9s}")
    print(f"  │{hdr:<{EW}}│")
    print(f"  ├{'─' * EW}┤")

    sys_ch = 0
    sys_dch = 0

    for bms_id in range(1, num_modules + 1):
        d = all_data.get(bms_id)
        if not d:
            print(f"  │{f' BMS{bms_id}  — no data —':<{EW}}│")
            continue

        ch = d['charge_energy_kwh']
        dch = d['discharge_energy_kwh']
        eff = (dch / ch * 100) if ch > 0 else 0
        cycles = dch / MODULE_USABLE_KWH
        sys_ch += ch
        sys_dch += dch

        # Warranty % per module (proportional share)
        mod_warranty_kwh = sys_warr_kwh / num_modules if sys_warr_kwh > 0 else 0
        warr_pct = (dch / mod_warranty_kwh * 100) if mod_warranty_kwh > 0 else 0

        row = (f" BMS{bms_id}"
               f" {ch:8.1f}"
               f" {dch:8.1f}"
               f" {eff:6.1f}"
               f" {cycles:7.0f}"
               f" {warr_pct:8.1f}%")
        print(f"  │{row:<{EW}}│")

    # System total
    print(f"  ├{'─' * EW}┤")
    sys_eff = (sys_dch / sys_ch * 100) if sys_ch > 0 else 0
    sys_cycles = sys_dch / (MODULE_USABLE_KWH * num_modules)
    sys_warr = (sys_dch / sys_warr_kwh * 100) if sys_warr_kwh > 0 else 0

    row = (f" SYS "
           f" {sys_ch:8.1f}"
           f" {sys_dch:8.1f}"
           f" {sys_eff:6.1f}"
           f" {sys_cycles:7.0f}"
           f" {sys_warr:8.1f}%"
           f"  limit: {sys_warr_kwh/1000:.1f} MWh")
    print(f"  │{row:<{EW}}│")
    print(f"  └{'─' * EW}┘")


def output_json(summary, all_data, towers=1):
    """Output all data as JSON for integrations."""
    num_modules = len(all_data)
    mods_per_tower = num_modules // towers if towers > 1 else num_modules
    warranty_per_tower_kwh = WARRANTY_MWH.get(mods_per_tower, 0) * 1000

    out = {
        'timestamp': datetime.now().isoformat(),
        'summary': summary,
        'modules': {},
    }
    for bms_id, d in all_data.items():
        ch = d['charge_energy_kwh']
        dch = d['discharge_energy_kwh']
        eff = (dch / ch * 100) if ch > 0 else 0
        cycles = dch / MODULE_USABLE_KWH
        mod_warranty_kwh = warranty_per_tower_kwh / mods_per_tower if warranty_per_tower_kwh > 0 and mods_per_tower > 0 else 0
        warr_pct = (dch / mod_warranty_kwh * 100) if mod_warranty_kwh > 0 else 0

        out['modules'][f'BMS{bms_id}'] = {
            'bms_id': bms_id,
            'soc': d['soc'],
            'soh': d['soh'],
            'bat_voltage': d['bat_voltage'],
            'output_voltage': d['output_voltage'],
            'current': d['current'],
            'power': d['power'],
            'min_temp': d['min_temp'],
            'max_temp': d['max_temp'],
            'cell_voltages': d['cell_voltages'],
            'cell_temps': d['cell_temps'],
            'balancing': d['balancing'],
            'balancing_active': d['balancing_active'],
            'errors': d['errors'],
            'warnings1': d['warnings1'],
            'warnings2': d['warnings2'],
            'charge_energy_kwh': round(ch, 3),
            'discharge_energy_kwh': round(dch, 3),
            'round_trip_efficiency': round(eff, 1),
            'estimated_cycles': round(cycles, 1),
            'warranty_used_pct': round(warr_pct, 1),
        }
    print(json.dumps(out, indent=2))


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
  %(prog)s --json                       # JSON output for scripts/integrations
  %(prog)s --json | jq '.modules.BMS1'  # extract single module with jq
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
    p.add_argument("--json", action="store_true",
                   help="output as JSON instead of table")
    return p.parse_args()


def main():
    args = parse_args()
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

        if not args.json:
            print_header(args.host, args.port, num_modules, bmu_serial)

        # System summary
        summary = read_summary(client)
        if summary:
            summary['towers'] = args.towers
            summary['bmu_serial'] = bmu_serial
        if not args.json:
            print_summary(summary)

        # Query all modules
        all_data = {}
        for bms_id in range(1, num_modules + 1):
            if not args.json:
                print(f"  Reading BMS{bms_id}...", end="\r")
            data = query_module(client, bms_id)
            if data:
                all_data[bms_id] = data
                if not args.json:
                    print(f"  Reading BMS{bms_id}... ✓", end="\r")
            else:
                if not args.json:
                    print(f"  Reading BMS{bms_id}... ✗ failed", end="\r")

        if args.json:
            output_json(summary, all_data, args.towers)
        else:
            print()
            print_cell_table(all_data, num_modules, args.towers)
            print_soc_overview(all_data, num_modules)
            print_energy_overview(all_data, num_modules, args.towers)

            if all_data:
                total_cells = sum(len(d['cell_voltages']) for d in all_data.values())
                all_v = [v for d in all_data.values() for v in d['cell_voltages']]
                print(f"\n  Total: {total_cells} cells monitored, "
                      f"global spread {max(all_v) - min(all_v)}mV "
                      f"({min(all_v)}-{max(all_v)}mV)")

    finally:
        client.close()
        if not args.json:
            print(f"\n  Connection closed.\n")


if __name__ == "__main__":
    main()
