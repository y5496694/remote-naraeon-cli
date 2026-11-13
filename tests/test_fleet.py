"""Synthetic fixtures exercise real engine and loopback HTTP, NOT physical tablets."""
import base64
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/remote-naraeon/scripts"))
from fleet import Fleet, FleetError, Busy
from monitor import MonitorServer
from secure_store import Store, StoreError, device_id, registered_devices
import remote_naraeon as cli


def png(width=120, height=80):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress((b'\0' + b'\x77\xaa\xcc' * width) * height)) + chunk(b'IEND', b'')


def obj(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}


class Tablet:
    def __init__(self, name):
        self.name, self.calls, self.counter = name, [], 0
        self.fail = None
        self.wait = None
        self.entered = threading.Event()
        self.schema = {"revision": {}, "action": {}, "image_id": {}, "coordinate_space": {},
                       "x": {}, "y": {}, "node_id": {}, "text": {}}

    def tools(self):
        return [{"name": n, "inputSchema": {"properties": self.schema if n == "phone_act" else {}}}
                for n in ("phone_read_screen", "phone_take_screenshot", "phone_act", "narae_take_control")]

    def request(self, method, payload):
        self.calls.append(payload)
        name = payload["name"]
        if name == self.fail:
            raise TimeoutError("synthetic timeout")
        if self.wait and name == "phone_take_screenshot":
            self.entered.set()
            self.wait.wait(3)
        if name == "phone_read_screen":
            return obj({"revision": self.name + '-rev-' + str(self.counter), "nodes": [
                {"id": 0, "scrollable": True}, {"id": 1, "editable": True, "hint": "문장"}]})
        if name == "phone_take_screenshot":
            result = obj({"revision": self.name + '-rev-' + str(self.counter), "image_id": self.name + '-image-' + str(self.counter)})
            result["content"].insert(0, {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png()).decode()})
            return result
        if name == "phone_act":
            self.counter += 1
            return obj({"accepted": True})
        return obj({"external_control": True})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tablets = {n: Tablet(n) for n in ('tab1', 'tab2')}
        self.fleet = Fleet(list(self.tablets), lambda name: {"name": name},
                           client_factory=lambda p, t: self.tablets[p['name']])

    def frame(self, name='tab1'):
        return self.fleet.run(name, 'frame', {"start": True})

    def arm(self, name='tab1'):
        return self.fleet.run(name, 'arm')

    def tap(self, frame, name='tab1', **overrides):
        return self.fleet.run(name, 'action', {"action": "tap", "frame_id": frame['frame_id'],
                                             "x": 12, "y": 15, **overrides})

    def test_frame_uses_fresh_revision_and_real_image_bytes_no_mutation(self):
        frame = self.frame()
        self.assertEqual([c['name'] for c in self.tablets['tab1'].calls], ['phone_read_screen', 'phone_take_screenshot'])
        self.assertEqual(base64.b64decode(frame['image'].split(',')[1]), png())
        self.assertEqual((frame['width'], frame['height']), (120, 80))
        self.assertFalse(frame['armed'])
        self.assertNotIn('revision', frame)  # Local frame handle is the browser contract.

    def test_device_ids_images_and_revisions_are_not_mixed(self):
        a, b = self.frame(), self.frame('tab2')
        self.arm('tab2')
        self.frame('tab2')
        with self.assertRaises(FleetError): self.tap(a, 'tab2')
        self.assertFalse(any(c['name'] == 'phone_act' for c in self.tablets['tab2'].calls))
        self.assertIsNotNone(self.fleet.devices['tab1'].frame)
        self.arm()
        a = self.frame()
        self.tap(a)
        params = self.tablets['tab1'].calls[-1]['arguments']
        self.assertEqual(params['image_id'], 'tab1-image-0')
        self.assertEqual(params['revision'], 'tab1-rev-0')
        self.assertEqual(params['coordinate_space'], 'image_pixels')
        self.assertNotEqual(a['frame_id'], b['frame_id'])

    def test_tap_without_explicit_arming_rejected(self):
        frame = self.frame()
        with self.assertRaises(FleetError): self.tap(frame)
        self.assertFalse(any(c['name'] == 'phone_act' for c in self.tablets['tab1'].calls))

    def test_single_use_frame_and_action_not_retried(self):
        self.arm(); frame = self.frame()
        self.assertTrue(self.tap(frame)['accepted'])
        with self.assertRaises(FleetError): self.tap(frame)
        self.assertEqual(sum(c['name'] == 'phone_act' for c in self.tablets['tab1'].calls), 1)

    def test_timeout_stops_only_affected_device_without_retries_or_reacquisition(self):
        self.frame('tab2'); self.arm(); frame = self.frame()
        self.tablets['tab1'].fail = 'phone_act'
        with self.assertRaises(TimeoutError): self.tap(frame)
        self.assertFalse(self.fleet.devices['tab1'].active)
        self.assertFalse(self.fleet.devices['tab1'].armed)
        self.assertTrue(self.fleet.devices['tab2'].active)
        self.assertEqual(sum(c['name'] == 'phone_act' for c in self.tablets['tab1'].calls), 1)
        with self.assertRaises(FleetError): self.fleet.run('tab1', 'frame')

    def test_no_automatic_resume_or_take_control_after_failure(self):
        self.tablets['tab1'].fail = 'phone_read_screen'
        with self.assertRaises(TimeoutError): self.frame()
        self.assertEqual(len(self.tablets['tab1'].calls), 1)
        self.tablets['tab1'].fail = None
        frame = self.frame()
        self.assertFalse(frame['armed'])
        self.assertFalse(any(c['name'] == 'narae_take_control' for c in self.tablets['tab1'].calls))

    def test_stale_or_out_of_range_coordinates_fail_closed(self):
        for overrides in ({'x': -1}, {'y': 80}, {'x': float('nan')}, {'x': True}, {'x': float('inf')}):
            self.arm(); frame = self.frame()
            with self.assertRaises(FleetError): self.tap(frame, **overrides)
        self.arm(); frame = self.frame()
        self.fleet.devices['tab1'].frame['at'] -= 11
        with self.assertRaises(FleetError): self.tap(frame)
        self.assertFalse(any(c['name'] == 'phone_act' for c in self.tablets['tab1'].calls))

    def test_old_frame_rejected_after_next_capture_even_same_revision(self):
        self.arm(); frame = self.frame(); self.frame()
        with self.assertRaises(FleetError): self.tap(frame)

    def test_typing_and_scroll_node_must_come_from_same_frame(self):
        for action, node in (('type', 1), ('scroll_forward', 0)):
            self.arm(); frame = self.frame()
            arguments = {'action': action, 'node_id': node, 'frame_id': frame['frame_id']}
            if action == 'type': arguments['text'] = '한글 입력'
            self.fleet.run('tab1', 'action', arguments)
            self.assertEqual(self.tablets['tab1'].calls[-1]['arguments']['node_id'], node)
        self.arm(); frame = self.frame()
        with self.assertRaises(FleetError): self.fleet.run('tab1', 'action', {'action': 'type', 'node_id': 0, 'text': 'x', 'frame_id': frame['frame_id']})

    def test_extra_fields_and_missing_capability_are_not_forwarded(self):
        self.arm(); frame = self.frame()
        with self.assertRaises(FleetError): self.tap(frame, token='not-sent')
        self.arm(); frame = self.frame()
        del self.tablets['tab1'].schema['image_id']
        with self.assertRaises(FleetError): self.tap(frame)
        self.assertFalse(any(c['name'] == 'phone_act' for c in self.tablets['tab1'].calls))

    def test_busy_device_does_not_block_other_device_or_queue_actions(self):
        dev = self.fleet.devices['tab1']
        dev.operation.acquire()
        try:
            with self.assertRaises(Busy): self.frame()
            self.assertEqual(self.frame('tab2')['device'], 'tab2')
        finally: dev.operation.release()

    def test_stop_discards_inflight_capture_and_never_changes_remote_mode(self):
        gate = threading.Event(); tablet = self.tablets['tab1']; tablet.wait = gate
        errors = []
        def capture():
            try: self.frame()
            except Exception as e: errors.append(e)
        thread = threading.Thread(target=capture); thread.start()
        self.assertTrue(tablet.entered.wait(2))
        self.fleet.run('tab1', 'stop'); gate.set(); thread.join(3)
        self.assertTrue(errors)
        self.assertIsNone(self.fleet.devices['tab1'].frame)
        self.assertFalse(self.fleet.devices['tab1'].armed)
        self.assertFalse(any('mode' in c['name'] or 'release' in c['name'] for c in tablet.calls))

    def test_permission_missing_denial_and_corrupt_image_stop_capture(self):
        tablet = self.tablets['tab1']
        original = tablet.tools
        tablet.tools = lambda: [t for t in original() if t['name'] != 'phone_take_screenshot']
        with self.assertRaises(FleetError): self.frame()
        self.assertEqual(tablet.calls, [])
        self.assertIsNone(self.fleet.devices['tab1'].frame)

    def test_arm_invalidates_frame_from_old_control_generation(self):
        frame = self.frame(); self.arm()
        self.assertIsNone(self.fleet.devices['tab1'].frame)
        with self.assertRaises(FleetError): self.tap(frame)
        self.assertFalse(any(c['name'] == 'phone_act' for c in self.tablets['tab1'].calls))

    def test_tool_denial_hides_server_secrets_and_does_not_save_frame(self):
        tablet = self.tablets['tab1']
        original = tablet.request
        def denied(method, payload):
            if payload['name'] == 'phone_take_screenshot':
                return {'isError': True, 'content': [{'type': 'text', 'text': 'secret-profile-token'}]}
            return original(method, payload)
        tablet.request = denied
        with self.assertRaises(FleetError) as caught: self.frame()
        self.assertNotIn('secret-profile-token', str(caught.exception))
        self.assertIsNone(self.fleet.devices['tab1'].frame)
        self.assertFalse(self.fleet.devices['tab1'].active)

    def test_corrupt_image_and_wrong_revision_rejected(self):
        tablet = self.tablets['tab1']
        original = tablet.request
        for wrong_revision in (False, True):
            def changed(method, payload):
                result = original(method, payload)
                if payload['name'] == 'phone_take_screenshot':
                    if wrong_revision:
                        result['content'][-1]['text'] = json.dumps({'revision': 'different', 'image_id': 'i'})
                    else:
                        result['content'][0]['data'] = 'bad-base64!'
                return result
            tablet.request = changed
            with self.assertRaises(FleetError): self.frame()
            self.assertIsNone(self.fleet.devices['tab1'].frame)

    def test_unknown_device_and_invalid_fleet_size(self):
        with self.assertRaises(FleetError): self.fleet.run('unknown', 'frame')
        for names in ([], ['a'] * 2, [f'a{i}' for i in range(13)]):
            with self.assertRaises(FleetError): Fleet(names, None)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tablet = Tablet('tab1')
        fleet = Fleet(['tab1'], lambda n: {}, client_factory=lambda p,t: self.tablet)
        self.server = MonitorServer(fleet)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def http(self, path, data=None, headers=None, authenticated=True):
        h = {'X-Narae-Session': self.server.nonce} if authenticated else {}
        if data is not None: h['Content-Type'] = 'application/json'
        h.update(headers or {})
        request = urllib.request.Request(self.server.origin + path,
                  data=json.dumps(data).encode() if data is not None else None, headers=h)
        try: response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as e: response = e
        with response: return response.status, response.headers, response.read()

    def test_bound_to_loopback_and_private_api_requires_local_session(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.http('/api/devices', authenticated=False)[0], 403)
        status, headers, body = self.http('/api/devices')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['devices'], ['tab1'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertNotIn(self.server.nonce.encode(), body)

    def test_cross_origin_host_rebinding_and_missing_nonce_rejected(self):
        for headers in ({'Origin': 'https://evil.invalid'}, {'Host': 'evil.invalid'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'X-Narae-Session': 'wrong'}):
            self.assertEqual(self.http('/api/devices', headers=headers)[0], 403)
        self.assertEqual(self.tablet.calls, [])

    def test_real_http_frame_and_action_flow(self):
        status, _, body = self.http('/api/device', {'device': 'tab1', 'operation': 'frame', 'arguments': {'start': True}})
        self.assertEqual(status, 200)
        frame = json.loads(body)['result']
        self.http('/api/device', {'device': 'tab1', 'operation': 'arm'})
        _, _, refreshed = self.http('/api/device', {'device': 'tab1', 'operation': 'frame'})
        frame = json.loads(refreshed)['result']
        status, _, body = self.http('/api/device', {'device': 'tab1', 'operation': 'action',
            'arguments': {'frame_id': frame['frame_id'], 'action': 'tap', 'x': 1, 'y': 2}})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['result']['accepted'])
        self.assertEqual(self.tablet.calls[-1]['arguments']['image_id'], 'tab1-image-0')

    def test_bounded_json_unknown_routes_and_assets_csp(self):
        self.assertEqual(self.http('/api/device', {'device': 'tab1', 'operation': 'frame', 'extra': 1})[0], 400)
        self.assertEqual(self.http('/../secure_store.py')[0], 404)
        self.assertEqual(self.http('/api/device', {'device': 'tab1', 'operation': 'frame', 'arguments': {'x': 'x'*33000}})[0], 400)
        status, headers, body = self.http('/')
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn(self.server.nonce.encode(), body)
        self.assertNotIn(b'<script>', body)

    def test_invalid_interval_fails_before_bind(self):
        for interval in (0, float('nan'), 31):
            with self.assertRaises(FleetError): MonitorServer(self.server.fleet, interval=interval)


@unittest.skipUnless(os.name == 'nt', 'Real Windows DPAPI required')
class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.p = {'url': 'https://example.invalid/first', 'token': 'FAKE_TEST_SECRET'}

    def tearDown(self): self.temp.cleanup()

    def test_named_profiles_and_default_have_separate_encrypted_files(self):
        a, b = Store(self.temp.name), Store(self.temp.name, 'tab1')
        a.save(self.p); b.save({**self.p, 'token': 'SECOND_FAKE_TEST_SECRET'})
        self.assertNotEqual(a.path, b.path)
        self.assertEqual(a.load(), self.p)
        self.assertEqual(b.load()['token'], 'SECOND_FAKE_TEST_SECRET')
        self.assertNotIn(b'SECOND_FAKE_TEST_SECRET', b.path.read_bytes())
        self.assertEqual(registered_devices(self.temp.name), ['default', 'tab1'])

    def test_missing_named_profile_never_falls_back_to_other_phone(self):
        with patch.object(cli, 'load_profile') as loader:
            with self.assertRaises(StoreError): cli.resolve_profile(Store(self.temp.name, 'missing'))
            loader.assert_not_called()

    def test_profile_names_no_path_traversal_or_reserved_windows_names(self):
        for name in ('../x', 'a/b', 'a\\b', 'A', '.', 'nul', 'con', 'com1', '', 'x'*41):
            with self.assertRaises(StoreError): device_id(name)

    def test_cli_named_register_and_device_list_without_network(self):
        def invoke(*args):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['cli', '--data-dir', self.temp.name, *args]), contextlib.redirect_stdout(out):
                result = cli.main()
            return result, json.loads(out.getvalue())
        with patch.object(cli, 'load_profile', return_value=self.p), patch.object(cli, 'Client') as client:
            self.assertEqual(invoke('--device', 'tab1', 'register', '--import-existing')[0], 0)
            code, listed = invoke('devices')
            self.assertEqual(code, 0)
            self.assertIn('tab1', listed['devices'])
            client.assert_not_called()
        self.assertFalse(Store(self.temp.name).path.exists())


if __name__ == '__main__': unittest.main()
