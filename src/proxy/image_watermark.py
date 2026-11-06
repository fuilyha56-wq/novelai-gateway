"""Sign image results through LFN while preserving their transport and format."""

import base64
import copy
import io
import json
import logging
import re
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import NoReturn

import httpx
import msgpack
from fastapi import HTTPException

from .config import settings


logger = logging.getLogger("gateway")
STATUS_HEADER = "X-LFN-Watermark"
VERSION_HEADER = "X-LFN-Watermark-Version"
SIGNING_RESPONSE_HEADERS = frozenset({STATUS_HEADER.lower(), VERSION_HEADER.lower()})
_VERSION = "2"
ERROR_CODE = "lfn_image_watermark_failed"
ERROR_MESSAGE = "Image watermark unavailable"
_MAX_IMAGE = 25 * 1024 * 1024
_MAX_SIGNED_IMAGE = _MAX_IMAGE + 128 * 1024
_MAX_FRAME = 64 * 1024 * 1024
_MAX_ARCHIVE = 128 * 1024 * 1024
_IMAGE_FIELDS = ("b64_json", "image", "image_data")
_IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")
_FINALS = {"final", "done", "image_generation.completed"}
_SSE_BOUNDARY = re.compile(rb"\r?\n\r?\n")
_CONTENT_VALIDATORS = {"content-length", "content-encoding", "content-range", "content-md5", "content-digest", "digest", "etag"}
NATIVE_IMAGE_PATHS = frozenset({
    "/ai/generate-image", "/ai/generate-image-stream", "/ai/upscale",
    "/ai/annotate-image", "/ai/augment-image", "/oa/v1/images/generations",
    "/oa/v1/images/generations/stream", "/oa/v1/images/edits",
})

_client: httpx.AsyncClient | None = None


class ImageSigningError(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=502, detail=ERROR_MESSAGE)


def is_configured() -> bool:
    return bool(settings.lfn_watermark_signer_url or settings.lfn_watermark_signer_token or settings.lfn_watermark_signer_token_file)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=1.5), follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4), trust_env=False,
        )
    return _client


async def close_watermark_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def without_body_validators(headers: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _CONTENT_VALIDATORS}


def native_stream_format(path: str, content_type: str) -> str | None:
    path = path.rstrip("/")
    if path not in NATIVE_IMAGE_PATHS:
        return None
    if "event-stream" in content_type:
        return "sse"
    if "ndjson" in content_type:
        return "ndjson"
    if "msgpack" in content_type:
        return "msgpack"
    if "octet-stream" in content_type and path.endswith(("-stream", "/stream")):
        return "auto"
    return None


class ImageWatermark:
    def __init__(self, model: str):
        self.request_id = uuid.uuid4().hex
        normalized = re.sub(r"[^A-Za-z0-9._:-]", "-", model).lstrip("._:-")
        self.model = normalized[:128] or "novelai"
        self.enabled = is_configured()
        self.status = "skipped"
        self._index = 0
        self._token: str | None = None
        self._finished = False
        self._upstream_error = False

    def _credentials(self) -> tuple[str, str]:
        if self._token is None:
            token_file = settings.lfn_watermark_signer_token_file.strip()
            if token_file:
                with Path(token_file).open(encoding="utf-8") as source:
                    token = source.read(4096).strip()
            else:
                token = settings.lfn_watermark_signer_token.strip()
            self._token = token
        url = httpx.URL(settings.lfn_watermark_signer_url.strip())
        if (not re.fullmatch(r"[\x21-\x7e]{32,512}", self._token) or url.scheme not in {"http", "https"}
                or not url.host or url.userinfo or url.query or url.fragment):
            raise ValueError("invalid signer configuration")
        return str(url), self._token

    def headers(self, *, streaming: bool = False) -> dict[str, str]:
        if not self.enabled:
            return {}
        return {STATUS_HEADER: "streaming" if streaming else self.status, VERSION_HEADER: _VERSION}

    def fail(self) -> NoReturn:
        self.status = "failed"
        raise ImageSigningError()

    def finish(self) -> None:
        if self.enabled and not self._finished:
            self._finished = True
            logger.info("Image watermark request=%s model=%s status=%s images=%d", self.request_id, self.model, self.status, self._index)

    async def image(self, data: bytes) -> bytes:
        if not self.enabled:
            return data
        mime = image_mime(data)
        if mime is None or len(data) > _MAX_IMAGE:
            self.fail()
        self._index += 1
        try:
            url, token = self._credentials()
        except (OSError, ValueError, httpx.InvalidURL):
            self.fail()
        headers = {
            "Authorization": f"Bearer {token}", "Content-Type": mime,
            "X-LFN-Watermark-Model": self.model,
            "X-LFN-Watermark-Request-Id": self.request_id,
            "X-LFN-Watermark-Image-Id": f"{self.request_id}-{self._index}",
        }
        for _ in range(2):
            try:
                async with _get_client().stream("POST", url, content=data, headers=headers) as response:
                    state = response.headers.get(STATUS_HEADER)
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    if (response.status_code != 200 or state not in {"embedded", "reused"}
                            or response.headers.get(VERSION_HEADER) != _VERSION or media_type != mime):
                        continue
                    signed = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(signed) + len(chunk) > _MAX_SIGNED_IMAGE:
                            break
                        signed.extend(chunk)
                    else:
                        if image_mime(signed) == mime and (state == "reused" or signed != data):
                            if state == "embedded" or self.status != "embedded":
                                self.status = state
                            return bytes(signed)
            except (httpx.HTTPError, ValueError):
                pass
        self.fail()

    async def _value(self, value):
        if isinstance(value, bytes):
            return await self.image(value)
        if not isinstance(value, str) or len(value) > _MAX_IMAGE * 4 // 3 + 128:
            self.fail()
        prefix, encoded = "", value
        if value.lower().startswith("data:image/"):
            prefix, separator, encoded = value.partition(",")
            if not separator or not prefix.lower().endswith(";base64"):
                self.fail()
            prefix += separator
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError):
            self.fail()
        signed = await self.image(raw)
        return value if signed == raw else prefix + base64.b64encode(signed).decode("ascii")

    async def _fields(self, payload: dict | list) -> bool:
        changed = False
        # Some envelopes repeat the same image in `data` and at the top level.
        marked = {}

        async def mark(value):
            if value is None or value == "" or value == b"":
                return value
            if not isinstance(value, (str, bytes)):
                self.fail()
            if value not in marked:
                marked[value] = await self._value(value)
            return marked[value]

        async def mark_fields(item: dict) -> None:
            nonlocal changed
            # The gateway can serve its own signed URLs, but cannot attest an upstream URL.
            if item.get("url"):
                self.fail()
            for field in _IMAGE_FIELDS:
                value = item.get(field)
                updated = await mark(value)
                if updated != value:
                    item[field] = updated
                    changed = True

        async def mark_items(items: list) -> None:
            nonlocal changed
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    await mark_fields(item)
                else:
                    updated = await mark(item)
                    if updated != item:
                        items[index] = updated
                        changed = True

        if isinstance(payload, list):
            await mark_items(payload)
        else:
            for key in ("data", "images"):
                items = payload.get(key)
                if isinstance(items, list):
                    await mark_items(items)
                elif isinstance(items, dict):
                    await mark_fields(items)
                elif isinstance(items, (str, bytes)):
                    updated = await mark(items)
                    if updated != items:
                        payload[key] = updated
                        changed = True
            await mark_fields(payload)
        return changed

    async def _zip(self, data: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            members = source.infolist()
            if len(members) > 1024 or sum(member.file_size for member in members) > _MAX_ARCHIVE:
                self.fail()
            changed = False
            with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as output:
                with zipfile.ZipFile(output, mode="w") as target:
                    target.comment = source.comment
                    for member in members:
                        raw = source.read(member)
                        signed = await self.image(raw) if member.filename.lower().endswith(_IMAGE_SUFFIXES) or image_mime(raw) else raw
                        changed = changed or signed != raw
                        target.writestr(copy.copy(member), signed)
                if not changed:
                    return data
                output.seek(0)
                return output.read()

    async def envelope(self, data: bytes, content_type: str) -> bytes:
        if not self.enabled:
            return data
        if len(data) > _MAX_ARCHIVE:
            self.fail()
        before = self._index
        try:
            if image_mime(data) is not None:
                return await self.image(data)
            # The legacy raw format can carry upstream JSON with a ZIP MIME type.
            if data.startswith(b"PK\x03\x04"):
                data = await self._zip(data)
            elif data.lstrip().startswith((b"{", b"[")) or "json" in content_type:
                payload = json.loads(data)
                if isinstance(payload, (dict, list)) and await self._fields(payload):
                    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            elif "zip" in content_type:
                data = await self._zip(data)
        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, UnicodeError):
            self.fail()
        if self._index == before:
            self.fail()
        return data

    async def _frame_payload(self, payload, event: str = "") -> bool:
        if not isinstance(payload, dict):
            self.fail()
        raw_kind = payload.get("event_type", payload.get("type", event))
        kind = raw_kind.lower() if isinstance(raw_kind, str) else ""
        if kind == "error" or event == "error" or payload.get("error"):
            self._upstream_error = True
            return False
        if kind not in _FINALS and event not in _FINALS:
            return False
        before = self._index
        changed = await self._fields(payload)
        if before == self._index and not (before and (kind == "done" or event == "done")):
            self.fail()
        return changed

    async def _frame(self, data: bytes, kind: str) -> bytes:
        try:
            if kind == "msgpack":
                payload = msgpack.unpackb(data[4:], raw=False, strict_map_key=False)
                if await self._frame_payload(payload):
                    return _msgpack_frame(payload)
            elif kind == "ndjson":
                if not data.strip():
                    return data
                payload = json.loads(data)
                if await self._frame_payload(payload):
                    return _json_bytes(payload) + _line_ending(data)
            else:
                lines = data.splitlines(keepends=True)
                values = [line[5:].strip() for line in lines if line.startswith(b"data:")]
                if not values:
                    return data
                if values == [b"[DONE]"]:
                    self._require_final()
                    return data
                event = next((line[6:].strip().decode("ascii").lower() for line in lines if line.startswith(b"event:")), "")
                if event == "error":
                    self._upstream_error = True
                    return data
                payload = json.loads(b"\n".join(values))
                if await self._frame_payload(payload, event):
                    first = next(index for index, line in enumerate(lines) if line.startswith(b"data:"))
                    lines[first] = b"data: " + _json_bytes(payload) + _line_ending(lines[first])
                    return b"".join(line for index, line in enumerate(lines) if index == first or not line.startswith(b"data:"))
        except (ValueError, TypeError, OverflowError, UnicodeError, msgpack.UnpackException):
            self.fail()
        return data

    def _require_final(self) -> None:
        if not self._index and not self._upstream_error:
            self.fail()

    async def stream(self, chunks: AsyncIterable[bytes], kind: str, *, report_error: bool = False) -> AsyncIterator[bytes]:
        if not self.enabled:
            async for chunk in chunks:
                yield chunk
            return
        pending = bytearray()
        scanning_from = 0
        try:
            async for chunk in chunks:
                pending.extend(chunk)
                if kind == "auto" and len(pending) >= 12:
                    if pending.startswith(b"PK\x03\x04") or image_mime(bytes(pending[:12])):
                        kind = "binary"
                    elif pending.startswith((b"data:", b"event:", b"id:", b"retry:", b":")):
                        kind = "sse"
                    elif pending.startswith(b"{"):
                        kind = "ndjson"
                    else:
                        kind = "msgpack"
                if kind in {"binary", "auto"}:
                    if len(pending) > _MAX_ARCHIVE:
                        self.fail()
                    continue
                while pending:
                    end = None
                    if kind == "msgpack" and len(pending) >= 4:
                        size = int.from_bytes(pending[:4], "big")
                        end = size + 4 if size else 0
                    elif kind == "ndjson":
                        boundary = pending.find(b"\n", scanning_from)
                        end = boundary + 1 if boundary >= 0 else None
                    elif kind == "sse":
                        boundary = _SSE_BOUNDARY.search(pending, scanning_from)
                        end = boundary.end() if boundary else None
                    if end == 0 or (end is not None and end > _MAX_FRAME) or (end is None and len(pending) > _MAX_FRAME):
                        self.fail()
                    if end is None or end > len(pending):
                        scanning_from = max(0, len(pending) - 3)
                        break
                    frame = bytes(pending[:end])
                    del pending[:end]
                    scanning_from = 0
                    yield await self._frame(frame, kind)
            if pending:
                if kind == "binary":
                    yield await self.envelope(bytes(pending), "")
                elif kind in {"auto", "msgpack"}:
                    self.fail()
                else:
                    yield await self._frame(bytes(pending), kind)
            self._require_final()
        except ImageSigningError:
            if not report_error:
                raise
            yield _stream_error(kind)
        finally:
            self.finish()


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _msgpack_frame(payload: dict) -> bytes:
    encoded = msgpack.packb(payload, use_bin_type=True)
    return len(encoded).to_bytes(4, "big") + encoded


def _line_ending(data: bytes) -> bytes:
    return b"\r\n" if data.endswith(b"\r\n") else b"\n" if data.endswith(b"\n") else b""


def _stream_error(kind: str) -> bytes:
    error = {"event_type": "error", "type": "error", "error": ERROR_MESSAGE, "message": ERROR_MESSAGE, "code": ERROR_CODE}
    if kind == "sse":
        return b"event: error\ndata: " + _json_bytes(error) + b"\n\ndata: [DONE]\n\n"
    if kind == "ndjson":
        return _json_bytes(error) + b"\n"
    return _msgpack_frame(error)


async def sign_buffered(data: bytes, content_type: str, model: str) -> tuple[bytes, dict[str, str]]:
    signer = ImageWatermark(model)
    try:
        result = await signer.envelope(data, content_type)
        return result, signer.headers()
    finally:
        signer.finish()
