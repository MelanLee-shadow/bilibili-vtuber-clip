"""Small read-only CloudDrive2 gRPC-Web client, with no provider-direct API.

Wire fields are from CloudDrive.proto in iLay1678/python-clouddrive2 (client
source, option version 2.1.0.0-beta). The official API entry is
https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html .
Unknown/older server fields never imply upload completion. In particular,
absence from the upload queue is not a Finished event.
"""
from __future__ import annotations

import struct
import stat
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator


class CD2Error(RuntimeError):
    pass


# Only the read-only RPC subset used here. No auth/login, cancel, restart,
# delete, provider download or queue mutation methods are implemented.
UPLOAD = {1: ("key", "s"), 2: ("destPath", "s"), 3: ("size", "v"),
          4: ("transferedBytes", "v"), 5: ("status", "s"),
          6: ("errorMessage", "s"), 8: ("statusEnum", "v")}
FILE = {1: ("id", "s"), 2: ("name", "s"), 3: ("fullPathName", "s"),
        4: ("size", "v"), 30: ("isDirectory", "b"), 34: ("isCloudFile", "b")}
TASKS = {4: ("uploadFileStatusChanges", UPLOAD)}
PUSH = {2: ("transferTaskStatus", TASKS)}
UPLOAD_LIST = {1: ("totalCount", "v"), 2: ("uploadFiles", UPLOAD)}
SUBFILES = {1: ("subFiles", FILE)}


def varint(value: int) -> bytes:
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def request_bytes(fields: dict[int, Any]) -> bytes:
    out = bytearray()
    for key, value in fields.items():
        if isinstance(value, str):
            data = value.encode("utf-8")
            out += varint((key << 3) | 2) + varint(len(data)) + data
        else:
            out += varint(key << 3) + varint(int(value))
    return bytes(out)


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise CD2Error("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise CD2Error("oversized protobuf varint")


def decode(data: bytes, schema: dict[int, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        key, wire = tag >> 3, tag & 7
        if wire == 0:
            raw, offset = _varint(data, offset)
        elif wire == 2:
            size, offset = _varint(data, offset)
            raw = data[offset:offset + size]
            offset += size
            if len(raw) != size:
                raise CD2Error("truncated protobuf field")
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
            continue
        else:
            raise CD2Error("unsupported protobuf wire type")
        if key not in schema:
            continue
        name, kind = schema[key]
        if isinstance(kind, dict):
            result.setdefault(name, []).append(decode(raw, kind))
        elif kind == "s":
            result[name] = raw.decode("utf-8")
        elif kind == "b":
            result[name] = bool(raw)
        else:
            result[name] = raw
    return result


def _exact(response: Any, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        part = response.read(count - len(chunks))
        if not part:
            break
        chunks.extend(part)
    return bytes(chunks)


class GrpcWeb:
    def __init__(self, endpoint: str, token_file: Path, timeout: float = 40):
        if not endpoint.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
            raise CD2Error("CD2 endpoint requires loopback HTTP or HTTPS")
        self.endpoint, self.token_file, self.timeout = endpoint.rstrip("/"), token_file, timeout

    def stream(self, method: str, fields: dict[int, Any], schema: dict[int, Any],
               *, timeout: float | None = None,
               on_open: Callable[[], None] | None = None) -> Iterator[dict[str, Any]]:
        # This file is supplied on the server by its operator. Never persisted
        # in state, subprocess argv, diagnostic text or RPC receipts.
        info = self.token_file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise CD2Error("CD2 token file must be private and regular")
        token = self.token_file.read_text(encoding="utf-8").strip()
        if not token or "\n" in token or "\r" in token:
            raise CD2Error("CD2 token is unavailable")
        payload = request_bytes(fields)
        request = urllib.request.Request(
            f"{self.endpoint}/clouddrive.CloudDriveFileSrv/{method}",
            data=b"\x00" + struct.pack(">I", len(payload)) + payload,
            headers={"Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1",
                     "Authorization": "Bearer " + token}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            if on_open is not None:
                on_open()
            trailers_seen = False
            while True:
                header = _exact(response, 5)
                if not header:
                    break
                if len(header) != 5:
                    raise CD2Error("truncated grpc-web header")
                flags, length = header[0], struct.unpack(">I", header[1:])[0]
                if length > 32 * 1024 * 1024:
                    raise CD2Error("oversized grpc-web message")
                data = _exact(response, length)
                if len(data) != length:
                    raise CD2Error("truncated grpc-web message")
                if flags == 128:
                    trailers = dict(line.split(":", 1) for line in
                                    data.decode("ascii").split("\r\n") if ":" in line)
                    if trailers.get("grpc-status", "").strip() != "0":
                        raise CD2Error("CD2 RPC did not succeed")
                    trailers_seen = True
                    break
                if flags != 0:
                    raise CD2Error("compressed/unknown grpc-web frame")
                yield decode(data, schema)
            if not trailers_seen:
                raise CD2Error("CD2 RPC lacks successful trailers")


class CD2Status:
    def __init__(self, transport: GrpcWeb):
        self.transport = transport

    @staticmethod
    def _upload_defaults(row: dict[str, Any]) -> dict[str, Any]:
        # proto3 omits zero-length sidecar sizes and empty error strings.
        # The default enum is Idle (0), never Finished (5).
        return {"size": 0, "statusEnum": 0, "errorMessage": "", **row}

    def upload_list(self, filename: str) -> list[dict[str, Any]]:
        replies = list(self.transport.stream("GetUploadFileList", {1: True, 4: filename}, UPLOAD_LIST))
        if len(replies) != 1:
            raise CD2Error("invalid CD2 upload list reply")
        rows = replies[0].get("uploadFiles", [])
        if replies[0].get("totalCount", 0) != len(rows):
            raise CD2Error("CD2 upload list was incomplete")
        return [self._upload_defaults(row) for row in rows]

    def push_uploads(self, *, on_open: Callable[[], None] | None = None) -> Iterator[dict[str, Any]]:
        for reply in self.transport.stream("PushMessage", {}, PUSH, timeout=65, on_open=on_open):
            for tasks in reply.get("transferTaskStatus", []):
                for row in tasks.get("uploadFileStatusChanges", []):
                    yield self._upload_defaults(row)

    def refresh(self, path: str) -> dict[str, Any]:
        parent = str(Path(path).parent)
        rows = []
        for reply in self.transport.stream("GetSubFiles", {1: parent, 2: True}, SUBFILES):
            rows.extend(row for row in reply.get("subFiles", []) if row.get("fullPathName") == path)
        if len(rows) != 1:
            raise CD2Error("refreshed CD2 object is missing or ambiguous")
        row = rows[0]
        # proto3 omits false booleans. Missing isCloudFile stays false and
        # therefore fails the delete gate; missing isDirectory means false.
        row.setdefault("isDirectory", False)
        row.setdefault("isCloudFile", False)
        row.setdefault("size", 0)
        return row
