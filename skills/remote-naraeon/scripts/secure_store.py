"""Current-user Windows DPAPI credential storage; no application PIN."""

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path


class StoreError(Exception):
    pass


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, *, decrypt=False):
    if os.name != "nt":
        raise StoreError("이 저장소는 Windows의 현재 사용자 계정에서만 사용할 수 있습니다.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise StoreError("Windows 보안 저장소를 열 수 없습니다. 등록한 Windows 계정인지 확인하세요."
                         if decrypt else "Windows 보안 저장에 실패했습니다.")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def default_directory():
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise StoreError("LOCALAPPDATA가 없습니다. --data-dir로 저장 폴더를 지정하세요.")
    return Path(root) / "RemoteNaraeonCLI"


def device_id(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", value):
        raise StoreError("기기 이름은 영문 소문자·숫자·_·-로 1~40자입니다.")
    if value in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(?:com|lpt)[0-9]", value):
        raise StoreError("Windows 예약 이름은 기기 이름으로 쓸 수 없습니다.")
    return value


def registered_devices(directory=None):
    root = Path(directory) if directory else default_directory()
    names = ["default"] if (root / "connection-v2.dpapi").is_file() else []
    for path in sorted((root / "devices").glob("*.dpapi")):
        try:
            name = device_id(path.stem)
        except StoreError:
            continue
        if name != "default" and path.is_file() and not path.is_symlink():
            names.append(name)
    return names


class Store:
    def __init__(self, directory=None, device="default"):
        root = Path(directory) if directory else default_directory()
        self.device = device_id(device)
        self.directory = root if device == "default" else root / "devices"
        self.path = self.directory / ("connection-v2.dpapi" if device == "default" else device + ".dpapi")

    def save(self, profile, *, replace=False):
        if self.path.exists() and not replace:
            raise StoreError("이미 등록되어 있습니다. 변경하려면 register --replace를 사용하세요.")
        record = {"format": "remote-naraeon-cli", "version": 2, "profile": profile}
        encrypted = dpapi(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".narae-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def load(self):
        try:
            record = json.loads(dpapi(self.path.read_bytes(), decrypt=True))
            if (not isinstance(record, dict) or record.get("format") != "remote-naraeon-cli"
                    or record.get("version") != 2 or not isinstance(record.get("profile"), dict)):
                raise ValueError("format")
            return record["profile"]
        except FileNotFoundError:
            raise StoreError("CLI에 등록된 연결정보가 없습니다.") from None
        except (ValueError, KeyError, TypeError):
            raise StoreError("보안 저장 파일이 올바르지 않습니다. 연결정보를 다시 등록하세요.") from None
