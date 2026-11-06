"""ImageX and VOD uploads reconstructed from the saved Dreamina SDK.

US image direct uploads and VOD classic-part uploads were live verified on
2026-10-25. VOD remains opt-in: a committed asset does not prove that every
reference-generation workflow accepts it. No session cookies reach upload hosts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

DIRECT_LIMIT = 3 * 1024 * 1024
DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # Local memory/transfer policy, not a platform limit.
UPLOAD_EVIDENCE = "upstream-commit-confirmed"
_ENDPOINTS = {
    "US": ("https://imagex16-normal-us-ttp.capcutapi.us", "https://vod16-normal-us-ttp.capcutapi.us", "us-east-1"),
    "SG": ("https://imagex-normal-sg.capcutapi.com", "https://vod-normal-sg.capcutapi.com", "ap-singapore-1"),
}


class UploadError(RuntimeError):
    """An upload failed; messages intentionally omit credentials and signed URLs."""

    def __init__(self, stage: str, message: str, code: str | int | None = None):
        self.stage, self.code = stage, code
        super().__init__(f"{stage}: {message}" + (f" (code={code})" if code is not None else ""))


def crc32_hex(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xffffffff:08x}"


def canonical_query(params: Mapping[str, Any]) -> str:
    pairs = []
    for name, value in params.items():
        if value is None:
            continue
        for item in value if isinstance(value, (tuple, list)) else [value]:
            if isinstance(item, bool):
                item = str(item).lower()
            pairs.append((quote(str(name), safe="-_.~"), quote(str(item), safe="-_.~")))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def aws_v4_headers(
    method: str,
    url: str,
    *,
    query: Mapping[str, Any] | None = None,
    body: bytes = b"",
    access_key_id: str,
    secret_access_key: str,
    session_token: str = "",
    region: str,
    service: str,
    now: datetime | None = None,
    headers: Mapping[str, str] | None = None,
    include_host: bool = True,
    include_payload_hash: bool = False,
) -> dict[str, str]:
    """Sign exact wire bytes. include_host=False matches the captured JS SDK.

    The SDK signs x-amz-* headers but does not explicitly set/sign Host.
    Keeping this switch explicit also permits the official AWS test vector.
    """
    parsed = urlsplit(url)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Signing timestamp must include a timezone")
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    signed = {str(k).lower(): " ".join(str(v).split()) for k, v in (headers or {}).items()}
    signed.pop("authorization", None)
    if include_host:
        signed["host"] = parsed.netloc
    signed["x-amz-date"] = stamp
    if session_token:
        signed["x-amz-security-token"] = session_token
    digest = hashlib.sha256(body).hexdigest()
    if include_payload_hash:
        signed["x-amz-content-sha256"] = digest
    excluded = {"content-type", "content-length", "user-agent", "presigned-expires", "expect", "x-amzn-trace-id"}
    names = sorted(k for k in signed if k not in excluded)
    canonical_headers = "".join(f"{key}:{signed[key]}\n" for key in names)
    names_text = ";".join(names)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update(query or {})
    canonical = "\n".join((method.upper(), parsed.path or "/", canonical_query(params), canonical_headers, names_text, digest))
    scope = f"{day}/{region}/{service}/aws4_request"
    message = "\n".join(("AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = ("AWS4" + secret_access_key).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    signed["authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, SignedHeaders={names_text}, Signature={signature}"
    return signed


def image_dimensions(data: bytes) -> tuple[int, int, str]:
    """Read dimensions without an optional imaging dependency (no resampling)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height, "png"
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return (*struct.unpack("<HH", data[6:10]), "gif")
    if data.startswith(b"\xff\xd8"):
        at = 2
        while at + 4 <= len(data):
            if data[at] != 255:
                at += 1
                continue
            while at < len(data) and data[at] == 255:
                at += 1
            if at >= len(data):
                break
            marker = data[at]
            at += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if at + 2 > len(data):
                break
            length = int.from_bytes(data[at:at + 2], "big")
            if length < 2 or at + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                height, width = struct.unpack(">HH", data[at + 3:at + 7])
                return width, height, "jpeg"
            at += length
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1, "webp"
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3fff, height & 0x3fff, "webp"
        if kind == b"VP8L" and data[20] == 0x2f:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1, "webp"
    raise ValueError("Cannot read image dimensions; use a valid PNG, JPEG, GIF, or WebP file")


def video_resource(vid: str, *, width: int, height: int, duration_ms: int, fps: float, name: str = "reference.mp4") -> dict[str, Any]:
    """Use a previously uploaded VOD resource without re-uploading it."""
    if not isinstance(vid, str) or not vid.strip() or any(c in vid for c in "\r\n"):
        raise ValueError("vid is required")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in (width, height, duration_ms, fps)):
        raise ValueError("width, height, duration_ms and fps must be positive finite numbers")
    return {"type": "video", "source_from": "upload", "name": name, "vid": vid, "width": int(width), "height": int(height), "duration": int(duration_ms), "fps": float(fps)}


def mp4_metadata(data: bytes) -> dict[str, int | float]:
    """Read unfragmented ISO BMFF/MP4 video-track metadata, without decoding.

    Uses tkhd display size, mdhd timescale and stts sample timing. Fragmented
    files without a populated stts need explicit metadata or a media probe.
    """
    def boxes(blob):
        at = 0
        while at + 8 <= len(blob):
            size = int.from_bytes(blob[at:at + 4], "big")
            kind = bytes(blob[at + 4:at + 8])
            header = 8
            if size == 1:
                if at + 16 > len(blob):
                    raise ValueError("Truncated extended MP4 box")
                size = int.from_bytes(blob[at + 8:at + 16], "big")
                header = 16
            elif size == 0:
                size = len(blob) - at
            if size < header or at + size > len(blob):
                raise ValueError("Invalid MP4 box length")
            yield kind, blob[at + header:at + size]
            at += size

    def child(blob, kind):
        return next((payload for name, payload in boxes(blob) if name == kind), None)

    moov = child(memoryview(data), b"moov")
    if moov is None:
        raise ValueError("No MP4 moov box; provide explicit video metadata")
    for name, track in boxes(moov):
        if name != b"trak":
            continue
        media = child(track, b"mdia")
        if media is None:
            continue
        handler = child(media, b"hdlr")
        if handler is None or len(handler) < 12 or bytes(handler[8:12]) != b"vide":
            continue
        tkhd, mdhd = child(track, b"tkhd"), child(media, b"mdhd")
        if tkhd is None or mdhd is None or len(tkhd) < 84 or len(mdhd) < 20:
            raise ValueError("Incomplete MP4 video-track headers")
        width, height = (round(int.from_bytes(tkhd[-8:-4], "big") / 65536), round(int.from_bytes(tkhd[-4:], "big") / 65536))
        # tkhd's matrix precedes its final width/height. Account for quarter-turn.
        matrix = tkhd[-44:-8]
        a, b, _, c, d = struct.unpack(">iiiii", matrix[:20])
        if a == d == 0 and abs(b) == abs(c) == 65536:
            width, height = height, width
        if mdhd[0] == 0:
            scale, duration = struct.unpack(">II", mdhd[12:20])
        elif mdhd[0] == 1 and len(mdhd) >= 32:
            scale = int.from_bytes(mdhd[20:24], "big")
            duration = int.from_bytes(mdhd[24:32], "big")
        else:
            raise ValueError("Unsupported MP4 mdhd version")
        minf = child(media, b"minf")
        stbl = child(minf, b"stbl") if minf is not None else None
        stts = child(stbl, b"stts") if stbl is not None else None
        if stts is None or len(stts) < 8:
            raise ValueError("MP4 has no sample timing table; provide explicit fps")
        count = int.from_bytes(stts[4:8], "big")
        if count == 0 or 8 + count * 8 > len(stts):
            raise ValueError("MP4 sample timing is empty or truncated; provide explicit fps")
        samples, ticks = 0, 0
        for offset in range(8, 8 + count * 8, 8):
            sample_count, delta = struct.unpack(">II", stts[offset:offset + 8])
            samples += sample_count
            ticks += sample_count * delta
        if not all(value > 0 for value in (width, height, scale, duration, samples, ticks)):
            raise ValueError("MP4 video metadata contains zero values")
        return {"width": width, "height": height, "duration_ms": round(duration * 1000 / scale), "fps": samples * scale / ticks}
    raise ValueError("No video track found in MP4; provide explicit video metadata")


class AssetUploader:
    """Client must supply request(), region, and a neutral httpx AsyncClient.

    Credentials returned by get_upload_token remain in memory and are not
    included in returned resources. Both image and video carry _upload metadata
    which callers must omit when serializing upstream draft resource nodes.
    """

    def __init__(self, client: Any, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.client = client
        self.max_bytes = max_bytes
        region = str(client.region).upper()
        if region not in _ENDPOINTS:
            raise ValueError("Upload region must be US or SG")
        self.image_host, self.video_host, self.signing_region = _ENDPOINTS[region]

    def _read(self, path: str | Path) -> tuple[Path, bytes]:
        path = Path(path)
        if not path.is_file():
            raise ValueError("Upload source must be an existing local file")
        size = path.stat().st_size
        if not 0 < size <= self.max_bytes:
            raise ValueError(f"File size must be between 1 and {self.max_bytes} bytes (local configured limit)")
        data = path.read_bytes()
        if len(data) != size:
            raise ValueError("Upload source changed while it was being read")
        return path, data

    async def _token(self, scene: int) -> dict[str, str]:
        data = await self.client.request("/mweb/v1/get_upload_token", {"scene": scene})
        if not isinstance(data, dict):
            raise UploadError("token", "Unexpected token response")
        result = {}
        for key, alternate in (("access_key_id", "accessKeyId"), ("secret_access_key", "secretAccessKey"), ("session_token", "sessionToken"), ("space_name", "spaceName")):
            value = data.get(key, data.get(alternate))
            if not isinstance(value, str) or not value:
                raise UploadError("token", f"Missing {key}")
            result[key] = value
        token_region = str(data.get("region", "")).lower()
        # Exact current module 859390 and its caller's T[region] ?? T.sg.
        # In particular, useast5 is an IDC value, not an AWS signing region:
        # the current website falls back to ap-singapore-1 for that value.
        aliases = {"us": "us-east-1", "sg": "ap-singapore-1", "gcp": "gcp", "cn": "cn-north-1", "boe": "boe"}
        result["region"] = aliases.get(token_region, "ap-singapore-1")
        result["token_region"] = token_region
        domain = data.get("upload_domain", data.get("uploadDomain"))
        if domain:
            if not isinstance(domain, str):
                raise UploadError("token", "Invalid upload domain")
            parsed = urlsplit(domain if "://" in domain else "https://" + domain)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise UploadError("token", "Invalid upload domain")
            result["upload_host"] = "https://" + parsed.netloc
        return result

    async def _send(self, method: str, url: str, *, stage: str, headers: dict | None = None, content: bytes = b"") -> dict:
        # Build first and then remove auto-merged cookies/default credentials.
        # A get_upload_token response can populate the shared client's jar.
        # send() does not re-merge cookies, and auth=None bypasses client auth.
        outgoing = self.client.http.build_request(method, url, headers=headers or {}, content=content)
        outgoing.headers.pop("cookie", None)
        if not any(k.lower() == "authorization" for k in (headers or {})):
            outgoing.headers.pop("authorization", None)
        try:
            response = await self.client.http.send(outgoing, follow_redirects=False, auth=None)
        except httpx.HTTPError as exc:
            raise UploadError(stage, f"Network request failed ({type(exc).__name__})") from None
        if not 200 <= response.status_code < 300:
            raise UploadError(stage, "HTTP request failed", response.status_code)
        try:
            result = response.json()
        except ValueError:
            raise UploadError(stage, "Response was not JSON") from None
        if not isinstance(result, dict):
            raise UploadError(stage, "Response must be an object")
        metadata = result.get("ResponseMetadata") or {}
        if not isinstance(metadata, dict):
            raise UploadError(stage, "Unexpected gateway metadata")
        error = metadata.get("Error")
        if error:
            raise UploadError(stage, "Upload gateway rejected the request", error.get("Code", "unknown") if isinstance(error, dict) else "unknown")
        return result

    async def _gateway(self, method: str, host: str, query: dict, token: dict, *, service: str, body: dict | None = None, stage: str) -> dict:
        content = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers = aws_v4_headers(method, host + "/", query=query, body=content, access_key_id=token["access_key_id"], secret_access_key=token["secret_access_key"], session_token=token["session_token"], region=token["region"], service=service, include_host=False, include_payload_hash=body is not None)
        if body is not None:
            headers["content-type"] = "application/json"
        return await self._send(method, host + "/?" + canonical_query(query), stage=stage, headers=headers, content=content)

    @staticmethod
    def _address(response: dict, *, image: bool) -> tuple[str, str, str, dict]:
        try:
            result = response["Result"]
            address = result["UploadAddress"] if image else result["InnerUploadAddress"]["UploadNodes"][0]
            info = address["StoreInfos"][0]
            host = address["UploadHosts"][0] if image else address["UploadHost"]
            auth, uri, session_key = info["Auth"], info["StoreUri"], address["SessionKey"]
            extra_headers = address.get("UploadHeader") or {}
            if not all(isinstance(v, str) and v for v in (host, auth, uri, session_key)):
                raise ValueError()
            parsed = urlsplit(host if "://" in host else "https://" + host)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError()
            if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                raise ValueError()
            url = "https://" + parsed.netloc + "/upload/v1/" + quote(uri, safe="/-_.~")
            if not isinstance(extra_headers, dict):
                raise ValueError()
            if any(k.lower() in ("cookie", "host", "authorization") for k in extra_headers):
                raise ValueError()
            headers = {"authorization": auth, **extra_headers}
            return url, session_key, uri, headers
        except (KeyError, IndexError, TypeError, ValueError):
            raise UploadError("apply", "Missing or unsupported upload address") from None

    @staticmethod
    def _storage_ok(result: dict, stage: str) -> dict:
        if result.get("code") != 2000:
            raise UploadError(stage, "Storage server rejected the request", result.get("code", "missing"))
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise UploadError(stage, "Unexpected storage data response")
        return data

    async def _transfer(self, url: str, headers: dict, data: bytes) -> str:
        if len(data) <= DIRECT_LIMIT:
            result = await self._send("POST", url, stage="transfer", headers={**headers, "content-type": "application/octet-stream", "content-crc32": crc32_hex(data)}, content=data)
            self._storage_ok(result, "transfer")
            return "direct"
        # Classic SDK part mode, not its newer dynamic stream / logical-part mode.
        if any(k.lower() == "x-logical-part-mode" for k in headers):
            raise UploadError("transfer", "This server requires logical-part mode; only classic part mode is implemented")
        init = await self._send("POST", url + "?uploadmode=part&phase=init", stage="init", headers=headers)
        upload_id = self._storage_ok(init, "init").get("uploadid")
        if not isinstance(upload_id, str) or not upload_id:
            raise UploadError("init", "Missing uploadid")
        checksums = []
        for index, offset in enumerate(range(0, len(data), DIRECT_LIMIT), start=1):
            part = data[offset:offset + DIRECT_LIMIT]
            checksum = crc32_hex(part)
            query = {"uploadid": upload_id, "part_number": index, "phase": "transfer", "part_offset": offset}
            response = await self._send("POST", url + "?" + canonical_query(query), stage=f"part-{index}", headers={**headers, "content-type": "application/octet-stream", "content-crc32": checksum}, content=part)
            self._storage_ok(response, f"part-{index}")
            checksums.append(f"{index}:{checksum}")
        query = {"uploadmode": "part", "phase": "finish", "uploadid": upload_id, "size": len(data)}
        response = await self._send("POST", url + "?" + canonical_query(query), stage="finish", headers=headers, content=",".join(checksums).encode())
        if not self._storage_ok(response, "finish").get("key"):
            raise UploadError("finish", "Storage server did not return a merged key")
        return "classic-part"

    async def upload_image(self, path: str | Path) -> dict[str, Any]:
        path, data = self._read(path)
        width, height, image_format = image_dimensions(data)
        if width <= 0 or height <= 0:
            raise ValueError("Image dimensions must be positive")
        token = await self._token(2)
        host = token.get("upload_host", self.image_host)
        query = {"Action": "ApplyImageUpload", "Version": "2018-08-01", "ServiceId": token["space_name"], "FileSize": len(data), "s": secrets.token_hex(8), "device_platform": "web"}
        apply = await self._gateway("GET", host, query, token, service="imagex", stage="apply")
        url, session_key, _, headers = self._address(apply, image=True)
        transfer = await self._transfer(url, headers, data)
        query = {"Action": "CommitImageUpload", "Version": "2018-08-01", "ServiceId": token["space_name"]}
        commit = await self._gateway("POST", host, query, token, service="imagex", body={"SessionKey": session_key}, stage="commit")
        result = commit.get("Result") or {}
        items, plugins = result.get("Results") or [], result.get("PluginResult") or []
        item, plugin = (items[0] if items else {}), (plugins[0] if plugins else {})
        if item.get("UriStatus") not in (None, 2000):
            raise UploadError("commit", "Image URI was not accepted", item.get("UriStatus"))
        uri = plugin.get("ImageUri") or item.get("Uri")
        if not isinstance(uri, str) or not uri:
            raise UploadError("commit", "ImageX did not return a committed image URI")
        return {"type": "image", "source_from": "upload", "platform_type": 1, "name": path.name, "image_uri": uri, "uri": uri, "width": width, "height": height, "format": image_format,
                "_upload": {"evidence": UPLOAD_EVIDENCE, "status": "committed", "generation_verified": False, "experimental": True, "transfer": transfer, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "upload_host": host, "signing_region": token["region"]}}

    async def upload_video(self, path: str | Path, *, metadata: dict | None = None, experimental: bool = False) -> dict[str, Any]:
        """Upload VOD with an explicit opt-in; metadata duration_ms is milliseconds.

        Commit VideoMeta supplies width/height/duration when available. fps and
        any missing fields must come from caller-provided real media metadata.
        This does not implement Dreamina's separate pre-audit or asset library.
        """
        if not experimental:
            raise NotImplementedError("Reference generation is not implied by a VOD upload; set experimental=True explicitly or supply an existing vid resource")
        path, data = self._read(path)
        metadata = dict(metadata or {})
        if not all(metadata.get(key) for key in ("width", "height", "duration_ms", "fps")):
            try:
                metadata = {**mp4_metadata(data), **metadata}
            except (ValueError, struct.error):
                # Other media containers still have a valid VOD upload path;
                # commit may return dimensions, and fps can be supplied later.
                pass
        token = await self._token(1)
        host = token.get("upload_host", self.video_host)
        query = {"Action": "ApplyUploadInner", "Version": "2020-11-19", "SpaceName": token["space_name"], "FileType": "video", "IsInner": 1, "FileSize": len(data), "s": secrets.token_hex(8), "device_platform": "web"}
        apply = await self._gateway("GET", host, query, token, service="vod", stage="apply")
        url, session_key, _, headers = self._address(apply, image=False)
        transfer = await self._transfer(url, headers, data)
        query = {"Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": token["space_name"]}
        commit = await self._gateway("POST", host, query, token, service="vod", body={"SessionKey": session_key, "Functions": []}, stage="commit")
        items = (commit.get("Result") or {}).get("Results") or []
        if not items or not items[0].get("Vid"):
            raise UploadError("commit", "VOD did not return a committed Vid")
        item = items[0]
        meta = item.get("VideoMeta") or {}
        vid = item["Vid"]
        values = {"width": meta.get("Width") or metadata.get("width"), "height": meta.get("Height") or metadata.get("height"), "duration_ms": metadata.get("duration_ms"), "fps": meta.get("Fps") or meta.get("FrameRate") or metadata.get("fps")}
        try:
            if meta.get("Duration"):
                values["duration_ms"] = round(float(meta["Duration"]) * 1000)
            values = {key: float(value) if value is not None else None for key, value in values.items()}
            resource = video_resource(vid, **values, name=path.name)
        except (ValueError, TypeError, OverflowError):
            # Preserve the newly committed identifier so the caller can supply
            # missing metadata without uploading the same video a second time.
            resource = {"type": "video", "source_from": "upload", "vid": vid, "name": path.name}
            invalid = [key for key, value in values.items() if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0]
            resource["_upload"] = {"metadata_required": invalid, "metadata_values": {key: None if key in invalid else value for key, value in values.items()}, "metadata_complete": False}
        info = resource.setdefault("_upload", {"metadata_complete": True})
        info.update({"evidence": UPLOAD_EVIDENCE, "status": "committed", "generation_verified": False, "experimental": True, "transfer": transfer, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "upload_host": host, "signing_region": token["region"], "limitations": ["Successful upload does not verify reference video generation", "Classic part mode only; logical-part and dynamic stream modes are unsupported", "Separate reference pre-audit is not performed"]})
        return resource
