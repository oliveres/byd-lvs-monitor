# BYD LVS Premium — Modbus Protocol Documentation

> Modbus proprietary protocol for cell-level monitoring of BYD Battery-Box Premium LVS
> via Modbus RTU over TCP. Verified against BE Connect Plus service application.
>
> **Date:** 2026-02-19
> **Hardware tested:** BYD LVS Premium 32 kWh (2 towers × 4 modules)
> **Firmware:** BMU A v1.37, BMU B v1.35, BMS v1.17
> **Inverter:** Victron (CAN bus connection)

---

## 1. Connection

| Parameter | Value |
|-----------|-------|
| Transport | TCP socket |
| Default IP | `192.168.16.254` (or DHCP-assigned, in our case `192.168.1.155`) |
| Port | `8080` |
| Framing | **Modbus RTU** (not Modbus TCP!) |
| Slave ID | `1` |
| Max registers per read | `65` (reads of 25 work too; >65 may fail) |

**Important:** Only one client connection at a time. If BE Connect Plus or Node-RED is
connected, other clients will fail. The BMU WiFi AP (activated by pressing the BMU button)
is used by the mobile BE Connect app and shares the same constraint.

### Python connection example

```python
from pymodbus.client import ModbusTcpClient
from pymodbus.framer.rtu_framer import ModbusRtuFramer

client = ModbusTcpClient(
    host='192.168.1.155',
    port=8080,
    framer=ModbusRtuFramer
)
client.connect()
```

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────┐
│                    BMU                           │
│          (Battery Management Unit)               │
│     IP: 192.168.1.155:8080                       │
│     Modbus RTU over TCP, Slave ID 1              │
├─────────────┬───────────────────────────────────┤
│  Tower 1    │  Tower 2                           │
├─────────────┼───────────────────────────────────┤
│  BMS1 (4kWh)│  BMS5 (4kWh)                      │
│  16 LFP cells, 8 temp sensors                   │
│  BMS2 (4kWh)│  BMS6 (4kWh)                      │
│  BMS3 (4kWh)│  BMS7 (4kWh)                      │
│  BMS4 (4kWh)│  BMS8 (4kWh)                      │
└─────────────┴───────────────────────────────────┘
  Total: 8 modules × 16 cells = 128 LFP cells
  Total: 8 modules × 8 temp sensors = 64 readings
```

Each 4 kWh module has its own BMS board with:
- 16 LiFePO4 cells in series (~3.3V each, ~53V module total)
- 8 NTC temperature sensors distributed across the BMS board
- Independent SOC/SOH tracking
- Cell balancing circuitry

---

## 3. Summary Registers (0x0500) — Read Only

These registers provide a system-wide overview and are always readable without
any write command. Use FC3 (Read Holding Registers).

```
client.read_holding_registers(0x0500, 25, slave=1)
```

| Register | Offset | Type | Scale | Unit | Description |
|----------|--------|------|-------|------|-------------|
| 0x0500 | 0 | UINT16 | 1 | % | System SOC |
| 0x0501 | 1 | UINT16 | ÷100 | V | Max cell voltage |
| 0x0502 | 2 | UINT16 | ÷100 | V | Min cell voltage |
| 0x0503 | 3 | UINT16 | 1 | % | System SOH |
| 0x0504 | 4 | INT16 | ÷10 | A | Current (negative = charging) |
| 0x0505 | 5 | UINT16 | ÷100 | V | Pack voltage |
| 0x0506 | 6 | UINT16 | 1 | °C | Max cell temperature |
| 0x0507 | 7 | UINT16 | 1 | °C | Min cell temperature |
| 0x050F | 15 | UINT16 | 1 | — | Number of towers |
| 0x0510 | 16 | UINT16 | ÷100 | V | Pack voltage (duplicate) |
| 0x0511 | 17 | UINT16 | 1 | — | Charge cycles |

### Decoding example

```python
result = client.read_holding_registers(0x0500, 25, slave=1)
r = result.registers

soc       = r[0]                                          # %
max_v     = r[1] / 100                                    # V
min_v     = r[2] / 100                                    # V
soh       = r[3]                                          # %
current   = (r[4] if r[4] < 32768 else r[4] - 65536) / 10  # A
voltage   = r[5] / 100                                    # V
max_temp  = r[6]                                          # °C
min_temp  = r[7]                                          # °C
```

---

## 4. Cell-Level Data Protocol (0x0550 → 0x0558)

This is the core protocol for reading per-module cell voltages, temperatures,
SOC, balancing status, and more. It uses a write-then-poll-then-read sequence.

### 4.1 Protocol Sequence

```
Step 1: WRITE (FC16) to 0x0550
        Payload: [bms_id, 0x8100]
        bms_id = 1-8 (module number)
        0x8100 = "request BMS status data" command

Step 2: POLL (FC3) register 0x0551
        Wait until value == 0x8801 ("data ready")
        Typical wait: 1.5-2.0 seconds
        Timeout: 10 seconds recommended

Step 3: READ (FC3) from 0x0558, count=65
        Returns FIFO buffer with module data
        Read 4 times for full 260-register dataset
        (chunk 0 = main data, chunk 2 = temperatures)
```

### 4.2 BMS ID Mapping

| bms_id | BE Connect | Physical Position |
|--------|------------|-------------------|
| 1 | BMS1 | Tower 1, Module 1 (top) |
| 2 | BMS2 | Tower 1, Module 2 |
| 3 | BMS3 | Tower 1, Module 3 |
| 4 | BMS4 | Tower 1, Module 4 (bottom) |
| 5 | BMS5 | Tower 2, Module 1 (top) |
| 6 | BMS6 | Tower 2, Module 2 |
| 7 | BMS7 | Tower 2, Module 3 |
| 8 | BMS8 | Tower 2, Module 4 (bottom) |

bms_id 9+ returns no response.

### 4.3 Response Register Map (Chunk 0, 65 registers from 0x0558)

| Index | Type | Scale | Description |
|-------|------|-------|-------------|
| 0 | UINT16 | — | Payload size (128 = 0x80 for LVS) |
| 1 | INT16 | 1 | Max cell voltage (mV) |
| 2 | INT16 | 1 | Min cell voltage (mV) |
| 3 | packed | hi/lo | Max voltage: hi=cell#, lo=module# |
| 4 | INT16 | 1 | Max temperature (°C) |
| 5 | INT16 | 1 | Min temperature (°C) |
| 6 | packed | hi/lo | Max temp: hi=sensor#, lo=module# |
| 7 | UINT16 | bitmask | Cell balancing flags for this module (1 bit/cell, see §4.5) |
| 8–14 | UINT16 | bitmask | Reserved (HVS/HVM use for multi-module balancing) |
| 15–16 | UINT32 | ×0.001 | Charge lifetime energy (kWh, LE word order) |
| 17–18 | UINT32 | ×0.001 | Discharge lifetime energy (kWh, LE word order) |
| 19–20 | — | — | Unknown |
| 21 | INT16 | ×0.1 | Battery voltage (V) |
| 22 | — | — | Unknown (seen: 0x4201) |
| 23 | — | — | Unknown (seen: 0x110B) |
| 24 | INT16 | ×0.1 | Output voltage (V) |
| 25 | INT16 | ×0.1 | SOC (%) |
| 26 | INT16 | 1 | SOH (%) |
| 27 | INT16 | ×0.1 | Current (A, negative = charging) |
| 28 | UINT16 | bitmask | Warnings group 1 |
| 29 | UINT16 | bitmask | Warnings group 2 |
| 30 | UINT16 | bitmask | Warnings group 3 |
| 31–33 | — | — | Static config data |
| **34–45** | **ASCII** | **packed** | **Module serial number (12 regs = 24 bytes, hi/lo byte = 2 chars)** |
| 46–47 | — | — | Reserved |
| 48 | UINT16 | bitmask | Error flags |
| **49–64** | **INT16** | **1** | **16 cell voltages (mV)** |

### 4.4 Temperature Data (Chunk 2, offset 50–53)

The FIFO buffer returns 4 chunks of 65 registers. Temperature data is located
in chunk 2 at positions 50-53 (absolute positions 180-183 in the 260-register array).

| Absolute Index | Chunk 2 Offset | Encoding | Values |
|----------------|----------------|----------|--------|
| 180 | 50 | packed INT8 | T1 (hi byte), T2 (lo byte) |
| 181 | 51 | packed INT8 | T3 (hi byte), T4 (lo byte) |
| 182 | 52 | packed INT8 | T5 (hi byte), T6 (lo byte) |
| 183 | 53 | packed INT8 | T7 (hi byte), T8 (lo byte) |
| 184 | 54 | packed INT8 | PCB/board temperatures (not cell) |
| 185 | 55 | packed INT8 | Additional board temp / zero |

Each register packs two INT8 temperature values (°C, whole degrees, no decimal):
```python
t_hi = (register >> 8) & 0xFF   # sensor N
t_lo = register & 0xFF          # sensor N+1
if t_hi > 127: t_hi -= 256      # sign extension
if t_lo > 127: t_lo -= 256
```

### 4.5 Cell Balancing Flags (Register 7)

For LVS, register `r[7]` in the cell data response is a 16-bit bitmask where
each bit corresponds to one cell. Bit 0 = Cell 1, Bit 15 = Cell 16.
A bit value of 1 means that cell is currently being balanced (passive balancing —
excess energy dissipated as heat through a bleed resistor).

```python
bal_flags = r[7]  # e.g. 0x0022 = bits 1 and 5 set
for cell in range(16):
    is_balancing = (bal_flags >> cell) & 1
    if is_balancing:
        print(f"  Cell {cell+1} is balancing")
```

**When does balancing occur?**

BYD LVS uses passive balancing which activates when:
- SOC is high (typically >80%, often near full charge)
- Cell voltage spread exceeds ~40-50 mV
- The system is charging or at rest (not during heavy discharge)

Balancing events are also logged as event codes 0x11 (Start Balancing) and
0x12 (Stop Balancing) in the BMU event log. The Start Balancing log entry
contains the max/min cell voltages and a balancing mask:

```
Start Balancing log data (23 bytes):
  [0-1]  max_cell_voltage (UINT16 LE, mV)
  [2-3]  min_cell_voltage (UINT16 LE, mV)
  [4-5]  balancing_mask (UINT16 LE, same as reg[7])
  [6-22] 0xFF padding

Stop Balancing log data:
  [0-1]  max_cell_voltage at end (UINT16 LE, mV)
  [2-3]  min_cell_voltage at end (UINT16 LE, mV)
  [4-5]  0x0000 (balancing stopped)
  [6-22] 0xFF padding
```

Example from real system:
```
Start: 8a 0d 51 0d 01 80 → max=3466mV, min=3409mV, Δ=57mV, mask=0x8001
Stop:  04 0d 03 0d 00 00 → max=3332mV, min=3331mV, Δ=1mV, balanced!
```

### 4.6 Complete Read Example

```python
import time
from pymodbus.client import ModbusTcpClient
from pymodbus.framer.rtu_framer import ModbusRtuFramer

client = ModbusTcpClient(host='192.168.1.155', port=8080, framer=ModbusRtuFramer)
client.connect()

def signed16(val):
    return val if val < 32768 else val - 65536

def query_module(client, bms_id):
    # Step 1: Write command
    client.write_registers(0x0550, [bms_id, 0x8100], slave=1)

    # Step 2: Poll for ready
    for _ in range(20):
        time.sleep(0.5)
        result = client.read_holding_registers(0x0551, 1, slave=1)
        if not result.isError() and result.registers[0] == 0x8801:
            break

    # Step 3: Read 4 chunks × 65 registers
    regs = []
    for _ in range(4):
        time.sleep(0.2)
        result = client.read_holding_registers(0x0558, 65, slave=1)
        if not result.isError():
            regs.extend(result.registers)
        else:
            regs.extend([0] * 65)

    # Decode
    r = regs
    cell_voltages = [signed16(r[49 + i]) for i in range(16)]  # mV
    soc = signed16(r[25]) * 0.1                                # %
    current = signed16(r[27]) * 0.1                            # A
    bat_voltage = signed16(r[21]) * 0.1                        # V

    # Temperatures (chunk 2, positions 180-183)
    temps = []
    for i in range(4):
        val = r[180 + i]
        t_hi = (val >> 8) & 0xFF
        t_lo = val & 0xFF
        if t_hi > 127: t_hi -= 256
        if t_lo > 127: t_lo -= 256
        temps.extend([t_hi, t_lo])

    return {
        'cell_voltages': cell_voltages,
        'temps': temps,
        'soc': soc,
        'current': current,
        'bat_voltage': bat_voltage,
    }

# Read all 8 modules
for bms_id in range(1, 9):
    data = query_module(client, bms_id)
    print(f"BMS{bms_id}: {data['cell_voltages']}")

client.close()
```

### 4.7 Module Serial Number (Registers 34–45)

Each BMS module reports its serial number in registers 34–45 of the cell data response
(12 registers = 24 bytes of ASCII, packed as hi/lo byte pairs):

```python
serial = ""
for i in range(34, 46):
    ch1 = (r[i] >> 8) & 0xFF
    ch2 = r[i] & 0xFF
    if 32 <= ch1 < 127: serial += chr(ch1)
    if 32 <= ch2 < 127: serial += chr(ch2)
serial = serial.rstrip('x \x00')
# Example: "P011T010Z2305150689"
```

---

## 5. Configuration Registers (0x0000–0x0066) — Read Only

These registers contain hardware configuration and serial number data.
Readable with a single `read_holding_registers(0x0000, 0x66, slave=1)`.

| Register | Description |
|----------|-------------|
| 0x0000–0x0009 | Serial number (ASCII packed in UINT16) |
| 0x000F | Working area: hi=BMU, lo=BMS |
| 0x0010 | hi=inverter type, lo nibble (bits[3:0])=BMS module count |
| 0x0011 | hi=application, lo=battery type |
| 0x0012 | hi=phase config |
| 0x004B | Address |
| 0x004C | BMU MCU type |
| 0x004D | BMS MCU type |
| 0x0063–0x0065 | Date/time (packed: year+2000, month, day, hour, min, sec) |

### Module count auto-detection

```python
result = client.read_holding_registers(0x0010, 1, slave=1)
module_count = result.registers[0] & 0x0F  # low nibble = number of BMS modules
```

**Warning:** Do not write to 0x0010–0x0012. These control hardware configuration
and incorrect values could damage the system.

---

## 6. Warning and Error Bitmasks

### BMS Errors (register 48 in cell data response)

| Bit | Description |
|-----|-------------|
| 0 | Cells Voltage Sensor Failure |
| 1 | Temperature Sensor Failure |
| 2 | BIC Communication Failure |
| 3 | Pack Voltage Sensor Failure |
| 4 | Current Sensor Failure |
| 5 | Charging MOS Failure |
| 6 | Discharging MOS Failure |
| 7 | Pre-charging MOS Failure |
| 8 | Main Relay Failure |
| 9 | Pre-charging Failed |
| 10 | Heating Device Failure |
| 11 | Radiator Failure |
| 12 | BIC Balance Failure |
| 13 | Cells Failure |
| 14 | PCB Temperature Sensor Failure |
| 15 | Functional Safety Failure |

### BMS Warnings (registers 28-29 in cell data response)

| Bit | Description |
|-----|-------------|
| 0 | Battery Over Voltage |
| 1 | Battery Under Voltage |
| 2 | Cells Over Voltage |
| 3 | Cells Under Voltage |
| 4 | Cells Imbalance |
| 5 | Charging High Temperature |
| 6 | Charging Low Temperature |
| 7 | Discharging High Temperature |
| 8 | Discharging Low Temperature |
| 9 | Charging Over Current |
| 10 | Discharging Over Current |
| 11 | Charging Over Current (Hardware) |
| 12 | Short Circuit |
| 13 | Inversely Connected |
| 14 | Interlock Switch Abnormal |
| 15 | Air Switch Abnormal |

---

## 7. Log Data Protocol (0x05A0 → 0x05A8)

Similar to cell data, but reads historical event logs:

```
Step 1: WRITE (FC16) to 0x05A0, payload: [unit_id, 0x8100]
Step 2: POLL 0x05A1 until == 0x8801
Step 3: READ from 0x05A8, count=65, repeat 5 times (325 registers)
```

Log entries contain timestamp, event code, and 23 bytes of context data.
Used by BE Connect Plus "Logs" tab.

### 7.1 Event Codes

| Code | Hex | Description |
|------|-----|-------------|
| 0 | 0x00 | Power ON |
| 1 | 0x01 | Power OFF |
| 2 | 0x02 | Events record |
| 3 | 0x03 | Timing Record |
| 4 | 0x04 | Start Charging |
| 5 | 0x05 | Stop Charging |
| 6 | 0x06 | Start DisCharging |
| 7 | 0x07 | Stop DisCharging |
| 8 | 0x08 | SOC calibration rough |
| 9 | 0x09 | SOC calibration fine |
| 10 | 0x0A | SOC calibration Stop |
| 11 | 0x0B | CAN Communication failed |
| 12 | 0x0C | Serial Communication failed |
| 13 | 0x0D | Receive PreCharge Command |
| 14 | 0x0E | PreCharge Successful |
| 15 | 0x0F | PreCharge Failure |
| 16 | 0x10 | Start end SOC calibration |
| 17 | 0x11 | **Start Balancing** |
| 18 | 0x12 | **Stop Balancing** |
| 19 | 0x13 | Address Registered |
| 20 | 0x14 | System Functional Safety Fault |
| 21 | 0x15 | Events additional info |
| 101 | 0x65 | Start Firmware Update |
| 102 | 0x66 | Firmware Update finish |
| 103 | 0x67 | Firmware Update fails |
| 104 | 0x68 | Firmware Jump into other section |
| 105 | 0x69 | Parameters table Update |
| 106 | 0x6A | SN Code was Changed |
| 107 | 0x6B | Current Calibration |
| 108 | 0x6C | Battery Voltage Calibration |
| 109 | 0x6D | PackVoltage Calibration |
| 110 | 0x6E | SOC/SOH Calibration |
| 111 | 0x6F | DateTime Calibration |

### 7.2 Log Data Format (23 bytes per entry)

**Charge/Discharge events** (codes 0x02–0x07):
```
Byte   Type     Description
[0-7]  8×UINT8  Warning/error flags
[8]    UINT8    Status byte (0x0B = normal operation)
[9]    UINT8    SOC (%)
[10]   UINT8    SOH (%)
[11-12] UINT16 LE  Battery voltage (×0.1V)
[13-14] UINT16 LE  Output voltage (×0.1V)
[15-16] INT16 LE   Current (×0.1A, signed)
[17-18] UINT16 LE  Max cell voltage (mV)
[19-20] UINT16 LE  Min cell voltage (mV)
[21]   INT8     Max temperature (°C)
[22]   INT8     Min temperature (°C)
```

**Balancing events** (codes 0x11, 0x12):
```
Byte   Type     Description
[0-1]  UINT16 LE  Max cell voltage (mV)
[2-3]  UINT16 LE  Min cell voltage (mV)
[4-5]  UINT16 LE  Balancing mask (same format as reg[7])
[6-22] 0xFF       Padding
```

**DateTime Calibration** (code 0x6F):
```
Byte   Description
[0]    Year - 2000
[1]    Month
[2]    Day
[3]    Hour
[4]    Minute
[5]    Second
[6-22] 0xFF padding
```

### 7.3 Log Data Source

Event codes and log format from
[sarnau/BYD-Battery-Box-Infos](https://github.com/sarnau/BYD-Battery-Box-Infos),
verified against BE Connect Plus CSV export.

---

## 8. Known Limitations

1. **Single connection:** Only one Modbus client at a time. BE Connect Plus,
   Node-RED, or Python — pick one.

2. **Read size:** Maximum 65 registers per FC3 request. Larger requests may
   return errors.

3. **FIFO behavior:** The 0x0558 register acts as a FIFO buffer. Reading it
   multiple times returns sequential chunks. If you only need cell voltages
   (chunk 0), one read of 65 registers suffices. For temperatures, read 4 times.

4. **Timing:** After writing 0x0550, allow 1.5-3 seconds for the BMU to collect
   data from the requested BMS module before reading. The 0x0551 polling
   mechanism handles this automatically.

5. **CAN bus conflict:** When a Victron (or other) inverter is connected via CAN,
   the BMU may occasionally return SlaveDeviceBusy for write operations.
   The write-then-poll protocol handles this gracefully with retries.

6. **No direct cell access:** Cell voltages are not stored in standard Modbus
   register space. They require the write-poll-read protocol described above.

---

## 9. Verified Data Accuracy

All values verified against BE Connect Plus v2.9 on 2026-02-19:

| Parameter | Modbus | BE Connect | Match |
|-----------|--------|------------|-------|
| Cell voltages | 3318-3323 mV | 3319-3323 mV | ✅ (±1mV timing) |
| Temperatures | 22-27°C | 22-27°C | ✅ exact |
| SOC per module | 44-84% | 44-84% | ✅ exact |
| SOH | 98-99% | 98-99% | ✅ exact |
| Current | -25.3A | -25.3A | ✅ exact |
| BAT Voltage | 53.7V | 53.7V | ✅ exact |
| V-Out | 53.8V | 53.8V | ✅ exact |

---

## 10. References

| Repository | Description |
|------------|-------------|
| [sarnau/BYD-Battery-Box-Infos](https://github.com/sarnau/BYD-Battery-Box-Infos) | Modbus protocol documentation, Python reference |
| [redpomodoro/byd_battery_box](https://github.com/redpomodoro/byd_battery_box) | Home Assistant integration (cell-level protocol source) |
| [christianh17/ioBroker.bydhvs](https://github.com/christianh17/ioBroker.bydhvs) | ioBroker adapter, hex structure docs |
| [dfch/BydCanProtocol](https://github.com/dfch/BydCanProtocol) | CAN bus protocol (Victron ↔ BMU) |

---

## 11. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────┐
│ BYD LVS Premium — Modbus Quick Reference                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ CONNECTION:  TCP 192.168.x.x:8080, RTU framing, slave=1        │
│                                                                 │
│ SUMMARY:    read_holding_registers(0x0500, 25)                  │
│             → SOC, voltages, current, temperatures              │
│                                                                 │
│ CELL DATA:  write_registers(0x0550, [bms_id, 0x8100])          │
│             poll 0x0551 until == 0x8801                         │
│             read_holding_registers(0x0558, 65) × 4              │
│             → regs[49:65] = 16 cell voltages (mV)              │
│             → regs[180:184] = 8 temperatures (packed INT8)     │
│             → regs[7] = balancing bitmask (1=active)           │
│                                                                 │
│ LOG DATA:   write_registers(0x05A0, [bms_id, 0x8100])          │
│             poll 0x05A1 until == 0x8801                         │
│             read_holding_registers(0x05A8, 65) × 5              │
│             → event code, timestamp, 23 bytes context          │
│                                                                 │
│ MODULES:    bms_id 1-4 = Tower 1, bms_id 5-8 = Tower 2        │
│             auto-detect: 0x050F = tower count × 4              │
│                                                                 │
│ TIMING:     ~2s per module, ~20s for full 8-module scan        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```
