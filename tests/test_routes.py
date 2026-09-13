"""End-to-end route tests using httpx MockTransport."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.asyncio

from app.cache import WorkflowCache
from app.main import create_app
from app.settings import Settings
from app.upstream import UpstreamClient


UPSTREAM_CALLS: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []


# Mock schema payloads keyed by workflow id. Tests override / extend this.
SCHEMAS: dict[str, dict[str, Any]] = {
    "wf-aaa": {
        "code": "Success",
        "data": {
            "uuid": "wf-aaa",
            "name": "Image To Video 5s",
            "input_rules": {
                "prompt": {"required": True, "type": "prompt"},
                "duration": {"required": False, "type": "integer", "default": 5},
                "seed": {"required": False, "type": "integer"},
                "ref_image_0": {
                    "required": True,
                    "type": "image",
                    "accept_types": ["image/jpeg", "image/png"],
                },
                "ref_image_1": {
                    "required": False,
                    "type": "image",
                    "accept_types": ["image/jpeg", "image/png"],
                },
                "resolution": {
                    "required": False,
                    "type": "enum",
                    "default": "768p横",
                    "options": [
                        {"label": "480p横", "values": {"aspect_ratio": "16:9"}},
                        {"label": "480p竖", "values": {"aspect_ratio": "9:16"}},
                        {"label": "768p横", "values": {"aspect_ratio": "16:9"}},
                        {"label": "768p竖", "values": {"aspect_ratio": "9:16"}},
                    ],
                },
            },
        },
    },
    "wf-bbb": {
        "code": "Success",
        "data": {
            "uuid": "wf-bbb",
            "name": "Text To Video 720p",
            "input_rules": {
                "prompt": {"required": True, "type": "prompt"},
            },
        },
    },
    "wf-no-name": {
        "code": "Success",
        "data": {
            "uuid": "wf-no-name",
            "name": "",
            "input_rules": {},
        },
    },
}


def _record(request: httpx.Request) -> httpx.Response:
    body: dict[str, Any] | None = None
    if request.content and request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = json.loads(request.content)
        except ValueError:
            body = None
    UPSTREAM_CALLS.append((request.method, str(request.url), body, dict(request.headers)))

    path = request.url.path

    if path.endswith("/api/v1/comfyui/workflows") and request.method == "POST":
        return httpx.Response(
            200,
            json={
                "code": "Success",
                "data": {
                    "list": [
                        {"uuid": "wf-aaa", "name": "Image To Video 5s"},
                        {"uuid": "wf-bbb", "name": "Text To Video 720p"},
                        {"uuid": "wf-no-name", "name": ""},
                        {"uuid": "", "name": "should be skipped"},
                    ]
                },
            },
            request=request,
        )

    if path.startswith("/api/v1/comfyui/workflows/") and "/comfyui_workflow/" not in path:
        # Schema lookup
        parts = path.rsplit("/", 1)
        workflow_id = parts[-1]
        schema = SCHEMAS.get(workflow_id)
        if schema is None:
            return httpx.Response(404, json={"code": "NotFound"}, request=request)
        return httpx.Response(200, json=schema, request=request)

    if path.startswith("/api/v1/comfyui/comfyui_workflow/") and "/result/" not in path:
        return httpx.Response(
            200,
            json={"code": "Success", "data": {"task_id": "task-xyz", "status": "QUEUED"}},
            request=request,
        )

    if "/api/v1/comfyui/comfyui_workflow/result/" in path:
        return httpx.Response(
            200,
            json={
                "task_id": "task-xyz",
                "status": "SUCCESS",
                "results": [
                    {"url": "https://cdn.autodl.example/r/abc.mp4", "filename": "abc.mp4"},
                ],
            },
            request=request,
        )

    return httpx.Response(404, json={"code": "NotFound"}, request=request)


@pytest.fixture
def mock_transport() -> httpx.MockTransport:
    UPSTREAM_CALLS.clear()
    return httpx.MockTransport(_record)


@pytest.fixture
def app(mock_transport: httpx.MockTransport):
    settings = Settings(
        upstream_base_url="https://www.autodl.art/api/v1/comfyui",
        log_level="WARNING",
    )
    application = create_app(settings)

    @asynccontextmanager
    async def patched_lifespan(app_):
        async with httpx.AsyncClient(transport=mock_transport) as http:
            app_.state.http = http
            app_.state.upstream = UpstreamClient(
                http, base_url=settings.upstream_base_url
            )
            app_.state.cache = WorkflowCache()
            app_.state.settings = settings
            yield

    application.router.lifespan_context = patched_lifespan
    return application


@pytest.fixture
async def client(app):
    from httpx import ASGITransport, AsyncClient

    @asynccontextmanager
    async def lifespan_scope():
        async with app.router.lifespan_context(app):
            yield

    async with lifespan_scope():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


# ---------------------------------------------------------------- /v1/models


async def test_list_models(client):
    r = await client.get(
        "/v1/models", headers={"Authorization": "Bearer raw-token"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    ids = [m["id"] for m in data]
    assert ids == ["wf-aaa", "wf-bbb", "wf-no-name"]
    assert data[0]["display_name"] == "Image To Video 5s"
    assert data[2]["display_name"] == "wf-no-name"


async def test_list_models_no_auth(client):
    r = await client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert r.headers["www-authenticate"] == "Bearer"


async def test_list_models_upstream_error(app):
    """If the catalog call fails, surface 502 even after cache init."""

    from httpx import ASGITransport, AsyncClient

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops", request=request)

    transport = httpx.MockTransport(boom)

    @asynccontextmanager
    async def lifespan(app_):
        async with httpx.AsyncClient(transport=transport) as http:
            app_.state.http = http
            app_.state.upstream = UpstreamClient(
                http, base_url="https://www.autodl.art/api/v1/comfyui"
            )
            app_.state.cache = WorkflowCache()
            app_.state.settings = Settings(
                upstream_base_url="https://www.autodl.art/api/v1/comfyui"
            )
            yield

    app.router.lifespan_context = lifespan

    @asynccontextmanager
    async def scope():
        async with app.router.lifespan_context(app):
            yield

    async with scope():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/v1/models", headers={"Authorization": "Bearer x"})
    assert r.status_code == 502


# ---------------------------------------------------------------- /v1/videos


async def test_create_video(client):
    """wf-aaa accepts any size we send (cache has whitelist but we
    send '1280x720' which is not in the whitelist -> 400 now).

    Use a value that IS in the whitelist to test happy path.
    """
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-autodl-token"},
        json={
            "model": "wf-aaa",
            "prompt": "a cat on the moon",
            "seconds": 5,
            "size": "768p横",
            "metadata": {"seed": 42, "ref_image_0": "https://x.test/r.png"},
            "extra_field": "should pass through",
        },
    )
    assert r.status_code == 202
    obj = r.json()
    assert obj["id"] == "task-xyz"
    assert obj["status"] == "queued"
    assert obj["model"] == "wf-aaa"

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "a cat on the moon"
    )
    body = submit[2]
    headers = submit[3]
    assert headers["authorization"] == "raw-autodl-token"
    assert "model" not in body
    assert body["prompt"] == "a cat on the moon"
    assert body["duration"] == 5
    assert body["resolution"] == "768p横"
    assert body["seed"] == 42
    assert body["ref_image_0"] == "https://x.test/r.png"
    # Unknown client-side fields are whitelist-filtered, not forwarded.
    assert "extra_field" not in body
    assert "metadata" not in body


async def test_create_video_size_mapped_to_label(client):
    """OpenAI 'WxH' size gets translated to the AutoDL label before submit."""
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "x",
            "size": "1280x720",  # landscape 720 → "768p横"
        },
    )
    assert r.status_code == 202
    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST" and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "x"
    )
    assert submit[2]["resolution"] == "768p横"


async def test_create_video_invalid_size_rejected_locally(client):
    """A WxH that maps to no known label → 400 before hitting upstream."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "x",
            "size": "1280x1280",  # square, but wf-aaa has no (1:1) labels
        },
    )
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "Invalid" in msg
    assert "1280x1280" in msg
    for label in ("480p横", "480p竖", "768p横", "768p竖"):
        assert label in msg
    # And no upstream submit happened for this workflow
    submits = [c for c in UPSTREAM_CALLS if c[0] == "POST" and "/comfyui_workflow/" in c[1]]
    assert not submits


async def test_create_video_unknown_workflow_passes_through(client):
    """If the workflow is not in the cache, we defer to upstream."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-unknown",
            "prompt": "x",
            "size": "anything",
        },
    )
    assert r.status_code == 202


async def test_create_video_no_resolution_rule_passes_through(client):
    """wf-bbb has no resolution rule → we don't validate."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-bbb",
            "prompt": "x",
            "size": "1280x720",
        },
    )
    assert r.status_code == 202


async def test_create_video_auto_fills_seed(client):
    """wf-aaa declares `seed` but the client doesn't send it → gateway
    injects a random seed within AutoDL's range."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={"model": "wf-aaa", "prompt": "auto-seed", "size": "480p横"},
    )
    assert r.status_code == 202
    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST" and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "auto-seed"
    )
    seed = submit[2].get("seed")
    assert isinstance(seed, int)
    assert 1 <= seed <= 999_999_999_999_999


async def test_create_video_client_seed_preserved(client):
    """When the client sends an explicit seed, it is not overwritten."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={"model": "wf-aaa", "prompt": "explicit-seed", "seed": 42, "size": "480p横"},
    )
    assert r.status_code == 202
    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST" and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "explicit-seed"
    )
    assert submit[2]["seed"] == 42


async def test_create_video_explicit_resolution_wins(client):
    """If client passes both size and resolution, explicit resolution wins
    (current behavior), but the validation still runs on the effective value.
    """
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "test",
            "duration": 12,
            "seconds": 5,
            "resolution": "480p横",
            "size": "640x360",
        },
    )
    assert r.status_code == 202
    body = next(
        c[2] for c in UPSTREAM_CALLS
        if c[0] == "POST" and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "test"
    )
    assert body["duration"] == 12
    assert body["resolution"] == "480p横"


async def test_create_video_missing_model(client):
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={"prompt": "x"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_create_video_bad_json(client):
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        content=b"not json",
    )
    assert r.status_code == 400


# ---------------------------------------------------------------- multipart / reference upload


async def test_create_video_multipart_uploads_ref_image_0(client):
    """A multipart POST that includes `input_reference[]` must turn
    the file bytes into `ref_image_0` on the upstream body, so the
    AutoDL workflow actually receives the image."""

    import base64

    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae"
        "426082"
    )
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        data={
            "model": "wf-aaa",
            "prompt": "animate this",
            "seconds": "6",
            "size": "720x1280",
        },
        files={
            "input_reference[]": ("image-image-1.png", png_bytes, "image/png"),
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "animate this"
    )
    body = submit[2]
    assert body["prompt"] == "animate this"
    assert body["duration"] == 6
    # The crucial assertion: the upstream body contains ref_image_0
    # as a data URL whose payload decodes back to the original PNG.
    assert "ref_image_0" in body
    assert body["ref_image_0"].startswith("data:image/png;base64,")
    encoded = body["ref_image_0"].split(",", 1)[1]
    assert base64.b64decode(encoded) == png_bytes


async def test_create_video_multipart_two_refs_become_ref_image_0_and_1(client):
    """Multiple `input_reference[]` files → ref_image_0, ref_image_1, ...,
    preserving multipart order."""

    import base64

    png_a = b"\x89PNG\r\n\x1a\n" + b"first"
    png_b = b"\x89PNG\r\n\x1a\n" + b"second"

    # httpx's `files=` dict only sends one file per field name, but a
    # browser using `<input type="file" name="input_reference[]" multiple>`
    # sends N parts with the SAME field name. Build the multipart body
    # by hand to mimic that.
    boundary = "TestBoundaryManyRefs"
    crlf = b"\r\n"
    parts: list[bytes] = []
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(b'Content-Disposition: form-data; name="model"' + crlf + crlf)
    parts.append(b"wf-aaa" + crlf)
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(b'Content-Disposition: form-data; name="prompt"' + crlf + crlf)
    parts.append(b"two-refs" + crlf)
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(b'Content-Disposition: form-data; name="size"' + crlf + crlf)
    parts.append("480p横".encode("utf-8") + crlf)
    for fname, payload, mime in [("a.png", png_a, "image/png"), ("b.jpg", png_b, "image/jpeg")]:
        parts.append(b"--" + boundary.encode() + crlf)
        parts.append(
            f'Content-Disposition: form-data; name="input_reference[]"; filename="{fname}"'.encode()
            + crlf
        )
        parts.append(f"Content-Type: {mime}".encode() + crlf + crlf)
        parts.append(payload + crlf)
    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    body_bytes = b"".join(parts)

    r = await client.post(
        "/v1/videos",
        headers={
            "Authorization": "Bearer raw-token",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=body_bytes,
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "two-refs"
    )
    body = submit[2]
    assert "ref_image_0" in body and "ref_image_1" in body
    assert body["ref_image_0"].startswith("data:image/png;base64,")
    assert body["ref_image_1"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(body["ref_image_0"].split(",", 1)[1]) == png_a
    assert base64.b64decode(body["ref_image_1"].split(",", 1)[1]) == png_b


async def test_create_video_multipart_no_reference_still_submits(client):
    """A multipart POST with no reference files still works — the
    workflow just runs without ref_image_0 (and would be rejected by
    AutoDL only if ref_image_0 is required and missing; that's an
    upstream error, not a gateway bug)."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        data={
            "model": "wf-bbb",  # no ref_image_0 declared
            "prompt": "no-refs",
        },
    )
    assert r.status_code == 202
    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-bbb" in c[1]
        and c[2] and c[2].get("prompt") == "no-refs"
    )
    assert "ref_image_0" not in submit[2]


# ---------------------------------------------------------------- references (JSON)


async def test_create_video_references_array_becomes_ref_image_0(client):
    """The infinite-canvas frontend sends a JSON ``references`` array of
    URLs. Without translation, the whitelist filter silently drops it
    and the workflow runs without any reference image — a hard-to-spot
    regression. After the fix, the URLs must land on ref_image_0 / 1."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "use the reference",
            "size": "720x1280",
            "metadata": {
                "references": [
                    "https://apioss15.sydney-ai.com/ximg/27h.t3.3c.o0x1ck/"
                    "bcd04854-1dfc-4c08-b10a-bc27c94cbada/gi21k-e1789298085.png",
                ],
                "negativePrompt": "",
                "shotType": "intelligence",
            },
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "use the reference"
    )
    body = submit[2]
    assert "references" not in body, (
        "references array must be removed before the whitelist filter"
    )
    assert body["ref_image_0"] == (
        "https://apioss15.sydney-ai.com/ximg/27h.t3.3c.o0x1ck/"
        "bcd04854-1dfc-4c08-b10a-bc27c94cbada/gi21k-e1789298085.png"
    )
    # Whitelist still kicks in for client-side UI fields.
    assert "negativePrompt" not in body
    assert "shotType" not in body
    # resolution mapping from size still works.
    assert body["resolution"] in {"768p竖", "480p竖"}


async def test_create_video_references_array_multiple_numbered_in_order(client):
    """Multiple entries in the references array become ref_image_0/1/…."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "two-refs",
            "size": "720x1280",
            "references": [
                "https://cdn.example/a.png",
                "https://cdn.example/b.png",
            ],
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "two-refs"
    )
    body = submit[2]
    assert body["ref_image_0"] == "https://cdn.example/a.png"
    assert body["ref_image_1"] == "https://cdn.example/b.png"
    assert "references" not in body


async def test_create_video_references_does_not_clobber_existing_ref_image_0(client):
    """If the caller already supplied ref_image_0 (hand-written JSON),
    the references array must continue from the next free slot rather
    than overwriting the explicit value. The slot index used here is
    1, which IS declared by the wf-aaa schema, so the whitelist check
    does not strip it."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "mixed",
            "size": "720x1280",
            "ref_image_0": "https://already.example/a.png",
            "references": [
                "https://cdn.example/b.png",
                "https://cdn.example/c.png",  # slot 2 — wf-aaa only declares 0/1
            ],
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "mixed"
    )
    body = submit[2]
    # The explicit value wins.
    assert body["ref_image_0"] == "https://already.example/a.png"
    # references[0] lands on the next free slot (1).
    assert body["ref_image_1"] == "https://cdn.example/b.png"
    # references[1] would land on slot 2, which the workflow doesn't
    # declare; the whitelist filter correctly drops it. We assert the
    # absence to lock in that no field outside the whitelist leaks.
    assert "ref_image_2" not in body


# ---------------------------------------------------------------- reference aliases (JSON)


async def test_create_video_input_reference_string_url_lands_on_ref_image_0(client):
    """The OpenAI official ``input_reference`` field, as a JSON string,
    must reach the upstream body as ``ref_image_0``. Without this fix
    the whitelist filter would drop it (the workflow schema only
    declares ``ref_image_<N>``)."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "use the input_reference",
            "size": "720x1280",
            "input_reference": "https://cdn.example/a.png",
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "use the input_reference"
    )
    body = submit[2]
    assert "input_reference" not in body
    assert body["ref_image_0"] == "https://cdn.example/a.png"


async def test_create_video_input_reference_object_with_image_url(client):
    """OpenAI's official object form ``{"image_url": "..."}``."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "use input_reference object",
            "size": "720x1280",
            "input_reference": {"image_url": "https://cdn.example/a.png"},
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "use input_reference object"
    )
    body = submit[2]
    assert body["ref_image_0"] == "https://cdn.example/a.png"


async def test_create_video_input_references_array(client):
    """OpenAI's array form ``input_references: [url, ...]``."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "two input_references",
            "size": "720x1280",
            "input_references": [
                "https://cdn.example/a.png",
                "https://cdn.example/b.png",
            ],
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "two input_references"
    )
    body = submit[2]
    assert body["ref_image_0"] == "https://cdn.example/a.png"
    assert body["ref_image_1"] == "https://cdn.example/b.png"


async def test_create_video_image_url_alias_lands_on_ref_image_0(client):
    """A client sending ``image_url`` as a top-level field must also
    end up on ``ref_image_0`` — otherwise the reference silently
    disappears after the whitelist filter."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "image_url alias",
            "size": "720x1280",
            "image_url": "https://cdn.example/a.png",
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "image_url alias"
    )
    body = submit[2]
    assert "image_url" not in body
    assert body["ref_image_0"] == "https://cdn.example/a.png"


async def test_create_video_reference_image_url_in_metadata(client):
    """``metadata.reference_image_url`` (canvas style) must also
    reach the upstream workflow."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "reference_image_url in metadata",
            "size": "720x1280",
            "metadata": {
                "reference_image_url": "https://cdn.example/a.png",
            },
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "reference_image_url in metadata"
    )
    body = submit[2]
    assert "reference_image_url" not in body
    assert body["ref_image_0"] == "https://cdn.example/a.png"


async def test_create_video_image_base64_in_metadata(client):
    """``metadata.image_base64`` must also reach the upstream
    workflow as a ``ref_image_<N>`` data URL."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "image_base64 in metadata",
            "size": "720x1280",
            "metadata": {
                "image_base64": "data:image/png;base64,AAAA",
            },
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "image_base64 in metadata"
    )
    body = submit[2]
    assert "image_base64" not in body
    assert body["ref_image_0"] == "data:image/png;base64,AAAA"


async def test_create_video_combines_input_reference_and_metadata_ref(client):
    """If the caller pre-populates ``metadata.ref_image_0`` AND also
    sends a JSON ``input_reference`` at the top level, both must reach
    upstream without collision."""

    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        json={
            "model": "wf-aaa",
            "prompt": "combined",
            "size": "720x1280",
            "metadata": {"ref_image_0": "https://already.example/a.png"},
            "input_reference": "https://cdn.example/b.png",
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "combined"
    )
    body = submit[2]
    assert body["ref_image_0"] == "https://already.example/a.png"
    assert body["ref_image_1"] == "https://cdn.example/b.png"


async def test_create_video_multipart_plus_json_alias_combines(client):
    """Multipart file upload AND a JSON ``input_reference`` top-level
    field must combine — the file goes to ref_image_0, the alias to
    ref_image_1."""

    import base64

    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae"
        "426082"
    )
    r = await client.post(
        "/v1/videos",
        headers={"Authorization": "Bearer raw-token"},
        data={
            "model": "wf-aaa",
            "prompt": "multipart + json",
            "size": "720x1280",
            "input_reference": "https://cdn.example/alias.png",
        },
        files={
            "input_reference[]": ("uploaded.png", png_bytes, "image/png"),
        },
    )
    assert r.status_code == 202, r.text

    submit = next(
        c for c in UPSTREAM_CALLS
        if c[0] == "POST"
        and "/comfyui_workflow/wf-aaa" in c[1]
        and c[2] and c[2].get("prompt") == "multipart + json"
    )
    body = submit[2]
    # The multipart file landed on slot 0 first (expand_multipart_files
    # runs before normalize_references), so the alias goes to slot 1.
    assert body["ref_image_0"].startswith("data:image/png;base64,")
    encoded = body["ref_image_0"].split(",", 1)[1]
    assert base64.b64decode(encoded) == png_bytes
    assert body["ref_image_1"] == "https://cdn.example/alias.png"


# ---------------------------------------------------------------- retrieve


async def test_retrieve_video(client):
    r = await client.get(
        "/v1/videos/task-xyz", headers={"Authorization": "Bearer raw-token"}
    )
    assert r.status_code == 200
    obj = r.json()
    assert obj["id"] == "task-xyz"
    assert obj["status"] == "completed"
    assert obj["url"] == "https://cdn.autodl.example/r/abc.mp4"


# ---------------------------------------------------------------- content


async def test_video_content_redirect(client):
    r = await client.get(
        "/v1/videos/task-xyz/content",
        headers={"Authorization": "Bearer raw-token"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "https://cdn.autodl.example/r/abc.mp4"


async def test_video_content_no_results(app):
    from httpx import ASGITransport, AsyncClient

    def empty_result(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"task_id": "t", "status": "RUNNING", "results": []},
            request=request,
        )

    transport = httpx.MockTransport(empty_result)

    @asynccontextmanager
    async def lifespan(app_):
        async with httpx.AsyncClient(transport=transport) as http:
            app_.state.http = http
            app_.state.upstream = UpstreamClient(
                http, base_url="https://www.autodl.art/api/v1/comfyui"
            )
            app_.state.cache = WorkflowCache()
            app_.state.settings = Settings(
                upstream_base_url="https://www.autodl.art/api/v1/comfyui"
            )
            yield

    app.router.lifespan_context = lifespan

    @asynccontextmanager
    async def scope():
        async with app.router.lifespan_context(app):
            yield

    async with scope():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get(
                "/v1/videos/t/content",
                headers={"Authorization": "Bearer x"},
                follow_redirects=False,
            )
    assert r.status_code == 404


# ---------------------------------------------------------------- schema


async def test_workflow_schema(client):
    r = await client.get(
        "/v1/workflows/wf-aaa/schema",
        headers={"Authorization": "Bearer raw-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "wf-aaa"
    assert body["name"] == "Image To Video 5s"
    assert "480p横" in body["resolution_labels"]
    assert "ref_image_0" in body["required_fields"]


async def test_workflow_schema_unknown(client):
    r = await client.get(
        "/v1/workflows/wf-does-not-exist/schema",
        headers={"Authorization": "Bearer raw-token"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------- log_upstream_responses switch


async def _make_client_with_settings(app, settings_kwargs):
    """Build an httpx AsyncClient backed by the existing mock transport
    but with a custom ``Settings`` instance.

    Used by the ``LOG_UPSTREAM_RESPONSES`` tests below — they need to
    toggle that setting on/off without rebuilding the whole app from
    scratch.
    """

    from httpx import ASGITransport, AsyncClient

    settings = Settings(
        upstream_base_url="https://www.autodl.art/api/v1/comfyui",
        log_level="DEBUG",
        **settings_kwargs,
    )
    transport = httpx.MockTransport(_record)

    @asynccontextmanager
    async def lifespan(app_):
        async with httpx.AsyncClient(transport=transport) as http:
            app_.state.http = http
            app_.state.upstream = UpstreamClient(
                http, base_url=settings.upstream_base_url
            )
            app_.state.cache = WorkflowCache()
            app_.state.settings = settings
            yield

    app.router.lifespan_context = lifespan

    @asynccontextmanager
    async def scope():
        async with app.router.lifespan_context(app):
            yield

    return scope()


async def test_log_upstream_responses_emits_digest_on_submit(app, caplog):
    """With ``LOG_UPSTREAM_RESPONSES=1`` a submit() call records an
    ``upstream_response`` DEBUG log carrying task_id / status / model."""

    caplog.set_level("DEBUG", logger="autodl-openai-gateway")

    async with await _make_client_with_settings(
        app, {"log_upstream_responses": True}
    ):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.post(
                "/v1/videos",
                headers={"Authorization": "Bearer raw-token"},
                json={
                    "model": "wf-aaa",
                    "prompt": "log submit",
                    "size": "720x1280",
                },
            )
    assert r.status_code == 202

    digest_records = [
        rec for rec in caplog.records
        if rec.name == "autodl-openai-gateway"
        and getattr(rec, "msg", None) == "upstream_response"
        and getattr(rec, "endpoint", None) == "submit"
    ]
    assert len(digest_records) == 1
    rec = digest_records[0]
    assert rec.levelname == "DEBUG"
    assert rec.task_id == "task-xyz"
    assert rec.status == "QUEUED"
    assert rec.endpoint == "submit"
    assert rec.path == "/v1/videos"


async def test_log_upstream_responses_emits_digest_on_retrieve(app, caplog):
    caplog.set_level("DEBUG", logger="autodl-openai-gateway")

    async with await _make_client_with_settings(
        app, {"log_upstream_responses": True}
    ):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/v1/videos/task-xyz",
                headers={"Authorization": "Bearer raw-token"},
            )
    assert r.status_code == 200

    digest_records = [
        rec for rec in caplog.records
        if getattr(rec, "msg", None) == "upstream_response"
        and getattr(rec, "endpoint", None) == "retrieve"
    ]
    assert len(digest_records) == 1
    rec = digest_records[0]
    # The mocked retrieve response carries a video URL.
    assert rec.result_count == 1
    assert rec.result_urls == ["https://cdn.autodl.example/r/abc.mp4"]
    assert rec.status == "SUCCESS"


async def test_log_upstream_responses_off_by_default(app, caplog):
    """Without the switch, no ``upstream_response`` digest is emitted —
    keeping the normal log output quiet."""

    caplog.set_level("DEBUG", logger="autodl-openai-gateway")

    async with await _make_client_with_settings(
        app, {"log_upstream_responses": False}
    ):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.post(
                "/v1/videos",
                headers={"Authorization": "Bearer raw-token"},
                json={
                    "model": "wf-aaa",
                    "prompt": "no log",
                    "size": "720x1280",
                },
            )
    assert r.status_code == 202

    digest_records = [
        rec for rec in caplog.records
        if getattr(rec, "msg", None) == "upstream_response"
    ]
    assert digest_records == []


async def test_log_upstream_responses_does_not_dump_results_payload(app, caplog):
    """A base64 / large payload inside ``results`` must NEVER appear in
    the log line — only the count and URLs do."""

    import base64

    # Patch the mock to return a fat base64 payload in results.
    def fat_record(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, Any] | None = None
        if request.content and request.method in {"POST", "PUT", "PATCH"}:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        UPSTREAM_CALLS.append((request.method, str(request.url), body, dict(request.headers)))

        if path.endswith("/api/v1/comfyui/workflows") and request.method == "POST":
            return httpx.Response(
                200,
                json={"code": "Success", "data": {"list": [{"uuid": "wf-aaa", "name": "X"}]}},
                request=request,
            )
        if path.startswith("/api/v1/comfyui/workflows/") and "/comfyui_workflow/" not in path:
            return httpx.Response(200, json=SCHEMAS["wf-aaa"], request=request)
        if path.startswith("/api/v1/comfyui/comfyui_workflow/") and "/result/" not in path:
            return httpx.Response(
                200,
                json={"code": "Success", "data": {"task_id": "task-xyz", "status": "QUEUED"}},
                request=request,
            )
        if "/api/v1/comfyui/comfyui_workflow/result/" in path:
            payload = "A" * 5_000  # a pretend big base64 thumbnail
            return httpx.Response(
                200,
                json={
                    "task_id": "task-xyz",
                    "status": "SUCCESS",
                    "results": [
                        {"url": "https://cdn.autodl.example/r/abc.mp4",
                         "thumbnail_base64": payload},
                    ],
                    "internal_blob": {"token": "secret-xyz"},
                },
                request=request,
            )
        return httpx.Response(404, json={"code": "NotFound"}, request=request)

    caplog.set_level("DEBUG", logger="autodl-openai-gateway")

    transport = httpx.MockTransport(fat_record)

    @asynccontextmanager
    async def lifespan(app_):
        async with httpx.AsyncClient(transport=transport) as http:
            app_.state.http = http
            app_.state.upstream = UpstreamClient(
                http, base_url="https://www.autodl.art/api/v1/comfyui"
            )
            app_.state.cache = WorkflowCache()
            app_.state.settings = Settings(
                upstream_base_url="https://www.autodl.art/api/v1/comfyui",
                log_level="DEBUG",
                log_upstream_responses=True,
            )
            yield

    app.router.lifespan_context = lifespan

    from httpx import ASGITransport, AsyncClient

    @asynccontextmanager
    async def scope():
        async with app.router.lifespan_context(app):
            yield

    async with scope():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                "/v1/videos/task-xyz",
                headers={"Authorization": "Bearer raw-token"},
            )
    assert r.status_code == 200

    retrieve_digests = [
        rec for rec in caplog.records
        if getattr(rec, "msg", None) == "upstream_response"
        and getattr(rec, "endpoint", None) == "retrieve"
    ]
    assert len(retrieve_digests) == 1
    rec = retrieve_digests[0]
    assert rec.result_count == 1
    assert rec.result_urls == ["https://cdn.autodl.example/r/abc.mp4"]
    # base64 thumbnail must not appear in any field attached to the log.
    for attr in ("thumbnail_base64", "internal_blob"):
        assert getattr(rec, attr, None) is None
    assert "A" * 100 not in rec.getMessage()
    # extra_keys lists the unknown field by name only.
    assert "internal_blob" in rec.extra_keys
    # secret-xyz is the value of the nested field; never logged.
    assert "secret-xyz" not in rec.getMessage()


# ---------------------------------------------------------------- healthz


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
