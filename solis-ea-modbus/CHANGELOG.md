# Changelog

## 1.0.3 — 2026-06-13

### Diagnostics
- **`RTU CRC mismatch` now logs the raw frame.** The error includes the unit/fc, the
  computed vs received CRC, the assembled frame bytes, and any bytes still waiting in the
  socket buffer. This pinpoints gateway framing quirks (a leading byte, a stripped/extra
  CRC, or a length mismatch) without needing a packet capture.

## 1.0.2 — 2026-06-13

### Fixed
- **`rtu_over_tcp`: recover from gateway buffer desync.** Stale bytes left in the gateway's
  shared serial buffer (a late/partial frame from a previous poll, or a connection killed
  mid-transaction) were read as the next response, causing a persistent `RTU CRC mismatch`
  → `timed out` → reconnect loop that never recovered. The socket buffer is now flushed
  before every request (`_drain`), so each transaction starts frame-aligned and the stream
  self-heals instead of looping. Set `debug: true` to log how many stale bytes are dropped.

## 1.0.1 — 2026-06-13

### Fixed
- **Poll loop no longer crashes on a malformed Modbus reply.** A short/odd-length or
  misframed response (e.g. a transparent gateway polled in pure-`tcp` mode) raised an
  uncaught `struct.error` that aborted every poll cycle. `read()` now validates the
  function-code echo and byte count, raising `ModbusError` so the per-register fallback
  handles it gracefully and the loop survives.
- Block-read failures now log the **raw response frame (hex)**, making protocol/unit-id
  mismatches diagnosable. If you see these warnings, try switching `protocol` to
  `rtu_over_tcp` (or set the Waveshare gateway to "Modbus TCP to RTU" mode).

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
