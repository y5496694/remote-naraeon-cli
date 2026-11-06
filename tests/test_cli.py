import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/remote-naraeon/scripts"))
import remote_naraeon as cli
import narae_client as transport
from secure_store import Store, StoreError

PROFILE = {"url": "https://example.invalid/mcp", "token": "fake-test-credential"}


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = {"Content-Type": "application/json", "Mcp-Session-Id": "test-session"}


class FakeOpener:
    def __init__(self, failure=None, tool_error=False):
        self.calls, self.failure, self.tool_error = [], failure, tool_error

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.calls.append(payload)
        if self.failure:
            raise self.failure
        method = payload["method"]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {},
                      "serverInfo": {"name": "test-server", "version": "1"}}
        elif method == "notifications/initialized":
            return Response({})
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                for name in ("narae_status", "phone_connection_status", "phone_act")]}
        else:
            result = {"isError": self.tool_error, "content": [{"type": "text", "text": json.dumps(
                {"ok": not self.tool_error, "token": PROFILE["token"], "url": PROFILE["url"]})}]}
        return Response({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})


@unittest.skipUnless(os.name == "nt", "Requires real Windows DPAPI")
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.store = Store(self.temp.name)
        self.loader = patch.object(cli, "load_profile", return_value=PROFILE)
        self.loader.start()

    def tearDown(self):
        self.loader.stop()
        self.temp.cleanup()

    def invoke(self, *args, hidden=()):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["naraeon", "--data-dir", self.temp.name, *args]), \
                patch.object(cli, "hidden", side_effect=hidden), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main()
        return code, out.getvalue(), err.getvalue()

    def test_dpapi_save_load_without_plaintext_credentials(self):
        self.store.save(PROFILE)
        self.assertEqual(self.store.load(), PROFILE)
        for secret in PROFILE.values():
            self.assertNotIn(secret.encode(), self.store.path.read_bytes())

    def test_import_registration_needs_no_input(self):
        code, out, err = self.invoke("register", "--import-existing")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["registered"])
        self.assertEqual(self.store.load(), PROFILE)
        self.assertNotIn(PROFILE["token"], out + err)

    def test_manual_registration_requests_only_url_and_token(self):
        code, _, err = self.invoke("register", hidden=(PROFILE["url"], PROFILE["token"]))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.load(), PROFILE)

    def test_status_automatically_uses_existing_without_network(self):
        with patch.object(transport.urllib.request, "build_opener") as opener:
            code, out, err = self.invoke("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["credential_source"], "existing-narae-phone")
        opener.assert_not_called()
        self.assertFalse(self.store.path.exists())

    def test_info_works_without_registration_or_unlock(self):
        opener = FakeOpener()
        with patch.object(transport.urllib.request, "build_opener", return_value=opener):
            code, out, err = self.invoke("info")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["serverInfo"]["name"], "test-server")
        self.assertFalse(self.store.path.exists())

    def test_corrupted_cli_store_never_falls_back(self):
        self.store.path.write_bytes(b"broken")
        with patch.object(cli, "load_profile") as loader:
            code, _, _ = self.invoke("info")
        self.assertEqual(code, 1)
        loader.assert_not_called()

    def test_registration_replacement_is_explicit(self):
        self.store.save(PROFILE)
        original = self.store.path.read_bytes()
        self.assertEqual(self.invoke("register", "--import-existing")[0], 1)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.invoke("register", "--import-existing", "--replace")[0], 0)

    def test_http_failure_and_timeout_are_not_retried(self):
        for failure in (urllib.error.HTTPError(PROFILE["url"], 401, "Unauthorized", {}, None), TimeoutError()):
            opener = FakeOpener(failure=failure)
            with patch.object(transport.urllib.request, "build_opener", return_value=opener):
                code, out, err = self.invoke("info")
            self.assertEqual(code, 1)
            self.assertEqual(len(opener.calls), 1)
            self.assertNotIn(PROFILE["token"], out + err)

    def test_unknown_tool_never_calls_action(self):
        opener = FakeOpener()
        with patch.object(transport.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.invoke("call", "unknown")[0], 1)
        self.assertFalse(any(c["method"] == "tools/call" for c in opener.calls))

    def test_tool_call_once_redacts_credentials_and_reports_failure(self):
        for failed in (False, True):
            opener = FakeOpener(tool_error=failed)
            with patch.object(transport.urllib.request, "build_opener", return_value=opener):
                code, out, err = self.invoke("call", "phone_act", "--args", '{"action":"back"}')
            self.assertEqual(code, 2 if failed else 0, err)
            self.assertEqual(sum(c["method"] == "tools/call" for c in opener.calls), 1)
            for secret in PROFILE.values():
                self.assertNotIn(secret, out + err)

    def test_pc_unlock_command_is_absent(self):
        for name in ("unlock", "lock"):
            with self.assertRaises(SystemExit) as caught:
                self.invoke(name)
            self.assertEqual(caught.exception.code, 2)

    def test_redirect_does_not_forward_credentials_and_sse_matches_id(self):
        with self.assertRaises(transport.BridgeError):
            transport.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid")
        response = io.BytesIO(b'data: {"jsonrpc":"2.0","id":5,"result":{}}\n\n')
        self.assertEqual(transport.read_sse(response, 5)["id"], 5)

    def test_invalid_endpoint_is_rejected(self):
        for url in ("http://example.invalid", "https://user:pass@example.invalid/mcp", "https://example.invalid/#secret"):
            with self.assertRaises(StoreError):
                cli.validate_profile({"url": url, "token": "fake"})


if __name__ == "__main__":
    unittest.main()
