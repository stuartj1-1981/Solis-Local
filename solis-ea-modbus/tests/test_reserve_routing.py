"""Reserve routing tests (v1.0.17): 43011 / 43024 + 43110 bit 4.

Runs without network access. The controller is built with object.__new__ and fake
Modbus / MQTT objects. paho-mqtt is not needed.
"""
import logging
import queue
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "solis"))
import solis_ea  # noqa: E402

BASE = "solis_ea"
RESERVE_TOPIC = f"{BASE}/reserve_soc/state"
WORK_MODE_TOPIC = f"{BASE}/work_mode/state"


class FakeModbus:
    """Holding-register store. 43011 refuses values > 40, as on the real unit."""

    def __init__(self, regs):
        self.regs = dict(regs)
        self.writes = []
        self.fail = set()

    def read(self, fc, addr, count):
        return [self.regs.get(a, 0) for a in range(addr, addr + count)]

    def read_holding(self, addr, count):
        return self.read(0x03, addr, count)

    def write_single(self, addr, value):
        self.writes.append((addr, value))
        if addr in self.fail:
            return False
        if addr == 43011 and value > 40:
            return False
        self.regs[addr] = value
        return True


class FakeMQTT:
    connected = True

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=True):
        self.published.append((topic, payload))

    def last(self, topic):
        vals = [p for t, p in self.published if t == topic]
        return vals[-1] if vals else None


def make_ctrl(regs=None, soc=81, od_max=40, margin=2):
    start = {43011: 10, 43024: 80, 43110: 35}
    if regs:
        start.update(regs)
    c = object.__new__(solis_ea.SolisController)
    c.config = dict(solis_ea.DEFAULT_CONFIG)
    c.config.update({"mqtt_base_topic": BASE, "enable_control": True,
                     "overdischarge_soc_max": od_max, "reserve_hold_margin": margin})
    c.running = False
    c.cmd_queue = queue.Queue()
    c.modbus = FakeModbus(start)
    c.mqtt = FakeMQTT()
    c.control_enabled = True
    c.discovery_sent = True
    c._reg_cache = dict(start)
    c._last_soc = soc
    c._reserve_cmd = None
    c._reserve_target = None
    return c


def reserve(c, value):
    c._handle_command("reserve_soc", str(value))


def test_01_reserve_10_no_write():
    c = make_ctrl()
    reserve(c, 10)
    assert c.modbus.writes == []
    assert c.mqtt.last(RESERVE_TOPIC) == "10"


def test_02_reserve_20_low_path():
    c = make_ctrl()
    reserve(c, 20)
    assert c.modbus.writes == [(43011, 20)]
    assert c.mqtt.last(RESERVE_TOPIC) == "20"


def test_03_reserve_82_hold_margin():
    c = make_ctrl()
    reserve(c, 82)
    assert c.modbus.writes == [(43024, 81), (43110, 51)]
    assert c.mqtt.last(RESERVE_TOPIC) == "82"


def test_04_reserve_82_margin_off():
    c = make_ctrl(margin=0)
    reserve(c, 82)
    assert c.modbus.writes == [(43024, 82), (43110, 51)]
    assert c.mqtt.last(RESERVE_TOPIC) == "82"


def test_05_reserve_60_high_path():
    c = make_ctrl()
    reserve(c, 60)
    assert c.modbus.writes == [(43024, 60), (43110, 51)]
    assert c.mqtt.last(RESERVE_TOPIC) == "60"


def test_06_low_from_reserve_mode_only_clears_bit():
    c = make_ctrl({43110: 51, 43024: 81})
    reserve(c, 10)
    assert c.modbus.writes == [(43110, 35)]
    assert c.mqtt.last(RESERVE_TOPIC) == "10"


def test_07_low_from_reserve_mode_writes_43011_first():
    c = make_ctrl({43110: 51, 43011: 20})
    reserve(c, 10)
    assert c.modbus.writes == [(43011, 10), (43110, 35)]
    assert c.mqtt.last(RESERVE_TOPIC) == "10"


def test_08_clamped_value_at_ceiling_uses_low_path():
    c = make_ctrl(soc=40)
    reserve(c, 41)
    assert c.modbus.writes == [(43011, 40)]
    assert c.mqtt.last(RESERVE_TOPIC) == "41"
    # Read-back keeps showing the request while the add-on's target is in place.
    c._poll_control()
    assert c.mqtt.last(RESERVE_TOPIC) == "41"


def test_09_repeat_request_no_write():
    c = make_ctrl()
    reserve(c, 82)
    c.modbus.writes.clear()
    reserve(c, 82)
    assert c.modbus.writes == []
    assert c.mqtt.last(RESERVE_TOPIC) == "82"


def test_10_first_write_fails_no_second_write():
    c = make_ctrl()
    c.modbus.fail.add(43024)
    reserve(c, 82)
    assert c.modbus.writes == [(43024, 81)]
    assert c.mqtt.last(RESERVE_TOPIC) == "10"
    assert c._reserve_cmd is None and c._reserve_target is None


def test_11_external_change_publishes_actual():
    c = make_ctrl()
    reserve(c, 82)
    c._poll_control()
    assert c.mqtt.last(RESERVE_TOPIC) == "82"
    c.modbus.writes.clear()
    c.modbus.regs[43024] = 70  # changed in the Solis app
    c._poll_control()
    assert c.modbus.writes == []
    assert c.mqtt.last(RESERVE_TOPIC) == "70"


def test_12_select_keeps_bit4_no_write():
    c = make_ctrl({43110: 51})
    c._handle_command("work_mode", "Self-Use + TOU + Grid Charge")
    assert c.modbus.writes == []
    assert c.mqtt.last(WORK_MODE_TOPIC) == "Self-Use + TOU + Grid Charge"


def test_13_select_writes_with_bit4_kept():
    c = make_ctrl({43110: 19})
    c._handle_command("work_mode", "Self-Use + TOU + Grid Charge")
    assert c.modbus.writes == [(43110, 51)]
    assert c.mqtt.last(WORK_MODE_TOPIC) == "Self-Use + TOU + Grid Charge"


def test_14_poll_control_masks_bit4_for_select():
    c = make_ctrl({43110: 51})
    c._poll_control()
    assert c.mqtt.last(WORK_MODE_TOPIC) == "Self-Use + TOU + Grid Charge"
    assert c.mqtt.last(f"{BASE}/work_mode_raw/state") == "51"


def test_15_refused_43011_warns_with_hint(caplog):
    c = make_ctrl(od_max=50)
    with caplog.at_level(logging.WARNING):
        reserve(c, 45)
    assert c.modbus.writes == [(43011, 45)]
    assert c.mqtt.last(RESERVE_TOPIC) == "10"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("overdischarge_soc_max" in m and "43011" in m for m in msgs), msgs


# ---- extra coverage ---------------------------------------------------------

def test_overdischarge_sensor_published():
    c = make_ctrl({43011: 20})
    c._poll_control()
    assert c.mqtt.last(f"{BASE}/overdischarge_soc/state") == "20"
    assert c.mqtt.last(f"{BASE}/backup_soc/state") == "80"


def test_second_write_fails_publishes_actual():
    c = make_ctrl()
    c.modbus.fail.add(43110)
    reserve(c, 82)
    assert c.modbus.writes == [(43024, 81), (43110, 51)]
    # 43024 written but bit 4 still clear -> effective reserve is 43011.
    assert c.mqtt.last(RESERVE_TOPIC) == "10"
    assert c._reserve_cmd is None


def test_control_disabled_rejects_and_publishes_actual():
    c = make_ctrl({43110: 51, 43024: 75})
    c.control_enabled = False
    reserve(c, 10)
    assert c.modbus.writes == []
    assert c.mqtt.last(RESERVE_TOPIC) == "75"


def test_work_mode_unknown_in_cache_is_read():
    c = make_ctrl()
    del c._reg_cache[43110]
    reserve(c, 82)
    assert c.modbus.writes == [(43024, 81), (43110, 51)]


@pytest.mark.parametrize("req,soc,expected", [
    (82, 81, (True, 43024, 81)),   # inside margin -> hold at SOC
    (83, 81, (True, 43024, 81)),   # SOC + margin -> hold at SOC
    (84, 81, (True, 43024, 84)),   # beyond margin -> request
    (81, 81, (True, 43024, 81)),   # equal to SOC -> request
    (30, 81, (False, 43011, 30)),
    (82, None, (True, 43024, 82)),  # SOC unknown -> no clamp
])
def test_reserve_plan(req, soc, expected):
    c = make_ctrl(soc=soc)
    assert c._reserve_plan(req) == expected
