#!/usr/bin/env python3
# =============================================================================
# Solis EA Modbus Control — Home Assistant add-on
# =============================================================================
# Active Modbus *master* for the Solis S6-EA single-phase hybrid inverter,
# reached over a Waveshare RS485-to-WiFi/ETH gateway.
#
#   - Polls the verified telemetry register map (FC 0x04 input registers).
#   - Exposes Predbat-ready, safety-clamped CONTROL entities (FC 0x06 holding
#     register writes) via MQTT auto-discovery: number / select / text / switch.
#   - Drives the LEGACY "Time-Charging" scheme (43143-43150), NOT the unused
#     V2 "Grid Time of Use" registers. RC force registers are deliberately
#     not exposed (no-op on this unit).
#
# Design notes (why this exists rather than the HACS integration):
#   - ONE thread owns the RS485 bus. MQTT command callbacks only enqueue work;
#     the main loop performs every read and write, so two transactions never
#     collide on the single-master bus.
#   - Spec-safe pacing: >= inter_frame_ms between transactions (Solis requires
#     >300 ms; the flaky HACS reads used ~50 ms). Block reads are kept <= 50
#     registers / 100 bytes per the protocol limit.
#   - Every write is read back and verified, with one retry. This is the fix
#     for the "discharge start slot reads unknown" flakiness.
#   - Hard safety clamps in the write path: charge/discharge current never
#     exceeds max_current_a (125 A once caused a standby fault), reserve SOC
#     0-100, export limit capped at the DNO approval, times range-checked.
#   - A runtime "Control Enable" interlock switch can hard-stop all writes
#     from the HA UI without uninstalling.
# =============================================================================

import os
import sys
import time
import json
import socket
import struct
import signal
import logging
import logging.handlers
import argparse
import threading
import queue
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:  # pragma: no cover
    HAS_MQTT = False
    logging.warning("paho-mqtt not installed — MQTT publishing disabled")

VERSION = "1.0.13"

# =============================================================================
# Defaults (overridden by environment variables from the S6 run script)
# =============================================================================
DEFAULT_CONFIG = {
    "gateway_host": "0.0.0.0",
    "gateway_port": 502,
    "protocol": "tcp",            # "tcp" (Modbus TCP<->RTU) or "rtu_over_tcp"
    "slave_address": 1,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_base_topic": "solis_ea",
    "poll_interval": 10,          # seconds between telemetry cycles
    "inter_frame_ms": 400,        # >300 ms per Solis spec
    "enable_control": True,
    "max_current_a": 105,         # hard clamp on charge/discharge current
    "export_limit_w": 10000,      # hard clamp on export power limit (DNO = 10 kW)
    "socket_timeout": 4.0,
    "reconnect_delay": 2,
    "reconnect_max_delay": 60,
    "log_dir": "/config",
}

# =============================================================================
# Register maps — verified on this S6-EA1P5K-L (see HANDOVER doc)
# 32-bit values are HIGH-WORD-FIRST. Telemetry = FC04 input; control = FC03/06.
# =============================================================================

# Telemetry sensors (read FC 0x04). words = 1 (u16/s16) or 2 (u32/s32).
TELEMETRY = [
    {"oid": "battery_soc",          "addr": 33139, "words": 1, "signed": False, "scale": 1,    "unit": "%",   "dclass": "battery",     "sclass": "measurement", "name": "Battery SOC"},
    {"oid": "battery_soh",          "addr": 33140, "words": 1, "signed": False, "scale": 1,    "unit": "%",   "dclass": None,          "sclass": "measurement", "name": "Battery SOH", "icon": "mdi:battery-heart-variant"},
    {"oid": "battery_voltage",      "addr": 33141, "words": 1, "signed": False, "scale": 0.01, "unit": "V",   "dclass": "voltage",     "sclass": "measurement", "name": "Battery Voltage (BMS)"},
    {"oid": "battery_current",      "addr": 33134, "words": 1, "signed": True,  "scale": 0.1,  "unit": "A",   "dclass": "current",     "sclass": "measurement", "name": "Battery Current"},
    {"oid": "battery_power",        "addr": 33149, "words": 2, "signed": True,  "scale": 1,    "unit": "W",   "dclass": "power",       "sclass": "measurement", "name": "Battery Power"},
    {"oid": "inverter_ac_power",    "addr": 33079, "words": 2, "signed": True,  "scale": 1,    "unit": "W",   "dclass": "power",       "sclass": "measurement", "name": "Inverter AC Power"},
    {"oid": "grid_power",           "addr": 33130, "words": 2, "signed": True,  "scale": 1,    "unit": "W",   "dclass": "power",       "sclass": "measurement", "name": "Grid Power"},
    {"oid": "ac_voltage",          "addr": 33073, "words": 1, "signed": False, "scale": 0.1,  "unit": "V",   "dclass": "voltage",     "sclass": "measurement", "name": "AC Voltage"},
    {"oid": "grid_frequency",       "addr": 33094, "words": 1, "signed": False, "scale": 0.01, "unit": "Hz",  "dclass": "frequency",   "sclass": "measurement", "name": "Grid Frequency"},
    {"oid": "energy_today",         "addr": 33035, "words": 1, "signed": False, "scale": 0.1,  "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "name": "PV Generation Today"},
    {"oid": "inverter_temperature", "addr": 33093, "words": 1, "signed": True,  "scale": 0.1,  "unit": "°C",  "dclass": "temperature", "sclass": "measurement", "name": "Inverter Temperature"},
    # --- Meter / CT-derived (whole-home, for the Predbat load model) ---------
    # Single-register U16 on the verified Solis map (wills106 plugin_solis).
    # The Total counterparts (33169/33173/33177) are U32; the Today ones are U16
    # x0.1 kWh. Run --probe on your unit to confirm before relying on them.
    {"oid": "house_load",           "addr": 33147, "words": 1, "signed": False, "scale": 1,    "unit": "W",   "dclass": "power",       "sclass": "measurement",      "name": "House Load"},
    {"oid": "grid_import_today",    "addr": 33171, "words": 1, "signed": False, "scale": 0.1,  "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "name": "Grid Import Today", "icon": "mdi:transmission-tower-import"},
    {"oid": "grid_export_today",    "addr": 33175, "words": 1, "signed": False, "scale": 0.1,  "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "name": "Grid Export Today", "icon": "mdi:transmission-tower-export"},
    {"oid": "house_load_today",     "addr": 33179, "words": 1, "signed": False, "scale": 0.1,  "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "name": "House Load Today", "icon": "mdi:home-lightning-bolt"},
]

# Max registers per Modbus read. This gateway is picky about read size: large reads
# truncate (~11 data bytes) and mid-size reads (~5) draw a "gateway path unavailable"
# (0x0A) that then wedges it. Only 1-2 register reads proved reliable, so block reads
# are split into chunks of this size. Raise it if your gateway tolerates larger frames.
MAX_REGS_PER_READ = 2

# Telemetry block reads (FC04). Each: (start, count). Targeted to the registers we
# actually decode, in <= MAX_REGS_PER_READ groups that never split a 32-bit (words=2)
# pair. Reading the low word of a u32/s32 on its own (e.g. 33150) hangs this inverter,
# so each 2-word value gets its own aligned block. Fewer reads also = faster cycle.
TELEMETRY_BLOCKS = [
    (33035, 1),    # energy_today (PV generation, AC-coupled via CT)
    (33073, 1),    # ac_voltage
    (33079, 2),    # inverter_ac_power (s32)
    (33093, 2),    # inverter_temperature, grid_frequency
    (33130, 2),    # grid_power (s32)
    (33134, 2),    # battery_current, battery flag (33135)
    (33139, 2),    # battery_soc, battery_soh
    (33141, 1),    # battery_voltage
    (33147, 1),    # house_load (meter CT)
    (33149, 2),    # battery_power (s32)
    (33171, 1),    # grid_import_today (meter CT)
    (33175, 1),    # grid_export_today (meter CT)
    (33179, 1),    # house_load_today (meter CT)
]

# Battery charge/discharge flag (FC04) — published as a text sensor.
BATTERY_FLAG_ADDR = 33135

# Discovery topics for sensors removed in past versions. send_all_discovery() clears
# the retained config (empty payload) so HA drops the orphaned entity automatically.
#   pv_power — removed in 1.0.13: the S6-EA is AC-coupled (no DC PV input), so PV DC
#   Power was always 0. Live PV comes from the PV inverter's own integration (e.g. Enphase).
RETIRED_DISCOVERY_TOPICS = [
    "homeassistant/sensor/solis_ea/pv_power/config",
]

# Control holding registers (FC03 read / FC06 write).
# Legacy Time-Charging scheme. component drives the HA entity type.
CONTROL = [
    {"oid": "work_mode", "comp": "select", "addr": 43110, "name": "Work Mode",
     "options": {"Self-Use": 1, "Self-Use + TOU": 3, "Self-Use + TOU + Grid Charge": 35},
     "icon": "mdi:home-lightning-bolt"},
    {"oid": "work_mode_raw", "comp": "sensor", "addr": 43110, "name": "Work Mode (raw)",
     "icon": "mdi:cog", "ecat": "diagnostic"},
    {"oid": "reserve_soc", "comp": "number", "addr": 43024, "name": "Reserve SOC (Backup)",
     "min": 0, "max": 100, "step": 1, "unit": "%", "icon": "mdi:battery-lock", "clamp": (0, 100)},
    {"oid": "export_limit", "comp": "number", "addr": 43074, "name": "Export Power Limit",
     "min": 0, "max_cfg": "export_limit_w", "step": 100, "unit": "W", "dclass": "power",
     "read_scale": 100, "write_div": 100, "icon": "mdi:transmission-tower-export"},
    {"oid": "charge_current", "comp": "number", "addr": 43141, "name": "Charge Current Limit",
     "min": 0, "max_cfg": "max_current_a", "step": 1, "unit": "A", "dclass": "current",
     "read_scale": 0.1, "write_div": 0.1, "icon": "mdi:current-dc"},
    {"oid": "discharge_current", "comp": "number", "addr": 43142, "name": "Discharge Current Limit",
     "min": 0, "max_cfg": "max_current_a", "step": 1, "unit": "A", "dclass": "current",
     "read_scale": 0.1, "write_div": 0.1, "icon": "mdi:current-dc"},
    {"oid": "charge_start", "comp": "text", "addr_h": 43143, "addr_m": 43144, "name": "Charge Start (slot 1)"},
    {"oid": "charge_end", "comp": "text", "addr_h": 43145, "addr_m": 43146, "name": "Charge End (slot 1)"},
    {"oid": "discharge_start", "comp": "text", "addr_h": 43147, "addr_m": 43148, "name": "Discharge Start (slot 1)"},
    {"oid": "discharge_end", "comp": "text", "addr_h": 43149, "addr_m": 43150, "name": "Discharge End (slot 1)"},
    {"oid": "control_enable", "comp": "switch", "internal": True, "name": "Control Enable",
     "icon": "mdi:lock-open-check", "ecat": "config"},
]

# Control block reads (FC03).
CONTROL_BLOCKS = [
    (43024, 1),    # reserve_soc
    (43074, 1),    # export_limit
    (43110, 1),    # work_mode
    (43141, 10),   # currents + all four slot edges (43141..43150)
]

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


# =============================================================================
# Modbus helpers
# =============================================================================
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def to_signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def decode(regs: dict, spec: dict):
    """Decode a telemetry value from a {addr: raw} dict. Returns None if missing."""
    addr = spec["addr"]
    if addr not in regs:
        return None
    if spec["words"] == 2:
        hi, lo = regs.get(addr), regs.get(addr + 1)
        if hi is None or lo is None:
            return None
        val = (hi << 16) | lo
        if spec["signed"] and val & 0x80000000:
            val -= 0x100000000
    else:
        val = regs[addr]
        if spec["signed"]:
            val = to_signed16(val)
    scale = spec["scale"]
    if scale != 1:
        return round(val * scale, 3)
    return val


class ModbusError(Exception):
    """Modbus protocol exception (e.g. illegal address) — distinct from socket errors."""


# =============================================================================
# Modbus master client (owns the TCP socket; single-threaded use only)
# =============================================================================
class SolisModbus:
    def __init__(self, config: dict):
        self.host = config["gateway_host"]
        self.port = config["gateway_port"]
        self.unit = config["slave_address"]
        self.protocol = config["protocol"]
        self.inter_frame = config["inter_frame_ms"] / 1000.0
        self.timeout = config["socket_timeout"]
        self.sock = None
        self.tid = 0
        self._last_txn = 0.0

    def connect(self):
        self.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except (AttributeError, OSError):
            pass
        self._last_txn = 0.0
        logging.info("Connected to gateway %s:%s (%s, unit %s)",
                     self.host, self.port, self.protocol, self.unit)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _pace(self):
        gap = time.monotonic() - self._last_txn
        if gap < self.inter_frame:
            time.sleep(self.inter_frame - gap)

    def _drain(self):
        """Discard any stale bytes sitting in the socket buffer before sending a
        request, so the reply we read belongs to THIS transaction. Cheap RTU-over-TCP
        gateways share one serial buffer and don't frame-delimit, so a late or partial
        frame left over from a previous poll (or a killed connection) would otherwise be
        read as the next response and desync the stream until reconnect."""
        if not self.sock:
            return
        self.sock.setblocking(False)
        dropped = 0
        try:
            while True:
                chunk = self.sock.recv(512)
                if not chunk:
                    break  # peer closed; let the next recv surface it
                dropped += len(chunk)
        except (BlockingIOError, OSError):
            pass  # nothing left to read
        finally:
            self.sock.settimeout(self.timeout)
        if dropped:
            logging.debug("Drained %d stale byte(s) before request", dropped)

    def _peek_waiting(self, limit: int = 64) -> bytes:
        """Non-blocking read of whatever is still in the socket buffer — diagnostic only,
        used to dump context on a framing/CRC error. Bytes are consumed, but the next
        request drains anyway, so this never disturbs a healthy stream."""
        if not self.sock:
            return b""
        self.sock.setblocking(False)
        buf = b""
        try:
            while len(buf) < limit:
                chunk = self.sock.recv(limit - len(buf))
                if not chunk:
                    break
                buf += chunk
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)
        return buf

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("gateway closed connection")
            buf += chunk
        return buf

    def _txn(self, pdu: bytes) -> bytes:
        """Send a PDU (function code + payload), return the response PDU."""
        self._pace()
        try:
            self._drain()  # flush stale bytes so the reply matches this request
            if self.protocol == "rtu_over_tcp":
                frame = bytes([self.unit]) + pdu
                frame += struct.pack("<H", crc16_modbus(frame))
                self.sock.sendall(frame)
                resp = self._recv_rtu()
            else:  # modbus tcp
                self.tid = (self.tid + 1) & 0xFFFF
                mbap = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit)
                self.sock.sendall(mbap + pdu)
                header = self._recv_exact(7)
                r_tid, r_pid, length, _uid = struct.unpack(">HHHB", header)
                # Validate the MBAP header: protocol id must be 0, length must be sane,
                # and the transaction id must echo ours. A mismatch means a stale/desynced
                # frame (or the wrong protocol) — raise so the fallback handles it and the
                # next request re-drains, rather than reading a garbage-length payload.
                if r_pid != 0 or not (2 <= length <= 260):
                    raise ModbusError(f"bad MBAP (check `protocol`): "
                                      f"tid=0x{r_tid:04X} pid=0x{r_pid:04X} len={length} "
                                      f"hdr={header.hex()}")
                resp = self._recv_exact(length - 1)
                if r_tid != self.tid:
                    raise ModbusError(f"MBAP tid mismatch: sent 0x{self.tid:04X} "
                                      f"got 0x{r_tid:04X} (stale frame) hdr={header.hex()}")
        finally:
            self._last_txn = time.monotonic()

        if resp and (resp[0] & 0x80):
            code = resp[1] if len(resp) > 1 else 0
            raise ModbusError(f"exception 0x{code:02X} for fc 0x{resp[0] & 0x7F:02X}")
        return resp

    def _recv_rtu(self) -> bytes:
        """Receive an RTU frame (transparent mode) and return the PDU (no unit/CRC)."""
        head = self._recv_exact(2)  # unit, fc
        fc = head[1]
        if fc & 0x80:
            rest = self._recv_exact(3)  # exception code + CRC
            frame = head + rest
        elif fc in (0x03, 0x04):
            bc = self._recv_exact(1)
            rest = self._recv_exact(bc[0] + 2)  # data + CRC
            frame = head + bc + rest
        elif fc in (0x06, 0x10):
            rest = self._recv_exact(6)  # addr(2)+val/count(2)+CRC(2)
            frame = head + rest
        else:
            rest = self._recv_exact(6)
            frame = head + rest
        calc = crc16_modbus(frame[:-2])
        got = struct.unpack("<H", frame[-2:])[0]
        if calc != got:
            extra = self._peek_waiting()  # reveals a prefix byte / missing CRC / next frame
            raise ModbusError(
                f"RTU CRC mismatch (unit=0x{head[0]:02X} fc=0x{fc:02X} "
                f"calc=0x{calc:04X} got=0x{got:04X}) frame={frame.hex()}"
                + (f" +{len(extra)}B waiting: {extra.hex()}" if extra else ""))
        return frame[1:-2]  # strip unit + CRC

    def read(self, fc: int, addr: int, count: int) -> list:
        pdu = struct.pack(">BHH", fc, addr, count)
        resp = self._txn(pdu)
        # Validate framing before unpacking. A short/odd/misframed reply (e.g. a
        # transparent gateway being polled in pure-TCP mode) must surface as a
        # ModbusError so _read_block's fallback handles it — never a struct.error
        # that would abort the whole poll loop. The raw frame is logged to aid
        # diagnosis (wrong `protocol`, wrong unit id, etc.).
        if len(resp) < 2:
            raise ModbusError(f"fc 0x{fc:02X} addr {addr}: truncated reply "
                              f"({len(resp)} bytes): {resp.hex()}")
        if resp[0] != fc:
            raise ModbusError(f"fc 0x{fc:02X} addr {addr}: unexpected fc 0x{resp[0]:02X} "
                              f"in reply (check `protocol`/unit): {resp.hex()}")
        bc = resp[1]
        data = resp[2:2 + bc]
        if bc % 2 or len(data) != bc:
            raise ModbusError(f"fc 0x{fc:02X} addr {addr}: bad byte count "
                              f"(declared {bc}, got {len(data)}): {resp.hex()}")
        return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, bc, 2)]

    def read_input(self, addr: int, count: int) -> list:
        return self.read(0x04, addr, count)

    def read_holding(self, addr: int, count: int) -> list:
        return self.read(0x03, addr, count)

    def write_single(self, addr: int, value: int) -> bool:
        """FC06 write, then read back and verify. One retry. Returns True if confirmed."""
        value &= 0xFFFF
        for attempt in (1, 2):
            try:
                resp = self._txn(struct.pack(">BHH", 0x06, addr, value))
                r_addr, r_val = struct.unpack(">HH", resp[1:5])
                if r_addr == addr and r_val == value:
                    # Belt and braces: independent read-back.
                    rb = self.read_holding(addr, 1)[0]
                    if rb == value:
                        return True
                    logging.warning("Write verify mismatch reg %d: wrote %d, read %d (attempt %d)",
                                    addr, value, rb, attempt)
                else:
                    logging.warning("Write echo mismatch reg %d (attempt %d)", addr, attempt)
            except (ModbusError, OSError) as e:
                logging.warning("Write reg %d failed: %s (attempt %d)", addr, e, attempt)
        logging.error("Write reg %d = %d FAILED after retries", addr, value)
        return False


# =============================================================================
# MQTT publisher + command intake
# =============================================================================
class MQTTPublisher:
    def __init__(self, config: dict, command_queue: "queue.Queue"):
        self.config = config
        self.base = config["mqtt_base_topic"]
        self.status_topic = f"{self.base}/status"
        self.cmd_queue = command_queue
        self.client = None
        self.connected = False
        self.expire_after = max(120, config["poll_interval"] * 6)
        if HAS_MQTT:
            self._setup()

    def _setup(self):
        self.client = mqtt.Client(client_id="solis_ea_modbus", protocol=mqtt.MQTTv311)
        if self.config["mqtt_user"]:
            self.client.username_pw_set(self.config["mqtt_user"], self.config["mqtt_pass"])
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.will_set(self.status_topic, payload="offline", qos=1, retain=True)
        try:
            self.client.connect(self.config["mqtt_host"], self.config["mqtt_port"], keepalive=60)
            self.client.loop_start()
        except Exception as e:
            logging.error("MQTT connection failed: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logging.info("MQTT connected")
            if self.config["enable_control"]:
                client.subscribe(f"{self.base}/+/set")
                logging.info("Subscribed to %s/+/set", self.base)
        else:
            logging.error("MQTT connect failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logging.warning("MQTT disconnected unexpectedly: rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        # Only enqueue — the main (bus-owning) thread performs the write.
        parts = msg.topic.split("/")
        if len(parts) >= 3 and parts[-1] == "set":
            oid = parts[-2]
            try:
                payload = msg.payload.decode("utf-8").strip()
            except Exception:
                payload = ""
            self.cmd_queue.put((oid, payload))
            logging.info("Command queued: %s = %r", oid, payload)

    def _device(self) -> dict:
        return {
            "identifiers": ["solis_ea_modbus"],
            "name": "Solis EA Inverter",
            "manufacturer": "Solis",
            "model": "S6-EA1P5K-L",
            "sw_version": VERSION,
        }

    def publish(self, topic: str, payload, retain=True):
        if self.client and self.connected:
            self.client.publish(topic, payload, retain=retain)

    def publish_status(self, online: bool):
        self.publish(self.status_topic, "online" if online else "offline")

    # ---- discovery -----------------------------------------------------------
    def send_all_discovery(self):
        # Clear retained config for sensors removed in past versions so HA drops them.
        for topic in RETIRED_DISCOVERY_TOPICS:
            self.publish(topic, "")
        self._disc_gateway_status()
        for s in TELEMETRY:
            self._disc_sensor(s["oid"], s["name"], unit=s.get("unit"),
                              dclass=s.get("dclass"), sclass=s.get("sclass"),
                              icon=s.get("icon"))
        self._disc_sensor("battery_status", "Battery Charge/Discharge",
                          icon="mdi:battery-charging")
        if self.config["enable_control"]:
            for c in CONTROL:
                self._disc_control(c)

    def _disc_gateway_status(self):
        payload = {
            "name": "Gateway Status",
            "unique_id": "solis_ea_gateway_status",
            "state_topic": self.status_topic,
            "payload_on": "online", "payload_off": "offline",
            "device_class": "connectivity", "entity_category": "diagnostic",
            "device": self._device(),
        }
        self.publish("homeassistant/binary_sensor/solis_ea/gateway_status/config",
                     json.dumps(payload))

    def _disc_sensor(self, oid, name, unit=None, dclass=None, sclass=None, icon=None, ecat=None):
        payload = {
            "name": name,
            "unique_id": f"solis_ea_{oid}",
            "state_topic": f"{self.base}/{oid}/state",
            "expire_after": self.expire_after,
            "availability": [{"topic": self.status_topic}],
            "device": self._device(),
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if dclass:
            payload["device_class"] = dclass
        if sclass:
            payload["state_class"] = sclass
        if icon:
            payload["icon"] = icon
        if ecat:
            payload["entity_category"] = ecat
        self.publish(f"homeassistant/sensor/solis_ea/{oid}/config", json.dumps(payload))

    def _disc_control(self, c):
        oid, comp = c["oid"], c["comp"]
        if comp == "sensor":
            self._disc_sensor(oid, c["name"], icon=c.get("icon"), ecat=c.get("ecat"))
            return
        base_payload = {
            "name": c["name"],
            "unique_id": f"solis_ea_{oid}",
            "state_topic": f"{self.base}/{oid}/state",
            "command_topic": f"{self.base}/{oid}/set",
            "availability": [{"topic": self.status_topic}],
            "device": self._device(),
        }
        if c.get("icon"):
            base_payload["icon"] = c["icon"]
        if c.get("ecat"):
            base_payload["entity_category"] = c["ecat"]

        if comp == "number":
            base_payload["min"] = c["min"]
            base_payload["max"] = self.config[c["max_cfg"]] if "max_cfg" in c else c["max"]
            base_payload["step"] = c["step"]
            base_payload["mode"] = "box"
            if c.get("unit"):
                base_payload["unit_of_measurement"] = c["unit"]
            if c.get("dclass"):
                base_payload["device_class"] = c["dclass"]
        elif comp == "select":
            base_payload["options"] = list(c["options"].keys())
        elif comp == "text":
            base_payload["pattern"] = TIME_PATTERN
            base_payload["min"] = 5
            base_payload["max"] = 5
            base_payload.setdefault("icon", "mdi:clock-outline")
        elif comp == "switch":
            base_payload["payload_on"] = "ON"
            base_payload["payload_off"] = "OFF"

        self.publish(f"homeassistant/{comp}/solis_ea/{oid}/config", json.dumps(base_payload))

    def stop(self):
        if self.client:
            self.publish_status(False)
            self.client.loop_stop()
            self.client.disconnect()


# =============================================================================
# Controller — owns the bus, runs the poll/command loop
# =============================================================================
class SolisController:
    def __init__(self, config: dict):
        self.config = config
        self.running = False
        self.cmd_queue: "queue.Queue" = queue.Queue()
        self.modbus = SolisModbus(config)
        self.mqtt = MQTTPublisher(config, self.cmd_queue)
        self.control_enabled = bool(config["enable_control"])  # runtime interlock
        self.discovery_sent = False

    # ---- connection with exponential backoff --------------------------------
    def _connect_bus(self) -> bool:
        delay = self.config["reconnect_delay"]
        while self.running:
            try:
                self.modbus.connect()
                return True
            except OSError as e:
                self.mqtt.publish_status(False)
                logging.error("Gateway connect failed: %s — retry in %ss", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, self.config["reconnect_max_delay"])
        return False

    # ---- block reads with chunking + per-address fallback -------------------
    def _read_block(self, fc: int, start: int, count: int) -> dict:
        """Read `count` registers from `start`, split into <= MAX_REGS_PER_READ chunks
        so a gateway that caps its response size doesn't truncate. A chunk that still
        fails (CRC/exception/short frame) drops to a per-register fallback, leaving gaps
        for registers the inverter doesn't expose rather than failing the whole block."""
        out = {}
        for off in range(0, count, MAX_REGS_PER_READ):
            sub_start = start + off
            sub_count = min(MAX_REGS_PER_READ, count - off)
            try:
                regs = self.modbus.read(fc, sub_start, sub_count)
                out.update({sub_start + i: regs[i] for i in range(len(regs))})
            except (ModbusError, TimeoutError) as e:
                # A single unanswered read (no-reply timeout, CRC, or exception) must not
                # abort the whole cycle — skip it, leave a gap, and carry on. The next
                # request re-drains, so a late reply can't desync us. Only a real socket
                # break (ConnectionError) propagates to trigger a reconnect.
                logging.warning("Block read fc%d %d+%d failed (%s) — per-register fallback",
                                fc, sub_start, sub_count, e)
                for a in range(sub_start, sub_start + sub_count):
                    try:
                        out[a] = self.modbus.read(fc, a, 1)[0]
                    except (ModbusError, TimeoutError):
                        pass  # leave gap; keep last published value
        return out

    # ---- telemetry ----------------------------------------------------------
    def _poll_telemetry(self):
        regs = {}
        for start, count in TELEMETRY_BLOCKS:
            regs.update(self._read_block(0x04, start, count))
        published = 0
        for s in TELEMETRY:
            val = decode(regs, s)
            if val is not None:
                self.mqtt.publish(f"{self.config['mqtt_base_topic']}/{s['oid']}/state", str(val))
                published += 1
        # Battery charge/discharge flag
        flag = regs.get(BATTERY_FLAG_ADDR)
        if flag is not None:
            text = {0: "Charging", 1: "Discharging"}.get(flag, f"State {flag}")
            self.mqtt.publish(f"{self.config['mqtt_base_topic']}/battery_status/state", text)
        # Heartbeat so a healthy run is visible: log the first poll and then periodically.
        self._poll_count = getattr(self, "_poll_count", 0) + 1
        if self._poll_count == 1 or self._poll_count % 30 == 0:
            logging.info("Telemetry poll #%d: published %d/%d values (MQTT %s)",
                         self._poll_count, published, len(TELEMETRY),
                         "up" if self.mqtt.connected else "DOWN")

    # ---- control state reflection ------------------------------------------
    def _poll_control(self):
        regs = {}
        for start, count in CONTROL_BLOCKS:
            regs.update(self._read_block(0x03, start, count))
        base = self.config["mqtt_base_topic"]
        for c in CONTROL:
            oid = c["oid"]
            if c.get("internal"):
                continue
            if c["comp"] == "select":
                raw = regs.get(c["addr"])
                if raw is not None:
                    rev = {v: k for k, v in c["options"].items()}
                    if raw in rev:
                        self.mqtt.publish(f"{base}/{oid}/state", rev[raw])
            elif c["comp"] == "sensor":
                raw = regs.get(c["addr"])
                if raw is not None:
                    self.mqtt.publish(f"{base}/{oid}/state", str(raw))
            elif c["comp"] == "number":
                raw = regs.get(c["addr"])
                if raw is not None:
                    val = round(raw * c.get("read_scale", 1), 1)
                    self.mqtt.publish(f"{base}/{oid}/state", str(val))
            elif c["comp"] == "text":
                h, m = regs.get(c["addr_h"]), regs.get(c["addr_m"])
                if h is not None and m is not None:
                    self.mqtt.publish(f"{base}/{oid}/state", f"{h:02d}:{m:02d}")
        # interlock switch state
        self.mqtt.publish(f"{base}/control_enable/state",
                          "ON" if self.control_enabled else "OFF")

    # ---- command handling (runs on the main/bus thread) ---------------------
    def _drain_commands(self):
        while True:
            try:
                oid, payload = self.cmd_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_command(oid, payload)
            except Exception as e:
                logging.error("Command %s=%r failed: %s", oid, payload, e)

    def _spec(self, oid):
        for c in CONTROL:
            if c["oid"] == oid:
                return c
        return None

    def _handle_command(self, oid, payload):
        base = self.config["mqtt_base_topic"]

        # The interlock itself is always actionable.
        if oid == "control_enable":
            self.control_enabled = (payload.upper() == "ON")
            self.mqtt.publish(f"{base}/control_enable/state",
                              "ON" if self.control_enabled else "OFF")
            logging.warning("Control interlock %s", "ENABLED" if self.control_enabled else "DISABLED")
            return

        spec = self._spec(oid)
        if not spec:
            logging.warning("Unknown control entity: %s", oid)
            return

        # Global guards.
        if not self.config["enable_control"] or not self.control_enabled:
            logging.warning("Write REJECTED (control disabled): %s=%r", oid, payload)
            self._publish_actual(spec)  # revert HA UI to true value
            return

        comp = spec["comp"]
        if comp == "select":
            if payload not in spec["options"]:
                logging.warning("Invalid work_mode option: %r", payload)
                self._publish_actual(spec)
                return
            self._write_verify(spec, [(spec["addr"], spec["options"][payload])])

        elif comp == "number":
            try:
                req = float(payload)
            except ValueError:
                logging.warning("Non-numeric value for %s: %r", oid, payload)
                self._publish_actual(spec)
                return
            lo = spec["min"]
            hi = self.config[spec["max_cfg"]] if "max_cfg" in spec else spec["max"]
            clamped = max(lo, min(hi, req))
            if clamped != req:
                logging.warning("CLAMPED %s: %s -> %s [%s..%s]", oid, req, clamped, lo, hi)
            reg_val = int(round(clamped / spec.get("write_div", 1)))
            self._write_verify(spec, [(spec["addr"], reg_val)])

        elif comp == "text":
            try:
                hh, mm = payload.split(":")
                hh, mm = int(hh), int(mm)
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError
            except ValueError:
                logging.warning("Invalid time for %s: %r (need HH:MM)", oid, payload)
                self._publish_actual(spec)
                return
            self._write_verify(spec, [(spec["addr_h"], hh), (spec["addr_m"], mm)])

    def _write_verify(self, spec, writes):
        """Perform clamped writes (already validated), then publish the true state."""
        ok = True
        for addr, val in writes:
            if not self.modbus.write_single(addr, val):
                ok = False
        if ok:
            logging.info("Wrote %s: %s", spec["oid"], writes)
        self._publish_actual(spec)

    def _publish_actual(self, spec):
        """Re-read the register(s) for one control and publish the real value."""
        base = self.config["mqtt_base_topic"]
        oid, comp = spec["oid"], spec["comp"]
        try:
            if comp in ("select", "sensor", "number"):
                raw = self.modbus.read_holding(spec["addr"], 1)[0]
                if comp == "select":
                    rev = {v: k for k, v in spec["options"].items()}
                    if raw in rev:
                        self.mqtt.publish(f"{base}/{oid}/state", rev[raw])
                elif comp == "number":
                    self.mqtt.publish(f"{base}/{oid}/state",
                                      str(round(raw * spec.get("read_scale", 1), 1)))
                else:
                    self.mqtt.publish(f"{base}/{oid}/state", str(raw))
            elif comp == "text":
                h = self.modbus.read_holding(spec["addr_h"], 1)[0]
                m = self.modbus.read_holding(spec["addr_m"], 1)[0]
                self.mqtt.publish(f"{base}/{oid}/state", f"{h:02d}:{m:02d}")
        except (ModbusError, OSError) as e:
            logging.warning("Could not re-read %s: %s", oid, e)

    # ---- main loop ----------------------------------------------------------
    def run(self):
        self.running = True
        logging.info("=" * 62)
        logging.info("SOLIS EA MODBUS CONTROL v%s", VERSION)
        logging.info("  Gateway: %s:%s (%s) unit %s",
                     self.config["gateway_host"], self.config["gateway_port"],
                     self.config["protocol"], self.config["slave_address"])
        logging.info("  Control: %s | max current %s A | export <= %s W",
                     self.config["enable_control"], self.config["max_current_a"],
                     self.config["export_limit_w"])
        logging.info("  Poll %ss | inter-frame %sms | MQTT %s",
                     self.config["poll_interval"], self.config["inter_frame_ms"],
                     "on" if HAS_MQTT else "off")
        logging.info("=" * 62)

        if not self._connect_bus():
            return

        fail_streak = 0
        while self.running:
            cycle_start = time.monotonic()
            try:
                if not self.discovery_sent and self.mqtt.connected:
                    self.mqtt.send_all_discovery()
                    self.discovery_sent = True
                    logging.info("MQTT discovery published — entities should appear in HA")

                self._poll_telemetry()
                if self.config["enable_control"]:
                    self._poll_control()
                    self._drain_commands()

                self.mqtt.publish_status(True)
                fail_streak = 0  # healthy cycle — reset backoff

            except (ConnectionError, OSError) as e:
                # A flaky gateway can accept TCP but stop answering Modbus (it wedges
                # after a "gateway path unavailable"). Reconnecting immediately just
                # hammers it, so back off exponentially to give it time to recover.
                fail_streak += 1
                backoff = min(self.config["reconnect_delay"] * (2 ** (fail_streak - 1)),
                              self.config["reconnect_max_delay"])
                logging.warning("Bus I/O error: %s — backing off %ss then reconnecting "
                                "(failure %d)", e, backoff, fail_streak)
                self.mqtt.publish_status(False)
                time.sleep(backoff)
                if not self._connect_bus():
                    break
                continue
            except Exception as e:  # pragma: no cover
                logging.error("Loop error: %s", e, exc_info=True)

            # Sleep the remainder of the poll interval, staying responsive to commands.
            while self.running and (time.monotonic() - cycle_start) < self.config["poll_interval"]:
                if self.config["enable_control"] and not self.cmd_queue.empty():
                    try:
                        self._drain_commands()
                    except (ConnectionError, OSError):
                        break
                time.sleep(0.2)

    def stop(self):
        self.running = False
        self.mqtt.stop()
        self.modbus.close()

    # ---- one-shot commissioning probe ---------------------------------------
    def probe(self):
        self.modbus.connect()
        print(f"\nSolis EA probe — {self.config['gateway_host']}:{self.config['gateway_port']} "
              f"({self.config['protocol']}) unit {self.config['slave_address']}\n")
        regs = {}
        for start, count in TELEMETRY_BLOCKS:
            regs.update(self._read_block(0x04, start, count))
        print("TELEMETRY (FC04)")
        for s in TELEMETRY:
            print(f"  {s['name']:<26} reg {s['addr']:<6} = {decode(regs, s)} {s.get('unit','')}")
        flag = regs.get(BATTERY_FLAG_ADDR)
        print(f"  {'Battery flag':<26} reg {BATTERY_FLAG_ADDR:<6} = {flag} "
              f"({ {0:'charging',1:'discharging'}.get(flag,'?') })")
        cregs = {}
        for start, count in CONTROL_BLOCKS:
            cregs.update(self._read_block(0x03, start, count))
        print("\nCONTROL (FC03)")
        for c in CONTROL:
            if c.get("internal"):
                continue
            if c["comp"] == "text":
                h, m = cregs.get(c["addr_h"]), cregs.get(c["addr_m"])
                print(f"  {c['name']:<26} reg {c['addr_h']}/{c['addr_m']} = "
                      f"{h:02d}:{m:02d}" if h is not None and m is not None
                      else f"  {c['name']:<26} = (no data)")
            else:
                print(f"  {c['name']:<26} reg {c['addr']:<6} = {cregs.get(c['addr'])}")
        print()
        self.modbus.close()


# =============================================================================
# Entry point
# =============================================================================
def build_config(args) -> dict:
    config = DEFAULT_CONFIG.copy()
    env_map = {
        "GATEWAY_HOST": ("gateway_host", str),
        "GATEWAY_PORT": ("gateway_port", int),
        "PROTOCOL": ("protocol", str),
        "SLAVE_ADDRESS": ("slave_address", int),
        "MQTT_HOST": ("mqtt_host", str),
        "MQTT_PORT": ("mqtt_port", int),
        "MQTT_USER": ("mqtt_user", str),
        "MQTT_PASS": ("mqtt_pass", str),
        "POLL_INTERVAL": ("poll_interval", int),
        "INTER_FRAME_MS": ("inter_frame_ms", int),
        "ENABLE_CONTROL": ("enable_control", lambda v: str(v).lower() in ("1", "true", "yes", "on")),
        "MAX_CURRENT_A": ("max_current_a", int),
        "EXPORT_LIMIT_W": ("export_limit_w", int),
        "LOG_DIR": ("log_dir", str),
    }
    for env_key, (cfg_key, conv) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None and val != "":
            try:
                config[cfg_key] = conv(val)
            except (ValueError, TypeError):
                pass
    # CLI overrides
    if args.gateway:
        config["gateway_host"] = args.gateway
    if args.port:
        config["gateway_port"] = args.port
    return config


def main():
    parser = argparse.ArgumentParser(description="Solis EA Modbus Control")
    parser.add_argument("--gateway", default=None, help="Gateway IP override")
    parser.add_argument("--port", type=int, default=None, help="Gateway port override")
    parser.add_argument("--probe", action="store_true", help="Read all registers once and exit")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    config = build_config(args)

    level = logging.DEBUG if (args.debug or os.environ.get("DEBUG") == "true") else logging.INFO
    Path(config["log_dir"]).mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(config["log_dir"]) / "solis_ea.log",
            maxBytes=10 * 1024 * 1024, backupCount=5, mode="a"))
    except OSError:
        pass
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)

    controller = SolisController(config)

    if args.probe:
        controller.probe()
        return

    def shutdown(signum, frame):
        logging.info("Signal %s — shutting down", signum)
        controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    controller.run()


if __name__ == "__main__":
    main()
