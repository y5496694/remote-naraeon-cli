#!/usr/bin/env python3
"""원격 나래온 CLI — 등록된 연결정보를 바로 불러와 나래온 MCP 호출."""

import argparse
import getpass
import json
import math
import sys
import urllib.parse
from pathlib import Path

from narae_client import BridgeError, Client, load_profile, tool_failed
from secure_store import Store, StoreError, registered_devices
from screen_images import capture, save_images, suppress_images

VERSION = "0.4.0"
SECRET_FIELDS = {"pin", "token", "access_token", "refreshtoken", "refresh_token", "password",
                 "secret", "client_secret", "authorization", "api_key", "apikey"}


def redact(value, profile=None):
    profile = profile or {}
    if isinstance(value, dict):
        return {key: ("[redacted]" if key.lower() in SECRET_FIELDS else redact(item, profile))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, profile) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(redact(parsed, profile), ensure_ascii=False)
            except ValueError:
                pass
        for key in ("token", "url"):
            if profile.get(key):
                value = value.replace(profile[key], "[redacted]")
    return value


def emit(value, profile=None, *, error=False):
    print(json.dumps(redact(value, profile), ensure_ascii=False, indent=2),
          file=sys.stderr if error else sys.stdout)


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise StoreError("연결정보 형식이 올바르지 않습니다.")
    url, token = profile.get("url"), profile.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not token.strip():
        raise StoreError("MCP 주소와 Bearer 토큰이 필요합니다.")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or any(c.isspace() for c in url)):
        raise StoreError("사용자정보나 fragment가 없는 올바른 HTTPS MCP 주소를 입력하세요.")
    if any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise StoreError("토큰에 사용할 수 없는 문자가 포함되어 있습니다.")
    return {"url": url, "token": token}


def hidden(prompt):
    if not sys.stdin.isatty():
        raise StoreError("새 주소·토큰 입력은 로컬 터미널에서 실행하세요. 기존 정보는 --import-existing으로 가져올 수 있습니다.")
    return getpass.getpass(prompt)


def resolve_profile(store):
    if store.path.exists():
        return validate_profile(store.load()), "cli-store"
    if store.device != "default":
        raise StoreError("선택한 기기에 등록된 연결정보가 없습니다. 다른 기기로 자동 대체하지 않습니다.")
    return validate_profile(load_profile()), "existing-narae-phone"


def arguments():
    parser = argparse.ArgumentParser(prog="원격 나래온 CLI", description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--data-dir", help="보안 저장 폴더. 기본: %%LOCALAPPDATA%%/RemoteNaraeonCLI")
    parser.add_argument("--device", default="default", help="사용할 기기 이름. 생략하면 기존 default 연결 유지")
    parser.add_argument("--timeout", type=float, default=40, help="요청별 제한 시간(초). 자동 재시도하지 않음")
    sub = parser.add_subparsers(dest="command", required=True)
    registration = sub.add_parser("register", help="연결정보 암호화 등록. PC 잠금 PIN 없음")
    registration.add_argument("--import-existing", action="store_true", help="기존 나래온 암호화 연결정보 가져오기")
    registration.add_argument("--replace", action="store_true", help="CLI 연결정보 교체")
    sub.add_parser("status", help="사용 가능한 로컬 연결정보 확인(네트워크 요청 없음)")
    sub.add_parser("devices", help="기기 이름만 조회. 주소·토큰은 출력하지 않음")
    monitor = sub.add_parser("monitor", help="여러 기기의 로컬 화면 관제창 열기")
    monitor.add_argument("--devices", nargs="+", help="선택할 기기 이름. 생략 시 등록된 모든 기기")
    monitor.add_argument("--port", type=int, default=0, help="로컬 포트. 0은 빈 포트 자동 선택")
    monitor.add_argument("--interval", type=float, default=2, help="캡처 완료 후 갱신 대기 1~30초")
    monitor.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 없이 URL 반환")
    sub.add_parser("info", help="실제 MCP 서버 연결·초기화 확인")
    sub.add_parser("phone-status", help="나래 상태와 휴대폰 연결 상태 조회")
    catalog = sub.add_parser("tools", help="실제 서버 도구 목록과 스키마 조회")
    catalog.add_argument("--name", help="상세 스키마를 볼 도구 이름")
    shot = sub.add_parser("screenshot", help="최신 화면 revision으로 1회 캡처하고 실제 이미지 파일 반환")
    shot.add_argument("--output", "-o", help="저장할 .jpg/.png 경로. 기존 파일은 덮어쓰지 않음")
    shot.add_argument("--image-dir", help="자동 이름 이미지 저장 폴더")
    shot.add_argument("--revision", help="이미 읽은 최신 revision. 생략하면 화면 읽기 1회")
    call = sub.add_parser("call", help="실제 목록에 있는 MCP 도구 한 번 호출")
    call.add_argument("--image-dir", help="반환된 이미지 저장 폴더. 기본은 개인 captures 폴더")
    call.add_argument("--inline-images", action="store_true", help="호스트 연동용: 파일 대신 원본 MCP image 블록 출력")
    call.add_argument("name")
    source = call.add_mutually_exclusive_group()
    source.add_argument("--args", default="{}", help="JSON 객체. 민감정보·한글은 --args-file 권장")
    source.add_argument("--args-file", help="UTF-8 JSON 파일. '-'이면 표준입력")
    return parser.parse_args()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = arguments()
    profile = {}
    try:
        if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300:
            raise StoreError("제한 시간은 1~300초입니다.")
        store = Store(args.data_dir, args.device)
        if args.command in {"devices", "monitor"}:
            names = registered_devices(args.data_dir)
            if "default" not in names:
                try:
                    resolve_profile(Store(args.data_dir))
                    names.insert(0, "default")
                except (StoreError, BridgeError, OSError, ValueError):
                    pass
            if args.command == "devices":
                emit({"devices": names, "network_verified": False})
                return 0
            selected = args.devices or ([args.device] if args.device != "default" else names)
            if any(name not in names for name in selected):
                raise StoreError("선택한 기기에 등록된 연결정보가 없습니다.")
            from fleet import Fleet
            from monitor import serve
            fleet = Fleet(selected, lambda name: resolve_profile(Store(args.data_dir, name))[0], args.timeout)
            serve(fleet, args.port, args.interval, open_browser=not args.no_browser)
            return 0
        if args.command == "register":
            if store.path.exists() and not args.replace:
                raise StoreError("이미 등록되어 있습니다. 변경하려면 register --replace를 사용하세요.")
            if args.import_existing:
                profile = validate_profile(load_profile())
            else:
                profile = validate_profile({"url": hidden("MCP HTTPS 주소(숨김): ").strip(),
                                            "token": hidden("Bearer 토큰(숨김): ").strip()})
            store.save(profile, replace=args.replace)
            emit({"ok": True, "registered": True, "credential_source": "cli-store",
                  "network_verified": False})
            return 0
        if args.command == "status":
            profile, source = resolve_profile(store)
            emit({"available": True, "device": args.device, "registered_in_cli": store.path.is_file(), "credential_source": source})
            return 0
        params = {}
        if args.command == "call":
            raw = ((sys.stdin.buffer.read().decode("utf-8-sig") if args.args_file == "-"
                    else Path(args.args_file).read_text(encoding="utf-8-sig"))
                   if args.args_file else args.args)
            params = json.loads(raw)
            if not isinstance(params, dict):
                raise StoreError("도구 인수는 JSON 객체여야 합니다.")
        profile, _ = resolve_profile(store)
        client = Client(profile, args.timeout)
        failed = False
        if args.command == "info":
            result = dict(client.initialized)
            result["sessionful"] = "Mcp-Session-Id" in client.headers
        else:
            tools = client.tools()
            by_name = {tool["name"]: tool for tool in tools}
            if args.command == "tools":
                if args.name and args.name not in by_name:
                    raise BridgeError("실제 서버 목록에 없는 도구입니다.")
                result = by_name[args.name] if args.name else [
                    {"name": tool["name"], "description": tool.get("description", "")} for tool in tools]
            elif args.command == "screenshot":
                result = capture(client, by_name, output=args.output, directory=args.image_dir, revision=args.revision)
                failed = tool_failed(result)
            elif args.command == "phone-status":
                names = ("narae_status", "phone_connection_status")
                if any(name not in by_name for name in names):
                    raise BridgeError("서버에 휴대폰 연결 조회 도구가 없습니다. tools로 목록을 확인하세요.")
                result = {}
                for name in names:
                    result[name] = client.request("tools/call", {"name": name, "arguments": {}})
                    failed = tool_failed(result[name]) or failed
            else:
                if args.name not in by_name:
                    raise BridgeError("실제 서버 목록에 없는 도구입니다.")
                result = client.request("tools/call", {"name": args.name, "arguments": params})
                failed = tool_failed(result)
                if failed:
                    result = suppress_images(result)
                elif not args.inline_images:
                    result = save_images(result, directory=args.image_dir)
        emit(result, profile)
        return 2 if failed else 0
    except (StoreError, BridgeError, OSError, ValueError, KeyError, TypeError) as exc:
        emit({"ok": False, "error": str(exc)}, profile, error=True)
        return 1
    except (KeyboardInterrupt, EOFError):
        emit({"ok": False, "error": "입력이 취소되었습니다."}, error=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
