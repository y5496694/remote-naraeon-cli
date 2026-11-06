"""Bounded MCP image extraction. Never prints base64, overwrites files or changes phone grants."""
import base64
import binascii
import copy
import datetime
import hashlib
import os
import struct
import uuid
import zlib
from pathlib import Path

from narae_client import BridgeError, tool_failed

MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 40_000_000


def image_shape(data, mime):
    if mime == "image/png":
        if (len(data) < 45 or data[:8] != b"\x89PNG\r\n\x1a\n" or
                data[8:16] != b"\x00\x00\x00\rIHDR" or
                data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82" or
                zlib.crc32(data[12:29]) & 0xffffffff != struct.unpack(">I", data[29:33])[0]):
            raise BridgeError("유효한 PNG 이미지가 아닙니다.")
        width, height = struct.unpack(">II", data[16:24])
    elif mime == "image/jpeg":
        if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            raise BridgeError("유효한 JPEG 이미지가 아닙니다.")
        i, width, height = 2, 0, 0
        while i < len(data) - 2:
            if data[i] != 0xff:
                break
            while i < len(data) and data[i] == 0xff:
                i += 1
            if i >= len(data):
                break
            marker = data[i]
            i += 1
            if marker in (0xd9, 0xda):
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd8:
                continue
            if i + 2 > len(data):
                break
            size = int.from_bytes(data[i:i + 2], "big")
            if size < 2 or i + size > len(data):
                break
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if size < 8:
                    break
                height, width = struct.unpack(">HH", data[i + 3:i + 7])
                break
            i += size
    else:
        raise BridgeError("지원하지 않는 MCP 이미지 형식입니다. PNG 또는 JPEG가 필요합니다.")
    if not width or not height or width * height > MAX_PIXELS:
        raise BridgeError("이미지 크기가 잘못되었거나 허용 범위를 초과했습니다.")
    return width, height


def image_blocks(result):
    content = result.get("content", []) if isinstance(result, dict) else []
    if not isinstance(content, list) or any(not isinstance(c, dict) for c in content):
        raise BridgeError("MCP 이미지 응답 형식이 잘못되었습니다.")
    return [c for c in content if c.get("type") == "image"]


def suppress_images(result):
    out = copy.deepcopy(result)
    if isinstance(out, dict) and isinstance(out.get("content"), list):
        out["content"] = [{"type": "image", "omitted": True} if isinstance(c, dict)
                          and c.get("type") == "image" else c for c in out["content"]]
    return out


def save_images(result, *, output=None, directory=None, require_one=False):
    blocks = image_blocks(result)
    if tool_failed(result):
        return suppress_images(result)
    if require_one and len(blocks) != 1:
        raise BridgeError("캡처 도구가 MCP 이미지 한 장을 반환하지 않았습니다. 텍스트를 이미지로 대신하지 않습니다.")
    if not blocks:
        return result
    if len(blocks) > 8 or (output and len(blocks) != 1):
        raise BridgeError("이미지 개수가 허용 범위를 초과했습니다.")
    plans = []
    total = 0
    for block in blocks:
        encoded, mime = block.get("data"), block.get("mimeType")
        if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise BridgeError("이미지 데이터가 없거나 허용 용량을 초과했습니다.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise BridgeError("MCP 이미지 base64 데이터가 손상되었습니다.") from None
        total += len(data)
        if not data or total > MAX_IMAGE_BYTES:
            raise BridgeError("이미지 응답의 총 용량을 초과했습니다.")
        width, height = image_shape(data, mime)
        suffix = ".jpg" if mime == "image/jpeg" else ".png"
        if output:
            path = Path(output).expanduser().absolute()
            if not path.suffix:
                path = path.with_suffix(suffix)
            if path.suffix.lower() not in ((".jpg", ".jpeg") if suffix == ".jpg" else (".png",)):
                raise BridgeError("출력 파일 확장자는 실제 이미지 형식과 같아야 합니다: " + suffix)
        else:
            root = Path(directory) if directory else Path(os.environ.get("LOCALAPPDATA") or
                   Path.home() / ".local/share") / "RemoteNaraeonCLI/captures"
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = root.expanduser().absolute() / ("screen-" + stamp + "-" + uuid.uuid4().hex[:12] + suffix)
        if path.exists() or path.is_symlink():
            raise BridgeError("기존 이미지 파일을 덮어쓰지 않습니다. 새 출력 경로를 지정하세요.")
        plans.append((path, data, {"type": "image", "mimeType": mime, "path": str(path),
                     "bytes": len(data), "width": width, "height": height,
                     "sha256": hashlib.sha256(data).hexdigest(), "media": "MEDIA:" + str(path)}))
    created = []
    try:
        for path, data, _ in plans:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            created.append(path)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    out = copy.deepcopy(result)
    iterator = iter([info for _, _, info in plans])
    out["content"] = [next(iterator) if c.get("type") == "image" else c for c in out["content"]]
    out["images"] = [info for _, _, info in plans]
    return out


def tool_object(result):
    if tool_failed(result):
        return None
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    import json
    for c in result.get("content", []):
        if c.get("type") == "text":
            try:
                value = json.loads(c.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    raise BridgeError("화면 조회에서 구조화된 revision을 받지 못했습니다.")


def capture(client, catalog, *, output=None, directory=None, revision=None):
    if "phone_take_screenshot" not in catalog:
        raise BridgeError("현재 서버에 phone_take_screenshot 도구가 없습니다. 휴대폰 설정 → 내장 휴대폰 조작 설정에서 화면 이미지 전달을 직접 허용한 뒤 tools를 다시 확인하세요. 권한을 자동 변경하지 않았습니다.")
    if revision is None:
        if "phone_read_screen" not in catalog:
            raise BridgeError("최신 화면 revision을 읽을 도구가 없습니다.")
        screen = client.request("tools/call", {"name": "phone_read_screen", "arguments": {}})
        value = tool_object(screen)
        if value is None:
            return suppress_images(screen)
        revision = value.get("revision")
    if not isinstance(revision, str) or not revision or len(revision) > 4096:
        raise BridgeError("유효한 최신 화면 revision이 필요합니다.")
    result = client.request("tools/call", {"name": "phone_take_screenshot", "arguments": {"revision": revision}})
    out = save_images(result, output=output, directory=directory, require_one=True)
    if not tool_failed(result):
        out.update(captured=True, revision=revision)
    return out
