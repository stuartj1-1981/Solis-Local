# Changelog

## 1.0.0 — 2026-06-13

Initial release. Active Modbus master for the Solis S6-EA hybrid inverter via a Waveshare
RS485 gateway, mirroring the CosyLocal add-on architecture (S6 supervision, MQTT
auto-discovery, robust reconnection) but as a master rather than a passive sniffer.

### Telemetry (FC 0x04)
- Battery SOC / SOH / voltage (BMS reg 33141) / current / power, charge-discharge state.
- Inverter AC power, grid power, PV DC power, AC voltage, grid frequency, energy today,
  inverter temperature.
- Block reads kept ≤ 50 registers; 32-bit values decoded high-word-first; signed handling
  for current, battery/grid/inverter power and temperature.
- Gateway connectivity `binary_sensor` with MQTT availability + LWT.

### Control (FC 0x06, write-verified)
- Writable entities via MQTT discovery on the **legacy Time-Charging** scheme:
  Work Mode (select, 43110), Reserve SOC (43024), Export Power Limit (43074 ×100 W),
  Charge/Discharge Current (43141/43142), and Charge/Discharge Start/End slot-1 times
  (43143–43150 as HH:MM `text` entities).
- V2 *Grid Time of Use* (43707+/43753+) and RC force (43135/43129) registers intentionally
  **not exposed** — unused/no-op on the EA.

### Reliability & safety
- Single thread owns the RS485 bus; MQTT commands are queued and executed by the poll loop.
- Spec-safe pacing (`inter_frame_ms`, default 400 ms — fixes the sub-300 ms flakiness behind
  the HACS discharge-start "unknown" issue).
- Every write echoed and independently read back, with one retry.
- Hard clamps: charge/discharge current ≤ `max_current_a` (105 A; 125 A previously caused a
  standby fault), export ≤ `export_limit_w`, reserve 0–100 %, times range-checked.
- Runtime **Control Enable** interlock switch; `enable_control: false` for read-only.
- `rtu_over_tcp` protocol option for transparent-mode gateways.
- `--probe` one-shot register dump for commissioning.
