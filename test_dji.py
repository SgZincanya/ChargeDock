"""Protocol fixtures contain no serials; never access USB from unit tests."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from core import AlertPolicy, DJI_MODELS, defaults, validate_readings
from dji_protocol import GAUGE_TEXT, Status, Stream, crc8, crc16, frame, readings


def status_frame(slots=(), mask=None):
    body = bytearray(52 + 32 * len(slots))
    body[4:6] = b"\x5a\x02"
    body[8:12] = b"\0\x5b\x03\x03"
    body[12:14] = (len(body) - 12).to_bytes(2, "little")
    body[14:20] = b"\x03\0\0\0\0\x20"
    body[44] = sum(1 << (unit - 1) for unit, _, _ in slots) if mask is None else mask
    for index, (unit, gauge, charging) in enumerate(slots):
        off = 52 + 32 * index
        body[off], body[off + 1], body[off + 5] = 2, unit, 26
        body[off + 7] = gauge << 2 | (2 if charging else 0)
    return frame(body)


class DjiProtocolTests(unittest.TestCase):
    def decode(self, raw):
        stream, status = Stream(), Status()
        packets = stream.feed(raw)
        self.assertEqual(len(packets), 1)
        self.assertTrue(status.decode(packets[0]))
        return status

    def test_real_status_vector_crc_and_two_actual_units(self):
        # Captured on MI_06, no identifying fields. Both TX report gauge 1.
        raw = bytes.fromhex('557604a65a020000005b03036600030000000020000100000000000000000000000000000000000000000000030000003c00000002010000001a182404010078000000000000000000000000000000000000000002020000001a18240401007800000000000000000000000000000000000000002300')
        self.assertEqual(crc8(raw[:3]), raw[3])
        self.assertEqual(crc16(raw), 0)
        status = self.decode(raw)
        self.assertEqual(status.connected, 3)
        self.assertEqual([status.tx[u]['gauge'] for u in (1, 2)], [1, 1])

    def test_solo_tx2_never_becomes_tx1(self):
        status = self.decode(status_frame(((2, 5, True),)))
        values = readings(status)
        self.assertEqual(values[0].state, 'offline')
        self.assertEqual((values[1].gauge, values[1].charging), (5, True))
        self.assertIsNone(values[1].level)

    def test_all_gauges_and_charging_independent(self):
        for gauge in range(8):
            status = self.decode(status_frame(((1, 1, False), (2, gauge, True))))
            a, b = readings(status)
            self.assertEqual(a.gauge, 1)
            self.assertEqual(b.gauge, gauge or None)
            self.assertEqual(b.state, 'online' if gauge else 'unknown')
            self.assertTrue(b.charging)
            self.assertIsNone(b.level)
        self.assertEqual(set(GAUGE_TEXT), set(range(1, 8)))

    def test_offline_then_connect_before_slot_then_gauge(self):
        status = self.decode(status_frame(((2, 6, False),)))
        status.decode(status_frame())
        self.assertFalse(status.tx)
        status.decode(status_frame(mask=2))
        self.assertIsNone(readings(status)[1].gauge)
        status.decode(status_frame(((2, 2, False),)))
        self.assertEqual(readings(status)[1].gauge, 2)

    def test_missing_slot_does_not_keep_old_gauge(self):
        status = self.decode(status_frame(((2, 6, False),)))
        status.decode(status_frame(mask=2))
        self.assertIsNone(readings(status)[1].gauge)

    def test_truncation_every_split_and_coalescing(self):
        raw = status_frame(((2, 4, False),))
        for index in range(len(raw)):
            stream = Stream()
            self.assertEqual(stream.feed(raw[:index]), [])
            self.assertEqual(stream.feed(raw[index:]), [raw])
        self.assertEqual(Stream().feed(raw * 3), [raw] * 3)

    def test_crc_errors_and_resynchronization(self):
        raw = status_frame(((1, 1, False),))
        for index in range(len(raw)):
            broken = bytearray(raw); broken[index] ^= 1
            stream = Stream()
            frames = stream.feed(bytes(broken) + raw)
            self.assertEqual(frames, [raw], index)
            self.assertGreater(stream.rejected, 0)

    def test_unrelated_push_and_ack_never_supply_battery(self):
        status = Status()
        for marker in (b'\x80\x5b\x01\0', b'\0\x5b\x04\x09', b'\0\x5b\x03\x02'):
            body = bytearray(12); body[8:12] = marker
            self.assertFalse(status.decode(frame(body)))
        self.assertFalse(status.status_received)

    def test_duplicate_unit_and_invalid_mask_rejected(self):
        for raw in (status_frame(((2, 2, False), (2, 3, False)), mask=2), status_frame(mask=4)):
            status = Status()
            self.assertFalse(status.decode(raw))

    def test_v1_never_uses_v2_offsets(self):
        body = bytearray(68)
        body[8:12] = b'\0\x5b\x03\0'
        body[24] = 0x20  # TX2 present after 9-byte TX1 stub
        raw = frame(body, 1)
        status = self.decode(raw)
        values = readings(status)
        self.assertEqual(status.version, 1)
        self.assertEqual(values[1].state, 'unsupported')
        self.assertIsNone(values[1].gauge)

    def test_result_validation_and_no_alerts(self):
        values = readings(self.decode(status_frame(((1, 7, False), (2, 7, False)))))
        raw = [asdict(r) for r in values]
        self.assertEqual(validate_readings(raw, DJI_MODELS), values)
        self.assertEqual(AlertPolicy().evaluate(values, defaults()), [])
        for field, value in (('gauge', 0), ('gauge', True), ('level', 0), ('protocol', 1), ('battery_kind', 'percent'), ('state', 'bogus')):
            broken = json.loads(json.dumps(raw)); broken[0][field] = value
            with self.assertRaises(ValueError):
                validate_readings(broken, DJI_MODELS)

    def test_hid_failure_does_not_invoke_or_disable_dji_transport(self):
        import core
        with patch('hid_win.enumerate_hid', side_effect=OSError('HID failed')):
            self.assertTrue(all(r.error for r in core.read_all()))
            # DJI's parser and supervisor have no dependency on enumerate_hid.
            self.assertEqual(readings(self.decode(status_frame(((2, 1, False),))))[1].gauge, 1)


class WorkerTests(unittest.TestCase):
    def test_single_owner_command_ack_and_report_confirmation(self):
        from dji_worker import run
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            folder, now, opens = Path(temp), [1.0], []
            request = {'id':'test','setting':'low-cut','value':True}
            class Fake:
                command = None
                count = 0
                def read(self):
                    self.count += 1; now[0] += 0.5
                    if self.command:
                        ack = bytearray(12); ack[6:8] = self.command[6:8]
                        ack[8:12] = b'\x80\x5b\x01\0'
                        status = bytearray(status_frame(((2,1,False),))[:-2]); status[61] |= 0x20
                        (folder/'stop').touch()
                        return frame(ack) + frame(status)
                    (folder/'command.json').write_text(json.dumps(request),encoding='utf8')
                    return status_frame(((2,1,False),))
                def write(self, data): self.command = data
                def close(self): pass
            def factory(): opens.append(True); return Fake()
            run(folder,factory,lambda:now[0])
            raw=json.loads((folder/'state.json').read_text(encoding='utf8'))
            self.assertEqual(opens,[True])
            self.assertEqual(raw['command_result'],{'id':'test','code':'confirmed'})
            self.assertTrue(raw['settings']['low-cut'])

    def test_disconnect_busy_retry_and_reconnect(self):
        from dji_worker import run
        from dji_usb import DeviceError
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            folder, now, attempts, closed = Path(temp), [1.0], [], []
            def advance(seconds):
                now[0] += seconds
                if now[0] > 25:
                    (folder / 'stop').touch()
            class Fake:
                def __init__(self, fail): self.fail = fail
                def read(self):
                    advance(0.5)
                    if self.fail: raise DeviceError('read_failed', 'unplugged')
                    return status_frame(((2, 3, False),))
                def close(self): closed.append(self)
            def factory():
                attempts.append(now[0])
                if len(attempts) == 1: raise DeviceError('interface_busy')
                return Fake(len(attempts) == 2)
            run(folder, factory, lambda: now[0], advance)
            state = json.loads((folder / 'state.json').read_text(encoding='utf8'))
            self.assertGreaterEqual(len(attempts), 3)
            self.assertEqual(state['readings'][1]['gauge'], 3)
            self.assertEqual(state['readings'][0]['state'], 'offline')
            self.assertEqual(len(closed), 2)

    def test_silent_reads_are_bounded_and_reopened_without_fake_zero(self):
        from dji_worker import run
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            folder, now, attempts, closed = Path(temp), [1.0], [], []
            def advance(seconds):
                now[0] += seconds
                if now[0] > 20: (folder / 'stop').touch()
            class Fake:
                def read(self): advance(0.2); return b''
                def close(self): closed.append(True)
            def factory(): attempts.append(now[0]); return Fake()
            run(folder, factory, lambda: now[0], advance)
            state = json.loads((folder / 'state.json').read_text(encoding='utf8'))
            self.assertGreaterEqual(len(attempts), 2)
            self.assertGreaterEqual(len(closed), 2)
            self.assertTrue(all(r['state'] == 'no_frames' and r['level'] is None and r['gauge'] is None
                                for r in state['readings']))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.status = Status()
        self.status.decode(status_frame(((1,1,False),(2,1,False))))
        for tx in self.status.tx.values(): tx['product']='DJI Mic Mini 2'
        self.status.receiver_id='fixture-rx'

    def ack(self, seq=7, code=0):
        body=bytearray(12); body[6:8]=seq.to_bytes(2,'little'); body[8:12]=bytes((0x80,0x5b,1,code))
        return frame(body)

    def test_reference_command_golden_vector(self):
        from dji_control import build_command
        self.assertEqual(build_command(self.status,0xb640,'noise-cancel','basic').hex(),
            '551604fc025a40b6405b0102ffff000037000100dde2')

    def test_every_setting_target_wire_crc(self):
        from dji_control import SETTINGS,build_command
        for key,desc in SETTINGS.items():
            for value,_,wire in desc.options:
                units=(1,2) if desc.target=='tx' else (None,)
                for unit in units:
                    packet=build_command(self.status,7,key,value,unit,True)
                    expected=unit if unit else 0xffff if desc.target=='broadcast' else 0
                    self.assertEqual(int.from_bytes(packet[12:14],'little'),expected)
                    self.assertEqual(int.from_bytes(packet[16:18],'little'),desc.command)
                    self.assertEqual(packet[19],wire)
                    self.assertEqual(Stream().feed(packet),[packet])

    def test_v1_offline_unsupported_invalid_targets_and_reboot_guard(self):
        from dji_control import build_command,ControlError
        invalid=(('voice-tone','rich',None,False),('voice-tone','rich',True,False),
            ('stereo',True,None,False),('low-cut',True,2,False),('plug-free',True,None,False))
        for key,value,unit,confirmed in invalid:
            with self.assertRaises(ControlError): build_command(self.status,7,key,value,unit,confirmed)
        self.status.tx[2]['product']='DJI Mic Mini'
        with self.assertRaises(ControlError): build_command(self.status,7,'voice-tone','rich',2)
        self.status.version=1
        with self.assertRaises(ControlError): build_command(self.status,7,'low-cut',True)

    def test_ack_must_match_and_report_must_confirm(self):
        from dji_control import Transaction
        t=Transaction({'setting':'low-cut','value':True},7,10)
        t.acknowledge(self.ack(8)); self.assertFalse(t.ack)
        self.assertFalse(t.confirmed(self.status))
        t.acknowledge(self.ack()); self.assertTrue(t.ack)
        self.assertFalse(t.confirmed(self.status))
        self.status.settings['low-cut']=True
        for tx in self.status.tx.values(): tx['settings']['low-cut']=True
        self.assertTrue(t.confirmed(self.status))
        self.assertFalse(t.expired(13)); self.assertTrue(t.expired(13.01))

    def test_non_success_ack_and_mutual_exclusion(self):
        from dji_control import Transaction
        t=Transaction({'setting':'stereo','value':'stereo'},7,0)
        t.acknowledge(self.ack(code=0xff)); self.assertFalse(t.ack); self.assertEqual(t.ack_status,255)
        t.acknowledge(self.ack())
        self.status.settings.update({'stereo':'stereo','safety-track':True})
        self.assertFalse(t.confirmed(self.status))
        self.status.settings['safety-track']=False
        self.assertTrue(t.confirmed(self.status))

    def test_reboot_requires_same_receiver_fresh_state_and_longer_timeout(self):
        from dji_control import Transaction
        t=Transaction({'setting':'plug-free','value':True},7,0,'fixture-rx')
        self.status.settings['plug-free']=True
        self.assertFalse(t.confirmed(self.status))
        self.assertTrue(t.confirmed(self.status,True))
        t.acknowledge(self.ack())
        self.assertFalse(t.confirmed(self.status))
        self.status.receiver_id='other-rx'
        self.assertFalse(t.confirmed(self.status,True))
        self.assertFalse(t.expired(14)); self.assertTrue(t.expired(15.1))

    def test_tone_confirmation_does_not_use_other_transmitter(self):
        from dji_control import Transaction
        t=Transaction({'setting':'voice-tone','value':'rich','unit':2},7,0)
        t.acknowledge(self.ack())
        self.status.tx[1]['tone']='rich'
        self.assertFalse(t.confirmed(self.status))
        self.status.tx[2]['tone']='rich'
        self.assertTrue(t.confirmed(self.status))

    def test_mixed_broadcast_state_is_unknown(self):
        from dji_control import reported
        self.status.tx[2]['settings']['low-cut']=True
        self.assertIsNone(reported(self.status,'low-cut'))


if __name__ == '__main__':
    unittest.main()
