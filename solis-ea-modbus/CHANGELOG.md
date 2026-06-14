# Changelog

## 1.0.13 — 2026-06-14

### Changed
- **Removed `PV DC Power` (33057).** The S6-EA is AC-coupled — no DC PV input — so this
  register was always 0. Live PV power should come from the PV inverter's own integration
  (e.g. Enphase). The retained MQTT discovery config is cleared on upgrade, so the orphaned
  entity disappears from HA automatically.
- **Renamed `Energy Today` → `PV Generation Today`.** Register 33035 is the EA's
  CT-measured PV generation today and matches the PV inverter's figure (verified against
  Enphase), so the name now reflects that. Friendly name only — the entity ID and history
  are unchanged.

## 1.0.12 — 2026-06-14

### Added
- **Whole-home meter/CT telemetry for Predbat's load model.** Four new read-only
  sensors so Predbat no longer needs external energy sensors:
  `House Load` (33147, W), `Grid Import Today` (33171), `Grid Export Today`
  (33175) and `House Load Today` (33179) — the Today counters are U16 ×0.1 kWh,
  `total_increasing`. PV generation already came through as `Energy Today`
  (33035). Each is a single-register read within the existing
  `MAX_REGS_PER_READ` limit. **Run `--probe` to confirm the registers populate
  and the Today values are U16 (not U32) on your firmware before relying on them.**

## 1.0.11 — 2026-06-14

### Fixed
- **Battery Voltage (BMS) scaling.** This inverter reports BMS voltage in 0.01 V units, so
  the previous `scale: 0.1` showed 531 V instead of 53.1 V. Corrected to `0.01`.

## 1.0.10 — 2026-06-14

### Fixed
- **Charge/discharge current scaling (10× too low).** These registers are in 0.1 A units,
  but the value was written raw — so a 105 A setpoint commanded only 10.5 A. Added
  `read_scale`/`write_div` of `0.1` to both `charge_current` and `discharge_current`, so
  105 A now writes 1050 (= 105.0 A) and reads back correctly. Number states are rounded to
  1 dp to avoid float noise. **Re-test at a low setpoint first to confirm before relying on
  it for control.**

## 1.0.9 — 2026-06-13

### Added
- **Heartbeat logging so a healthy run is visible.** A successful poll used to log nothing,
  making "is it actually working?" impossible to tell. Now logs `MQTT discovery published`
  once, and `Telemetry poll #N: published X/12 values (MQTT up)` on the first poll and every
  30th — confirming the loop is alive, values decode, and MQTT is connected.

## 1.0.8 — 2026-06-13

### Fixed
- **Stop splitting 32-bit register pairs (the `33150` timeout).** The wide telemetry blocks
  were chunked into 2-register reads, which split the `battery_power` u32 (`33149`/`33150`)
  and left `33150` read on its own — the inverter won't return a 32-bit low word in
  isolation, so it hung and timed out every cycle. Telemetry blocks are now targeted to the
  exact registers decoded, each 32-bit value kept in one aligned block. Also cuts the cycle
  from ~30 reads to ~10 (faster, and a smaller window for cloud-dongle bus collisions).

## 1.0.7 — 2026-06-13

### Fixed
- **A single read timeout no longer drops the connection.** Previously any no-reply
  timeout raised `OSError` and aborted the whole poll → reconnect + backoff every cycle.
  Now a timeout on one read (or chunk) is treated like any other failed read: it's skipped,
  leaves a gap, and the cycle continues. Only a real socket break (`ConnectionError`)
  triggers a reconnect. The per-chunk warning also narrows a persistently stalling
  register to ≤2 addresses for diagnosis.

## 1.0.6 — 2026-06-13

### Fixed
- **Smaller reads for picky gateways.** `MAX_REGS_PER_READ` lowered 5 → 2. A 5-register
  read drew a `gateway path unavailable` (0x0A) exception from the gateway and then wedged
  it; 1–2 register reads are stable.
- **No more reconnect storm.** When the gateway accepts TCP but stops answering Modbus,
  the poll loop now backs off exponentially (`reconnect_delay` → `reconnect_max_delay`)
  instead of reconnecting and re-polling every few seconds, giving the gateway time to
  recover. The backoff resets after a healthy cycle.

## 1.0.5 — 2026-06-13

### Fixed
- **Split block reads to fit small gateway response buffers.** Some TCP gateways cap their
  reply size (observed: reads over ~5 registers truncated to ~11 data bytes, failing the
  22- and 21-register telemetry blocks). Block reads are now chunked into
  `MAX_REGS_PER_READ` (5) registers each, with the per-register fallback retained per chunk.
  This removes the `bad byte count` warnings and reads the big telemetry blocks efficiently
  instead of one register at a time.

## 1.0.4 — 2026-06-13

### Fixed / changed
- **Validate the Modbus TCP MBAP header.** The `tcp` path now checks the protocol id
  (must be 0), a sane length, and that the transaction id echoes the request. A stale or
  desynced reply (or a wrong-`protocol` setting) now raises a clear `ModbusError` — caught
  by the per-register fallback, with the next request re-draining — instead of reading a
  garbage-length payload.

### Notes
- Diagnosis: gateways that present an MBAP header (protocol id `0x0000`) with **no** RTU CRC
  footer are Modbus **TCP** — use `protocol: tcp`. The earlier `tcp` failures were the
  stale-buffer desync fixed in 1.0.2, not a wrong protocol.

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
