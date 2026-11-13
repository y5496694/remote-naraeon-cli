"""Per-device live screenshot/control engine. No background retries or grant changes.

Frames live only in RAM. Each device has an independent MCP client and non-queuing
operation lock. Screenshot revisions and image IDs are never mixed across devices.
"""
import base64
import binascii
import math
import threading
import time
import uuid

from narae_client import BridgeError, Client, tool_failed
from screen_images import MAX_IMAGE_BYTES, image_blocks, image_shape, tool_object
from secure_store import device_id


class FleetError(BridgeError):
    pass


class Busy(FleetError):
    pass


def checked(result):
    if tool_failed(result):
        # Do not leak arbitrary server content, endpoints, tokens or screen text.
        raise FleetError("기기에서 요청을 거절했습니다. 잠금·원격 선택·허용 앱·화면 권한 및 앱 내 작업 정책을 확인하세요. 자동 재시도하지 않았습니다.")
    return tool_object(result)


def bounded_image(result):
    checked(result)
    blocks = image_blocks(result)
    if len(blocks) != 1:
        raise FleetError("실제 화면 이미지 한 장이 필요합니다.")
    block = blocks[0]
    data = block.get("data")
    if not isinstance(data, str) or len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise FleetError("화면 이미지 용량이 허용 범위를 초과했습니다.")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise FleetError("화면 이미지가 손상되었습니다.") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise FleetError("화면 이미지 용량이 허용 범위를 초과했습니다.")
    width, height = image_shape(raw, block.get("mimeType"))
    return raw, block["mimeType"], width, height


class Device:
    def __init__(self, name, loader, timeout=15, client_factory=Client):
        self.name = device_id(name)
        self.loader, self.timeout, self.client_factory = loader, timeout, client_factory
        self.operation = threading.Lock()
        self.lifecycle = threading.Lock()
        self.epoch = 0
        self.active = False
        self.armed = False
        self.client = None
        self.catalog = {}
        self.frame = None

    def stop(self):
        # Does not wait for a slow in-flight read. Its response will be discarded.
        with self.lifecycle:
            self.epoch += 1
            self.active = self.armed = False
            self.frame = None
        return {"stopped": True, "inflight_may_finish": self.operation.locked(),
                "remote_mode_changed": False}

    def _connect(self):
        if self.client is None:
            profile = self.loader(self.name)
            self.client = self.client_factory(profile, self.timeout)
            self.catalog = {t["name"]: t for t in self.client.tools()}
        return self.client

    def _call(self, name, arguments):
        client = self._connect()
        if name not in self.catalog:
            raise FleetError("현재 기기에서 필요한 도구를 제공하지 않습니다: " + name)
        return client.request("tools/call", {"name": name, "arguments": arguments})

    def run(self, operation, arguments=None):
        if operation == "stop":
            return self.stop()
        if not self.operation.acquire(blocking=False):
            raise Busy("이 기기의 요청이 진행 중입니다. 조작은 대기열에 넣거나 자동 재전송하지 않습니다.")
        with self.lifecycle:
            epoch = self.epoch
        try:
            args = arguments or {}
            if operation == "frame":
                if set(args) - {"start"} or ("start" in args and not isinstance(args["start"], bool)):
                    raise FleetError("잘못된 화면 조회 인수입니다.")
                with self.lifecycle:
                    if args.get("start"):
                        self.active = True
                    if not self.active:
                        raise FleetError("화면 조회가 중지됐습니다. 명시적으로 다시 시작하세요.")
                # Catalog is refreshed on every explicit start (permission updates).
                self._connect()
                if args.get("start"):
                    self.catalog = {t["name"]: t for t in self.client.tools()}
                result = self._capture()
            elif operation == "arm":
                if args:
                    raise FleetError("제어권 확인에는 인수가 없습니다.")
                checked(self._call("narae_take_control", {}))
                with self.lifecycle:
                    if epoch != self.epoch:
                        raise FleetError("요청 중 관제가 중지됐습니다. 재인계하지 않았습니다.")
                    self.armed = True
                    self.frame = None  # Control generation may have changed; observe again.
                result = {"armed": True, "remote_mode_changed": False}
            elif operation == "action":
                result = self._action(args)
            else:
                raise FleetError("지원하지 않는 관제 요청입니다.")
            with self.lifecycle:
                if epoch != self.epoch:
                    self.frame = None
                    raise FleetError("관제가 중지되어 진행 중 응답을 폐기했습니다.")
            return result
        except Busy:
            raise
        except Exception:
            self.stop()
            self.client = None
            self.catalog = {}
            raise
        finally:
            self.operation.release()

    def _capture(self):
        if "phone_take_screenshot" not in self.catalog:
            raise FleetError("이 기기의 화면 이미지 전달을 나래온 설정에서 직접 허용하세요. 권한을 자동 변경하지 않았습니다.")
        screen = checked(self._call("phone_read_screen", {}))
        revision = screen.get("revision")
        if not isinstance(revision, str) or not revision or len(revision) > 4096:
            raise FleetError("유효한 최신 revision이 없습니다.")
        response = self._call("phone_take_screenshot", {"revision": revision})
        metadata = checked(response)
        raw, mime, width, height = bounded_image(response)
        # Metadata must correspond to the requested revision if supplied.
        if metadata.get("revision", revision) != revision:
            raise FleetError("캡처 revision이 일치하지 않습니다.")
        image_id = metadata.get("image_id")
        if image_id is not None and (not isinstance(image_id, str) or len(image_id) > 4096):
            raise FleetError("이미지 식별자가 올바르지 않습니다.")
        frame = {"id": uuid.uuid4().hex, "revision": revision,
                 "image_id": image_id, "width": width, "height": height,
                 "at": time.monotonic(), "nodes": screen.get("nodes", [])}
        if not isinstance(frame["nodes"], list) or any(not isinstance(n, dict) for n in frame["nodes"]):
            raise FleetError("화면 요소 형식이 올바르지 않습니다.")
        with self.lifecycle:
            if not self.active:
                raise FleetError("관제가 중지되어 이미지를 폐기했습니다.")
            self.frame = frame
        return {"device": self.name, "frame_id": frame["id"], "width": width, "height": height,
                "image": "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii"),
                "captured_at": time.time(), "armed": self.armed, "tap_supported": bool(image_id),
                "editable": [{"id": n.get("id"), "label": str(n.get("hint") or n.get("description") or
                              n.get("resource_id") or "입력란")[:100]} for n in frame["nodes"]
                             if n.get("editable") and n.get("enabled", True)],
                "scrollable": [n.get("id") for n in frame["nodes"]
                               if n.get("scrollable") and n.get("enabled", True)]}

    def _action(self, args):
        if set(args) - {"frame_id", "action", "x", "y", "node_id", "text"}:
            raise FleetError("잘못된 조작 인수입니다.")
        with self.lifecycle:
            frame = self.frame
            if not self.active or not self.armed:
                raise FleetError("이 기기의 화면과 제어권을 먼저 확인하세요.")
            if (not frame or args.get("frame_id") != frame["id"] or
                    time.monotonic() - frame["at"] > 10):
                raise FleetError("표시된 화면이 오래되었거나 다른 화면입니다. 다시 보고 조작하세요.")
            # A frame is single-use even if an action times out or is denied.
            self.frame = None
        action = args.get("action")
        allowed = {"tap", "home", "back", "type", "scroll_forward", "scroll_backward"}
        if action not in allowed:
            raise FleetError("지원하지 않는 조작입니다.")
        fields = {"frame_id", "action"}
        params = {"revision": frame["revision"], "action": action}
        if action == "tap":
            fields |= {"x", "y"}
            if not frame["image_id"]:
                raise FleetError("이 앱 버전은 이미지 좌표 터치를 제공하지 않습니다.")
            for key, upper in (("x", frame["width"]), ("y", frame["height"])):
                value = args.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value < upper:
                    raise FleetError("화면 이미지 바깥의 좌표입니다.")
                params[key] = value
            params.update(image_id=frame["image_id"], coordinate_space="image_pixels")
        elif action in {"type", "scroll_forward", "scroll_backward"}:
            fields.add("node_id")
            node_id = args.get("node_id")
            if isinstance(node_id, bool) or not isinstance(node_id, int):
                raise FleetError("현재 화면의 항목을 선택하세요.")
            targets = [n for n in frame["nodes"] if n.get("id") == node_id and n.get("enabled", True)
                       and n.get("editable" if action == "type" else "scrollable")]
            if len(targets) != 1:
                raise FleetError("현재 화면에서 사용할 수 없는 항목입니다.")
            params["node_id"] = node_id
            if action == "type":
                fields.add("text")
                if not isinstance(args.get("text"), str) or len(args["text"]) > 5000:
                    raise FleetError("입력 문장은 5000자 이내입니다.")
                params["text"] = args["text"]
        if set(args) - fields:
            raise FleetError("현재 동작에 맞지 않는 인수입니다.")
        # Discover capability, but never invent extra fields or bypass server policy.
        props = self.catalog.get("phone_act", {}).get("inputSchema", {}).get("properties", {})
        if not set(params) <= set(props):
            raise FleetError("현재 기기의 phone_act 스키마가 이 조작을 지원하지 않습니다.")
        value = checked(self._call("phone_act", params))
        if value.get("accepted") is not True:
            raise FleetError("기기에서 조작을 수락하지 않았습니다. 자동 재전송하지 않았습니다.")
        return {"accepted": True, "verified": False, "message": "요청 수락. 새 화면에서 결과를 확인하세요."}


class Fleet:
    def __init__(self, names, loader, timeout=15, client_factory=Client):
        if not names or len(names) > 12 or len(set(names)) != len(names):
            raise FleetError("서로 다른 기기 1~12대를 선택하세요.")
        self.devices = {name: Device(name, loader, timeout, client_factory) for name in names}

    def run(self, name, operation, arguments=None):
        if name not in self.devices:
            raise FleetError("이번 관제에서 선택하지 않은 기기입니다.")
        return self.devices[name].run(operation, arguments)

    def close(self):
        for device in self.devices.values():
            device.stop()
