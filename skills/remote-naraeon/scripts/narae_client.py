#!/usr/bin/env python3
"""MCP transport and legacy profile importer used by Remote Naraeon CLI.

Adapted from the user's installed narae-phone client. This module has no CLI.
"""

import ctypes
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path


class BridgeError(Exception):
    pass


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def load_profile():
    url, token = os.environ.get("NARAE_MCP_URL"), os.environ.get("NARAE_MCP_TOKEN")
    if url and token:
        return {"url": url, "token": token}
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = root / "secrets" / "narae-phone.dpapi"
    if not path.is_file():
        raise BridgeError("Encrypted Narae profile missing. Configure NARAE_MCP_URL and NARAE_MCP_TOKEN without printing them.")
    if os.name != "nt":
        raise BridgeError("This credential profile requires its original Windows user. Environment credentials are supported on other systems.")
    data = path.read_bytes()
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise BridgeError("Cannot decrypt Narae profile under this Windows user.")
    try:
        profile = json.loads(ctypes.string_at(target.pbData, target.cbData).decode("utf-8"))
    finally:
        kernel.LocalFree(target.pbData)
    if url:
        profile["url"] = url
    if token:
        profile["token"] = token
    return profile


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("MCP redirected the request; credentials were not forwarded. Check the configured endpoint.")


def read_sse(response, request_id):
    parts = []
    size = 0
    for raw_line in response:
        size += len(raw_line)
        if size > 8 * 1024 * 1024:
            raise BridgeError("MCP SSE response exceeded 8 MiB.")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            parts.append(line[5:].lstrip(" "))
        elif not line and parts:
            message = json.loads("\n".join(parts))
            parts = []
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
    if parts:
        message = json.loads("\n".join(parts))
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise BridgeError("SSE response ended without a matching JSON-RPC response. Do not repeat a phone action before checking its result.")


class Client:
    def __init__(self, profile, timeout):
        self.profile = profile
        if urllib.parse.urlsplit(profile.get("url", "")).scheme != "https":
            raise BridgeError("Narae MCP requires an HTTPS endpoint.")
        if not profile.get("token"):
            raise BridgeError("Narae MCP token is missing.")
        self.timeout = timeout
        self.sequence = 0
        self.headers = {"Authorization": "Bearer " + profile["token"], "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.opener = urllib.request.build_opener(NoRedirect())
        self.initialized = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "remote-naraeon-cli", "version": "0.4.0"}})
        if not isinstance(self.initialized, dict) or not isinstance(self.initialized.get("protocolVersion"), str):
            raise BridgeError("MCP initialization returned an invalid protocol version.")
        self.headers["MCP-Protocol-Version"] = self.initialized["protocolVersion"]
        self.request("notifications/initialized", {}, notification=True)

    def request(self, method, params, notification=False):
        self.sequence += 1
        request_id = None if notification else self.sequence
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = request_id
        request = urllib.request.Request(self.profile["url"], json.dumps(payload, ensure_ascii=False).encode("utf-8"), self.headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.headers.get("Mcp-Session-Id"):
                    self.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                if notification:
                    return None
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    message = read_sse(response, request_id)
                else:
                    data = response.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024:
                        raise BridgeError("MCP response exceeded 8 MiB.")
                    message = json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hints = {401: "Authentication rejected; check the saved token.", 403: "Server denied access.", 404: "Endpoint or MCP session unavailable."}
            raise BridgeError(f"HTTP {exc.code}: {hints.get(exc.code, 'MCP request failed.')} No request was retried.") from None
        except (TimeoutError, socket.timeout):
            raise BridgeError("MCP request timed out. Outcome may be unknown; read status/screen before repeating an action.") from None
        except urllib.error.URLError as exc:
            raise BridgeError(f"MCP network connection failed ({type(exc.reason).__name__}). No request was retried.") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise BridgeError("MCP response is not a JSON-RPC 2.0 object.")
        if message.get("id") != request_id:
            raise BridgeError("MCP response ID mismatch.")
        if "error" in message:
            details = message["error"]
            code = details.get("code") if isinstance(details, dict) else None
            raise BridgeError(f"MCP JSON-RPC error ({code if isinstance(code, int) else 'unknown'}). No request was retried.")
        if "result" not in message:
            raise BridgeError("MCP response has no result.")
        return message["result"]

    def tools(self):
        collected, cursor, seen = [], None, set()
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                raise BridgeError("MCP returned an invalid tool catalog.")
            if any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in page["tools"]):
                raise BridgeError("MCP returned an invalid tool entry.")
            collected.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return collected
            if not isinstance(cursor, str):
                raise BridgeError("MCP pagination cursor is invalid.")
            if cursor in seen:
                raise BridgeError("MCP repeated a tools pagination cursor.")
            seen.add(cursor)


def scrub(value, profile):
    if isinstance(value, str):
        for secret in (profile.get("token"), profile.get("url")):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [scrub(item, profile) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item, profile) for key, item in value.items()}
    return value


def tool_failed(result):
    if not isinstance(result, dict):
        raise BridgeError("MCP tool result is not an object.")
    if result.get("isError"):
        return True
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and (structured.get("ok") is False or structured.get("success") is False):
        return True
    content = result.get("content", [])
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise BridgeError("MCP tool content is invalid.")
    for block in content:
        if block.get("type") == "text":
            try:
                nested = json.loads(block.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(nested, dict) and (nested.get("ok") is False or nested.get("success") is False):
                return True
    return False


