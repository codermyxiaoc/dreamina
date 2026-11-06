"""Offline protocol checks; these do not prove upstream upload acceptance."""
import asyncio
import struct
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from dreamina_api.uploads import (
    AssetUploader, DIRECT_LIMIT, UploadError, aws_v4_headers, canonical_query,
    crc32_hex, image_dimensions, video_resource, mp4_metadata,
)


def test_aws_official_get_vanilla_vector():
    # https://github.com/boto/botocore/tree/develop/tests/unit/auth/aws4_testsuite/get-vanilla
    headers = aws_v4_headers(
        "GET", "https://example.amazonaws.com/", access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1", service="service", now=datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc),
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_query_encoding_and_crc_known_check_value():
    assert crc32_hex(b"123456789") == "cbf43926"
    assert crc32_hex(b"") == "00000000"
    assert canonical_query({"z": "a b/c", "a": ["z", "+"], "ignored": None}) == "a=%2B&a=z&z=a%20b%2Fc"


def test_sdk_signing_omits_host_but_signs_exact_body_and_token():
    args = dict(access_key_id="fixture-ak", secret_access_key="fixture-secret", session_token="fixture-session", region="us-east-1", service="imagex", now=datetime(2026, 10, 25, tzinfo=timezone.utc), include_host=False, include_payload_hash=True)
    one = aws_v4_headers("POST", "https://example.test/", body=b'{"a":1}', **args)
    two = aws_v4_headers("POST", "https://example.test/", body=b'{ "a":1}', **args)
    assert "SignedHeaders=x-amz-content-sha256;x-amz-date;x-amz-security-token" in one["authorization"]
    assert "host" not in one
    assert one["authorization"] != two["authorization"]
    assert one["x-amz-security-token"] == "fixture-session"


def test_real_useast5_token_uses_sdk_fallback_and_returned_upload_domain():
    class ActualShapeClient(FakeClient):
        async def request(self, path, body, query=None):
            value = await super().request(path, body, query)
            return {**value, "region": "useast5", "upload_domain": "imagex.capcutapi.us"}

    def handler(request):
        assert request.url.host == "imagex.capcutapi.us"
        assert "/ap-singapore-1/imagex/aws4_request" in request.headers["authorization"]
        return httpx.Response(200, json={"Result": {}})

    async def run():
        client = ActualShapeClient(handler)
        try:
            uploader = AssetUploader(client)
            token = await uploader._token(2)
            assert token["region"] == "ap-singapore-1"
            assert token["upload_host"] == "https://imagex.capcutapi.us"
            await uploader._gateway("GET", token["upload_host"], {"Action": "ApplyImageUpload"}, token, service="imagex", stage="apply")
        finally:
            await client.http.aclose()
    asyncio.run(run())


def _png_header(width=640, height=360):
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", width, height)


def test_dimensions_and_reject_missing_video_metadata():
    assert image_dimensions(_png_header()) == (640, 360, "png")
    with pytest.raises(ValueError):
        image_dimensions(b"not-an-image")
    with pytest.raises(ValueError):
        video_resource("existing-vid", width=0, height=360, duration_ms=4000, fps=24)
    with pytest.raises(ValueError):
        video_resource("existing-vid", width=640, height=360, duration_ms=4000, fps=float("nan"))
    assert video_resource("existing-vid", width=640, height=360, duration_ms=4000, fps=24)["duration"] == 4000


def test_mp4_timing_and_display_dimensions():
    def box(kind, content):
        return struct.pack(">I", len(content) + 8) + kind + content
    track_header = bytearray(84)
    track_header[-8:] = struct.pack(">II", 1280 * 65536, 720 * 65536)
    track_header[-44:-8] = struct.pack(">9i", 65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
    media_header = bytearray(24)
    media_header[12:20] = struct.pack(">II", 24000, 96000)
    handler = b"\0" * 8 + b"vide" + b"\0" * 12
    timing = b"\0" * 4 + struct.pack(">III", 1, 96, 1000)
    media = box(b"mdhd", media_header) + box(b"hdlr", handler) + box(b"minf", box(b"stbl", box(b"stts", timing)))
    movie = box(b"moov", box(b"trak", box(b"tkhd", track_header) + box(b"mdia", media)))
    assert mp4_metadata(movie) == {"width": 1280, "height": 720, "duration_ms": 4000, "fps": 24.0}
    with pytest.raises(ValueError, match="No MP4 moov"):
        mp4_metadata(b"")
    with pytest.raises(ValueError, match="Invalid MP4 box length"):
        mp4_metadata(struct.pack(">I", 9999999) + b"moov")


class FakeClient:
    region = "US"

    def __init__(self, handler):
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.scenes = []

    async def request(self, path, body, query=None):
        assert path == "/mweb/v1/get_upload_token"
        self.scenes.append(body["scene"])
        return {"access_key_id": "fixture-ak", "secret_access_key": "fixture-secret", "session_token": "fixture-session", "space_name": "fixture-space", "region": "us"}


def test_image_complete_flow_and_no_cookie_on_storage(tmp_path):
    calls = []
    data = _png_header()
    path = tmp_path / "image.png"
    path.write_bytes(data)

    def handler(request):
        calls.append(request)
        assert not request.headers.get("cookie")
        action = request.url.params.get("Action")
        if action == "ApplyImageUpload":
            assert request.url.params["ServiceId"] == "fixture-space"
            assert request.headers["x-amz-security-token"] == "fixture-session"
            return httpx.Response(200, json={"Result": {"UploadAddress": {"StoreInfos": [{"StoreUri": "bucket/image", "Auth": "storage-auth"}], "UploadHosts": ["store.example.test"], "SessionKey": "fixture-key"}}})
        if request.url.host == "store.example.test":
            assert request.content == data
            assert request.headers["authorization"] == "storage-auth"
            assert request.headers["content-crc32"] == crc32_hex(data)
            assert "x-amz-security-token" not in request.headers
            return httpx.Response(200, json={"code": 2000, "data": {}})
        assert action == "CommitImageUpload"
        assert request.content == b'{"SessionKey":"fixture-key"}'
        return httpx.Response(200, json={"Result": {"Results": [{"Uri": "committed/image", "UriStatus": 2000}]}})

    async def run():
        client = FakeClient(handler)
        try:
            result = await AssetUploader(client).upload_image(path)
            assert result["uri"] == result["image_uri"] == "committed/image"
            assert (result["width"], result["height"]) == (640, 360)
            assert result["_upload"]["evidence"] == "upstream-commit-confirmed"
            assert result["_upload"]["generation_verified"] is False
            assert client.scenes == [2]
            assert len(calls) == 3
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_storage_business_error_stops_before_commit(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(_png_header())
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.params.get("Action") == "ApplyImageUpload":
            return httpx.Response(200, json={"Result": {"UploadAddress": {"StoreInfos": [{"StoreUri": "b/i", "Auth": "storage-auth"}], "UploadHosts": ["store.example.test"], "SessionKey": "fixture-key"}}})
        return httpx.Response(200, json={"code": 4001, "message": "do not expose fixture-secret"})

    async def run():
        client = FakeClient(handler)
        try:
            with pytest.raises(UploadError, match="Storage server rejected") as exc:
                await AssetUploader(client).upload_image(path)
            assert "fixture-secret" not in str(exc.value)
            assert len(calls) == 2
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_classic_part_flow_crc_merge_and_order():
    data = b"a" * DIRECT_LIMIT + b"second part"
    calls = []

    def handler(request):
        query = parse_qs(request.url.query.decode())
        calls.append(query["phase"][0])
        if query["phase"] == ["init"]:
            return httpx.Response(200, json={"code": 2000, "data": {"uploadid": "fixture-upload"}})
        if query["phase"] == ["transfer"]:
            index = int(query["part_number"][0])
            expected = data[(index - 1) * DIRECT_LIMIT:index * DIRECT_LIMIT]
            assert int(query["part_offset"][0]) == (index - 1) * DIRECT_LIMIT
            assert request.content == expected
            assert request.headers["content-crc32"] == crc32_hex(expected)
            return httpx.Response(200, json={"code": 2000})
        assert query["size"] == [str(len(data))]
        assert request.content.decode() == f"1:{crc32_hex(data[:DIRECT_LIMIT])},2:{crc32_hex(data[DIRECT_LIMIT:])}"
        return httpx.Response(200, json={"code": 2000, "data": {"key": "merged"}})

    async def run():
        client = FakeClient(handler)
        try:
            mode = await AssetUploader(client)._transfer("https://store.example.test/upload/v1/file", {"authorization": "storage-auth"}, data)
            assert mode == "classic-part"
            assert calls == ["init", "transfer", "transfer", "finish"]
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_video_explicit_optin_and_existing_vid(tmp_path):
    async def run():
        client = FakeClient(lambda request: pytest.fail("Must not make a network request"))
        try:
            with pytest.raises(NotImplementedError, match="experimental=True"):
                await AssetUploader(client).upload_video(tmp_path / "missing.mp4")
            assert client.scenes == []
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_video_flow_preserves_committed_vid_when_fps_missing(tmp_path):
    path = tmp_path / "reference.mp4"
    path.write_bytes(b"fixture-video")

    def handler(request):
        action = request.url.params.get("Action")
        if action == "ApplyUploadInner":
            assert request.url.params["FileType"] == "video"
            return httpx.Response(200, json={"Result": {"InnerUploadAddress": {"UploadNodes": [{"StoreInfos": [{"StoreUri": "b/v", "Auth": "storage-auth"}], "UploadHost": "store.example.test", "SessionKey": "fixture-key"}]}}})
        if action == "CommitUploadInner":
            assert request.content == b'{"SessionKey":"fixture-key","Functions":[]}'
            return httpx.Response(200, json={"Result": {"Results": [{"Vid": "new-vid", "VideoMeta": {"Width": 640, "Height": 360, "Duration": 4}}]}})
        return httpx.Response(200, json={"code": 2000})

    async def run():
        client = FakeClient(handler)
        try:
            resource = await AssetUploader(client).upload_video(path, experimental=True)
            assert resource["vid"] == "new-vid"
            assert resource["_upload"]["metadata_complete"] is False
            assert resource["_upload"]["metadata_required"] == ["fps"]
            assert client.scenes == [1]
            resource = await AssetUploader(client).upload_video(path, experimental=True, metadata={"fps": 24})
            assert resource["duration"] == 4000
            assert resource["_upload"]["metadata_complete"] is True
        finally:
            await client.http.aclose()
    asyncio.run(run())


def test_removes_default_cookie_and_authorization_before_cross_domain_request():
    def handler(request):
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"ok": True})
    async def run():
        client = FakeClient(handler)
        client.http.headers["cookie"] = "sessionid=fixture"
        client.http.headers["authorization"] = "Bearer fixture"
        client.http.cookies.set("sessionid", "fixture", domain="store.example.test")
        try:
            assert await AssetUploader(client)._send("GET", "https://store.example.test", stage="apply") == {"ok": True}
        finally:
            await client.http.aclose()
    asyncio.run(run())
