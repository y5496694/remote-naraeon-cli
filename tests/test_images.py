import base64
import contextlib
import copy
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/remote-naraeon/scripts"))
import screen_images as images
import remote_naraeon as cli
from narae_client import BridgeError


def png(width=2, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + b"\x23\x78\xaa" * width)) + chunk(b"IEND", b""))


def picture(data=None, mime="image/png"):
    return {"isError": False, "content": [
        {"type": "image", "mimeType": mime, "data": base64.b64encode(png() if data is None else data).decode()},
        {"type": "text", "text": json.dumps({"revision": "fresh-revision", "package": "test.fixture"})}]}


class Phone:
    def __init__(self, read=None, shot=None, fail=None):
        self.calls = []
        self.read = read if read is not None else {"content": [{"type": "text", "text": '{"revision":"fresh-revision"}'}]}
        self.shot = shot if shot is not None else picture()
        self.fail = fail

    def tools(self):
        return [{"name": x} for x in ("phone_read_screen", "phone_take_screenshot")]

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if self.fail:
            raise self.fail
        return self.read if params["name"] == "phone_read_screen" else self.shot


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = {x: {} for x in ("phone_read_screen", "phone_take_screenshot")}

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_png_bytes_shape_hash_and_no_inline_base64(self):
        original = picture()
        out = images.save_images(original, directory=self.root)
        item = out["images"][0]
        self.assertEqual(Path(item["path"]).read_bytes(), png())
        self.assertEqual((item["width"], item["height"]), (2, 1))
        self.assertEqual(item["sha256"], hashlib.sha256(png()).hexdigest())
        self.assertTrue(item["media"].startswith("MEDIA:"))
        self.assertNotIn('"data":', json.dumps(out))
        self.assertIn("data", original["content"][0])

    def test_latest_revision_read_once_then_capture_once_no_mode_change(self):
        phone = Phone()
        result = images.capture(phone, self.catalog, directory=self.root)
        self.assertTrue(result["captured"])
        self.assertEqual([p["name"] for _, p in phone.calls], ["phone_read_screen", "phone_take_screenshot"])
        self.assertEqual(phone.calls[1][1]["arguments"], {"revision": "fresh-revision"})

    def test_explicit_revision_does_not_read_or_retry(self):
        phone = Phone()
        images.capture(phone, self.catalog, revision="provided", directory=self.root)
        self.assertEqual(len(phone.calls), 1)
        self.assertEqual(phone.calls[0][1]["arguments"], {"revision": "provided"})

    def test_missing_permission_or_tool_never_calls_phone(self):
        phone = Phone()
        with self.assertRaisesRegex(BridgeError, "직접 허용"):
            images.capture(phone, {}, directory=self.root)
        self.assertEqual(phone.calls, [])

    def test_failed_screen_read_prevents_capture(self):
        phone = Phone(read={"isError": True, "content": [{"type": "text", "text": "locked"}]})
        self.assertTrue(images.capture(phone, self.catalog, directory=self.root)["isError"])
        self.assertEqual(len(phone.calls), 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_capture_never_saves_even_if_error_contains_image(self):
        error = picture()
        error["isError"] = True
        out = images.capture(Phone(shot=error), self.catalog, directory=self.root)
        self.assertTrue(out["isError"])
        self.assertNotIn('"data":', json.dumps(out))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timeout_is_not_retried(self):
        phone = Phone(fail=BridgeError("timeout"))
        with self.assertRaises(BridgeError):
            images.capture(phone, self.catalog, directory=self.root)
        self.assertEqual(len(phone.calls), 1)

    def test_missing_or_invalid_revision_never_captures(self):
        for value in (None, "", 123, "x" * 4097):
            phone = Phone(read={"structuredContent": {"revision": value}})
            with self.assertRaises(BridgeError):
                images.capture(phone, self.catalog, directory=self.root)
            self.assertEqual(len(phone.calls), 1)

    def test_structured_screen_revision_supported(self):
        phone = Phone(read={"structuredContent": {"revision": "structured"}})
        images.capture(phone, self.catalog, directory=self.root)
        self.assertEqual(phone.calls[1][1]["arguments"]["revision"], "structured")

    def test_invalid_base64_has_no_output_file(self):
        result = picture()
        result["content"][0]["data"] = "not base64!"
        with self.assertRaises(BridgeError):
            images.save_images(result, directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_mime_magic_dimensions_and_truncation_rejected(self):
        for result in (picture(mime="image/svg+xml"), picture(mime="image/jpeg"), picture(b"plain text"),
                       picture(png()[:-12]), picture(png(width=0)), picture(png(width=50000, height=50000))):
            with self.assertRaises(BridgeError):
                images.save_images(result, directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_large_image_rejected_before_decoding_or_writing(self):
        result = picture()
        result["content"][0]["data"] = "A" * ((images.MAX_IMAGE_BYTES + 3) // 3 * 4 + 4)
        with self.assertRaises(BridgeError):
            images.save_images(result, directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_file_or_symlink_never_overwritten(self):
        path = self.root / "same.png"
        path.write_bytes(b"keep")
        with self.assertRaises(BridgeError):
            images.save_images(picture(), output=path)
        self.assertEqual(path.read_bytes(), b"keep")

    def test_explicit_output_extension_is_checked_and_missing_one_added(self):
        with self.assertRaises(BridgeError):
            images.save_images(picture(), output=self.root / "wrong.jpg")
        out = images.save_images(picture(), output=self.root / "new")
        self.assertTrue(out["images"][0]["path"].endswith("new.png"))

    def test_all_blocks_validated_before_any_file_is_saved(self):
        result = picture()
        result["content"].append({"type": "image", "mimeType": "image/png", "data": "bad"})
        with self.assertRaises(BridgeError):
            images.save_images(result, directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_write_failure_rolls_back_only_new_files(self):
        result = picture()
        result["content"].append(copy.deepcopy(result["content"][0]))
        from unittest.mock import Mock
        real_open = images.os.open
        opened = []
        def one_then_fail(*args, **kwargs):
            if opened:
                raise OSError("test disk error")
            opened.append(args[0])
            return real_open(*args, **kwargs)
        with patch.object(images.os, "open", side_effect=one_then_fail), self.assertRaises(OSError):
            images.save_images(result, directory=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_text_only_success_is_not_claimed_as_image_capture(self):
        with self.assertRaises(BridgeError):
            images.capture(Phone(shot={"content": [{"type": "text", "text": "done"}]}), self.catalog, directory=self.root)

    def invoke(self, *args, phone=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["cli", "--data-dir", str(self.root / "store"), *args]), \
                patch.object(cli, "resolve_profile", return_value=({"url": "https://example.invalid/mcp", "token": "fake-key"}, "test")), \
                patch.object(cli, "Client", return_value=phone or Phone()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main()
        return code, out.getvalue(), err.getvalue()

    def test_cli_screenshot_writes_real_file_and_prints_attachment(self):
        code, out, err = self.invoke("screenshot", "--output", str(self.root / "photo.png"))
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertTrue(value["captured"])
        self.assertEqual((self.root / "photo.png").read_bytes(), png())
        self.assertNotIn('"data":', out)
        self.assertIn("MEDIA:", out)

    def test_generic_call_saves_image_by_default(self):
        code, out, err = self.invoke("call", "phone_take_screenshot", "--image-dir", str(self.root / "shots"))
        self.assertEqual(code, 0, err)
        self.assertTrue(Path(json.loads(out)["images"][0]["path"]).is_file())
        self.assertNotIn('"data":', out)

    def test_inline_image_option_preserves_mcp_blocks_without_files(self):
        code, out, err = self.invoke("call", "phone_take_screenshot", "--inline-images")
        self.assertEqual(code, 0, err)
        self.assertIn("data", json.loads(out)["content"][0])
        self.assertFalse(list(self.root.glob("*.png")))

    def test_cli_tool_error_exit_two_without_image(self):
        error = picture()
        error["isError"] = True
        code, out, err = self.invoke("screenshot", phone=Phone(shot=error))
        self.assertEqual(code, 2, err)
        self.assertNotIn('"data":', out)


if __name__ == "__main__":
    unittest.main()
