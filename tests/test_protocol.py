import pytest

from maxwell_control import protocol as p
from maxwell_control.headset import Headset, HeadsetTimeout
from maxwell_control.simulator import SimulatedHeadset
from maxwell_control.transport import descriptor_usage_pages


# --- framing: byte layouts taken from the source documents -----------------------------------

def test_battery_request_report_matches_documented_bytes():
    reports = p.build_reports(p.build_message(p.TYPE_REQUEST, p.battery_body()), p.ROUTE_HEADSET)
    assert len(reports) == 1
    r = reports[0]
    assert len(r) == 62
    # 06 <len 7> 80 | 05 5A 03 00 D6 0C 00
    assert r[:10] == bytes.fromhex("06 07 80 05 5A 03 00 D6 0C 00")
    assert r[10:] == bytes(52)


def test_read_key_and_param_bodies():
    assert p.build_message(p.TYPE_REQUEST, p.read_key_body(0x00)) == bytes.fromhex("05 5A 04 00 01 09 00 00")
    assert p.build_message(p.TYPE_REQUEST, p.read_param_body(0x0B)) == bytes.fromhex("05 5A 04 00 83 2C 0B 00")
    assert p.build_message(p.TYPE_REQUEST, p.write_key_body(0x24, 2)) == bytes.fromhex("05 5A 05 00 00 09 24 00 02")


def test_long_message_is_split_into_fragments():
    body = bytes(range(100))
    msg = p.build_message(p.TYPE_REQUEST, body)
    reports = p.build_reports(msg, p.ROUTE_LOCAL)
    assert [r[1] for r in reports] == [59, 45]
    stream = p.MessageStream()
    for r in reports:
        stream.feed(p.report_fragment(r))
    out = stream.pop()
    assert out.type == p.TYPE_REQUEST and out.body == body


def test_stream_handles_multiple_messages_and_garbage():
    s = p.MessageStream()
    s.feed(b"\xff\x00" + p.build_message(p.TYPE_RESPONSE, b"\x01\x09\x00\x00\x00\x03")
           + p.build_message(p.TYPE_RESULT, b"\xd6\x0c\x00\x00\x40")[:5])
    m = s.pop()
    assert m.type == p.TYPE_RESPONSE and m.body[-1] == 3
    assert s.pop() is None  # second message incomplete
    s.feed(b"\x0c\x00\x00\x40")
    assert p.parse_battery(s.pop()) == 0x40


def test_zero_length_report_is_ignored():
    r = bytearray(62)
    r[0] = 7
    r[3:6] = b"\x05\x5b\x01"
    assert p.report_fragment(bytes(r)) == b""


def test_reply_matching():
    key_reply = p.Message(p.TYPE_RESPONSE, bytes.fromhex("01 09 22 00 00 FF"))
    assert p.is_read_key_reply(key_reply, 0x22)
    assert not p.is_read_key_reply(key_reply, 0x24)
    assert p.parse_read_key_reply(key_reply) == 0xFF
    param_reply = p.Message(p.TYPE_RESPONSE, bytes.fromhex("83 2C 00 0B 00 0A"))
    assert p.is_read_param_reply(param_reply, 0x0B)
    assert p.parse_read_param_reply(param_reply) == b"\x0a"
    refused = p.Message(p.TYPE_RESPONSE, bytes.fromhex("01 09 29 00 01 80"))
    with pytest.raises(p.ProtocolError):
        p.parse_read_key_reply(refused)


def test_auto_off_value_matches_headsetcontrol_layout():
    assert p.auto_off_value(30) == bytes.fromhex("01 00 08 07 01 00 08 07")  # 1800 s
    assert p.auto_off_value(0) == bytes(8)
    assert p.parse_auto_off(p.auto_off_value(360)) == 360
    with pytest.raises(ValueError):
        p.auto_off_value(7)


def test_parse_hex():
    assert p.parse_hex("01 09 22 00") == b"\x01\x09\x22\x00"
    assert p.parse_hex("0x01,0x09") == b"\x01\x09"


def test_descriptor_usage_pages():
    # Usage Page (0xFF13), Usage (1), Collection, ..., Usage Page (Consumer)
    desc = bytes.fromhex("06 13 FF 09 01 A1 01 85 06 C0 05 0C")
    assert descriptor_usage_pages(desc) == [0xFF13, 0x0C]


# --- headset logic against the simulator ----------------------------------------------------

@pytest.fixture
def sim():
    return SimulatedHeadset()


@pytest.fixture
def headset(sim, monkeypatch):
    h = Headset(sim)
    monkeypatch.setattr(Headset, "POLL_INTERVAL", 0)
    monkeypatch.setattr(Headset, "TIMEOUT", 0.05)
    return h


def test_status(headset):
    s = headset.status(include_firmware=True)
    assert s.battery == 73
    assert s.mic_muted is False
    assert s.eq_preset == 1
    assert s.noise_filter == 1
    assert s.sidetone.enabled is False and s.sidetone.level == 12 and s.sidetone.max_level == 31
    assert s.chatmix == 10
    assert s.auto_off_minutes == 30
    assert s.headset_firmware == "1.0.5.6"
    assert s.dongle_firmware == "1.0.5.6-dongle"
    assert not s.errors


def test_headset_off_is_reported_quickly(headset, sim):
    sim.powered_on = False
    s = headset.status()
    assert not s.headset_reachable
    assert "battery" in s.errors
    # Only the battery request was retried; nothing else was attempted.
    assert all(r[:2] == p.CMD_BATTERY for r in sim.requests)


def test_sidetone_toggle_written_only_when_state_differs(headset, sim):
    headset.set_sidetone_enabled(False)  # already off
    assert not any(r[:2] == p.CMD_WRITE_PARAM for r in sim.requests)
    headset.set_sidetone_enabled(True)
    writes = [r for r in sim.requests if r[:2] == p.CMD_WRITE_PARAM]
    assert len(writes) == 1
    assert headset.sidetone().enabled is True


def test_sidetone_level_enables_then_writes_level(headset, sim):
    headset.set_sidetone_level(20)
    st = headset.sidetone()
    assert st.enabled and st.level == 20
    cmds = [r[:3] for r in sim.requests if r[:2] in (p.CMD_WRITE_PARAM, p.CMD_WRITE_KEY)]
    assert cmds == [p.CMD_WRITE_PARAM + b"\x07", p.CMD_WRITE_KEY + b"\x2c"]
    with pytest.raises(ValueError):
        headset.set_sidetone_level(32)


def test_eq_noise_voice_autooff(headset):
    headset.set_eq_preset(3)
    assert headset.eq_preset() == 3
    headset.set_noise_filter(2)
    assert headset.noise_filter() == 2
    headset.set_voice_prompt_level(0)
    assert headset.voice_prompt_level() == 0
    headset.set_auto_off(0)
    assert headset.auto_off() == 0
    with pytest.raises(ValueError):
        headset.set_eq_preset(11)


def test_refused_write_raises(headset):
    with pytest.raises(p.ProtocolError):
        headset.write_key(p.KEY_MIC_MUTE, 0)


def test_writes_are_not_retried(headset, sim):
    sim.powered_on = False
    with pytest.raises(HeadsetTimeout):
        headset.set_eq_preset(2)
    assert len(sim.requests) == 1


def test_scan_finds_known_ids(headset):
    result = headset.scan(keys=range(0x20, 0x26), params=range(0x00, 0x0C))
    assert result == {
        "key 0x20": "00 07",
        "key 0x22": "00 FF",
        "key 0x24": "00 01",
        "param 0x01": "00 " + p.auto_off_value(30).hex(" ").upper(),
        "param 0x07": "00 00",
        "param 0x0B": "00 0A",
    }


def test_list_devices_from_sysfs(tmp_path):
    from maxwell_control.transport import list_devices
    from maxwell_control.protocol import ROUTE_HEADSET

    def node(name, hid_id, descriptor):
        iface = tmp_path / "usb" / f"1-3:1.{name[-1]}"
        dev = iface / "0003:3329:4BA9.0001"
        dev.mkdir(parents=True)
        (dev / "uevent").write_text(f"HID_ID={hid_id}\nHID_NAME=Audeze Maxwell\n")
        (dev / "report_descriptor").write_bytes(descriptor)
        (tmp_path / "class").mkdir(exist_ok=True)
        (tmp_path / "class" / name).mkdir()
        (tmp_path / "class" / name / "device").symlink_to(dev)

    node("hidraw4", "0003:00003329:00004BA9", bytes.fromhex("05 0C 09 01 A1 01 C0"))  # consumer only
    node("hidraw5", "0003:00003329:00004BA9", bytes.fromhex("06 13 FF 09 01 A1 01 C0"))  # control
    node("hidraw6", "0003:0000046D:0000C52B", bytes.fromhex("06 13 FF"))  # other vendor
    devices = list_devices(str(tmp_path / "class"))
    assert [d.path for d in devices] == ["/dev/hidraw5"]
    assert devices[0].product_id == 0x4BA9
    assert devices[0].default_route == ROUTE_HEADSET
    assert devices[0].interface == "1-3:1.5"
