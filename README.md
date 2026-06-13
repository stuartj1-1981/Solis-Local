# Solis EA Local Control

Home Assistant add-on that turns a **Solis S6-EA** single-phase hybrid inverter into a
fully local, Predbat-drivable device over Modbus — no cloud, no SolisCloud dependency.

It is the active-master counterpart to the [CosyLocal](https://github.com/stuartj1-1981/CosyLocal)
passive sniffer: same add-on architecture (S6 service supervision, MQTT auto-discovery,
robust gateway reconnection), but instead of *listening* to a bus it *is* the Modbus master.

> Built for and verified against an **S6-EA1P5K-L** (5 kW, AC-coupled, 32.2 kWh Fogstar)
> behind a Waveshare RS485→WiFi/ETH gateway. See the handover doc for the hardware story.

## Why this exists

The HACS `Pho3niX90/solis_modbus` integration reads fine but its discharge-**start** slot
entity is persistently flaky. The root cause is almost certainly Modbus timing: the Solis
protocol requires **>300 ms between frames**, and the integration paces at ~50 ms. This
add-on fixes that and the broader reliability problem by:

- **One thread owning the RS485 bus.** MQTT commands are queued; the poll loop performs
  every read and write. Two transactions never collide on the single-master bus.
- **Spec-safe pacing** (`inter_frame_ms`, default 400 ms) and **block reads ≤ 50 registers**.
- **Write-then-verify**: every write is echoed *and* independently read back, with one retry.
- **Hard safety clamps** in the write path (see below).
- A runtime **Control Enable** interlock to hard-stop all writes from the HA UI.

## Hardware / gateway

| Setting | Value |
|---|---|
| Gateway | Waveshare RS485 TO WIFI/ETH (Hi-Flying M2M firmware) |
| Gateway mode | **Modbus TCP ⇔ Modbus RTU**, Network A = TCP Server **port 502** |
| UART | **9600 8N1** |
| 485 selector | ON · Modbus Polling **OFF** (gateway must not poll) |
| Inverter port | dedicated **RS485** RJ45: pin 3 = A (green/white), pin 2 = B (orange) |
| Unit address | 1 |

If you instead run the gateway in *transparent* mode (raw RTU over TCP, usually port 8899),
set `protocol: rtu_over_tcp`.

> ⚠️ **Run only ONE Modbus master on the bus.** Remove/disable the HACS solis_modbus
> integration (and any SolaX integration) before enabling this add-on — two masters collide.

## Install

1. Home Assistant → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add:
   `https://github.com/stuartj1-1981/Solis-Local`
2. Install **Solis EA Modbus Control**, open **Configuration**, set `gateway_host` (and MQTT
   if you don't use the HA Mosquitto add-on), **Save**, **Start**.
3. Entities appear automatically under a device named **Solis EA Inverter** via MQTT discovery.

## Configuration

| Option | Default | Notes |
|---|---|---|
| `gateway_host` | `0.0.0.0` | Waveshare IP |
| `gateway_port` | `502` | 502 for TCP⇔RTU mode |
| `protocol` | `tcp` | `tcp` or `rtu_over_tcp` |
| `slave_address` | `1` | Inverter unit address |
| `mqtt_*` | *(auto)* | Leave blank to auto-detect HA's MQTT service |
| `poll_interval` | `10` | Seconds per telemetry cycle |
| `inter_frame_ms` | `400` | Gap between Modbus frames (**keep > 300**) |
| `enable_control` | `true` | Expose writable control entities |
| `max_current_a` | `105` | **Hard clamp** on charge/discharge current |
| `export_limit_w` | `10000` | Upper clamp for export power write (DNO = 10 kW) |
| `debug` | `false` | Log every frame |

## Entities

### Telemetry (`sensor.*`)

Battery SOC, SOH, voltage (BMS reg 33141), current, power · Inverter AC power · Grid power ·
PV DC power (0 — AC-coupled) · AC voltage · Grid frequency · Energy today · Inverter
temperature · Battery charge/discharge state · plus a **Gateway Status** connectivity
binary_sensor (availability + LWT).

### Control (`number.*`, `select.*`, `text.*`, `switch.*`)

| Entity | Type | Register(s) | Range / options |
|---|---|---|---|
| Work Mode | select | 43110 | Self-Use / +TOU / +TOU+Grid Charge (35) |
| Work Mode (raw) | sensor | 43110 | bitfield, diagnostic |
| Reserve SOC (Backup) | number | 43024 | 0–100 % |
| Export Power Limit | number | 43074 (×100 W) | 0–`export_limit_w` |
| Charge Current Limit | number | 43141 | 0–`max_current_a` A |
| Discharge Current Limit | number | 43142 | 0–`max_current_a` A |
| Charge Start / End (slot 1) | text | 43143/44, 43145/46 | HH:MM |
| Discharge Start / End (slot 1) | text | 43147/48, 43149/50 | HH:MM |
| Control Enable | switch | *(interlock)* | ON/OFF |

This add-on drives the **legacy Time-Charging** scheme only. The V2 *Grid Time of Use*
registers (43707+/43753+) and the RC force registers (43135/43129) are **deliberately not
exposed** — both are unused/no-op on the EA.

> Charge End (43145/46) is inferred from the standard Solis legacy slot-1 layout (it sits
> between the verified charge-start and discharge-start registers). Confirm against your unit
> with `--probe` before relying on it.

## Safety

- Charge/discharge current writes are **clamped to `max_current_a` (default 105 A)**. A 125 A
  write previously caused a standby fault — the clamp makes that unreachable from HA.
- Export limit writes are clamped to `export_limit_w`.
- Reserve SOC 0–100; times range-checked to HH 0–23 / MM 0–59.
- **Control Enable = OFF** rejects every register write (and reverts the HA entity to the
  inverter's true value). Set `enable_control: false` for a fully read-only deployment.
- No EPS/EPS-backup is exposed — the EA has none; use V2L for outage cover.

## Commissioning probe

Read every mapped register once and print a decoded table (no MQTT, no writes):

```bash
# from the add-on container, or any machine that can reach the gateway
python3 solis_ea.py --probe --gateway 0.0.0.0
```

## Driving it from Predbat

Predbat controls a custom inverter through HA helper entities plus *service* automations it
triggers — the same pattern the Predbat docs document for LuxPower/custom Modbus. Map its
helpers to this add-on's entities (entity_ids are prefixed with the device name, e.g.
`number.solis_ea_inverter_discharge_current_limit` — verify yours in **Developer Tools →
States**).

**1. SOC in kWh** — Predbat needs `soc_kw`. Add a template sensor:

```yaml
template:
  - sensor:
      - name: "Home Battery SOC kWh"
        unique_id: home_battery_soc_kwh
        unit_of_measurement: "kWh"
        device_class: energy
        state: >
          {{ (states('sensor.solis_ea_inverter_battery_soc') | float(0) / 100 * 32.2) | round(2) }}
```

**2. apps.yaml** — point Predbat at the entities and define the control services:

```yaml
  soc_kw:       sensor.home_battery_soc_kwh
  soc_max:      32.2
  inverter_limit: 5000
  battery_rate_max: 5000            # ~105 A * ~48 V
  reserve:      number.solis_ea_inverter_reserve_soc_backup
  charge_rate:    number.solis_ea_inverter_charge_current_limit
  discharge_rate: number.solis_ea_inverter_discharge_current_limit
  charge_start_service:    script.solis_predbat_charge_start
  discharge_start_service: script.solis_predbat_discharge_start
```

> Predbat's `*_rate` are powers (W); the add-on's current entities are amps. Either feed the
> rate scripts an A value (W ÷ ~48 V, then clamped to `max_current_a`), or map rates to your
> own template and only use the scripts to set the time windows.

**3. The service scripts** set the legacy slots via the add-on's entities. Example
charge-start script (Predbat passes the window via its `input_number`/`input_datetime`
helpers; adapt to your config):

```yaml
script:
  solis_predbat_charge_start:
    sequence:
      - service: select.select_option
        target: { entity_id: select.solis_ea_inverter_work_mode }
        data: { option: "Self-Use + TOU + Grid Charge" }
      - service: text.set_value
        target: { entity_id: text.solis_ea_inverter_charge_start_slot_1 }
        data: { value: "{{ states('input_datetime.predbat_charge_start') [:5] }}" }
      - service: text.set_value
        target: { entity_id: text.solis_ea_inverter_charge_end_slot_1 }
        data: { value: "{{ states('input_datetime.predbat_charge_end') [:5] }}" }
```

Add **Solcast** (HACS) for the solar forecast. Keep **Control Enable** ON for unattended
Predbat operation; it resets to ON on every add-on start.

## Architecture

```
solis-ea-modbus/
├─ config.yaml            add-on manifest (options, schema, mqtt:need, maps)
├─ build.yaml             HA base images per arch
├─ Dockerfile            pip install paho-mqtt; copy rootfs; chmod
├─ translations/en.json   option labels
└─ rootfs/
   ├─ etc/services.d/solis-ea/{run,finish}   S6 service (bashio → env → exec)
   └─ opt/solis/solis_ea.py                  the controller
```

`solis_ea.py` = `SolisModbus` (TCP/RTU master, paced, verified writes) + `MQTTPublisher`
(HA discovery, LWT, command intake → queue) + `SolisController` (bus-owning poll/command
loop, clamps, interlock).

## License

MIT (see CosyLocal).
