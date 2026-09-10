"""Unit tests for the pure mapping helpers."""

from __future__ import annotations

from app.mapping import (
    build_upstream_body,
    coerce_field_types,
    expand_multipart_files,
    extract_result_urls,
    map_size_to_resolution,
    map_status,
    video_object,
)


# ---------------------------------------------------------------- map_status


def test_map_status_queued_variants():
    assert map_status("QUEUED") == "queued"
    assert map_status("queued") == "queued"
    assert map_status("PENDING") == "queued"
    assert map_status(None) == "queued"
    assert map_status("") == "queued"


def test_map_status_running_variants():
    assert map_status("RUNNING") == "in_progress"
    assert map_status("running") == "in_progress"
    assert map_status("PROCESSING") == "in_progress"
    assert map_status("IN_PROGRESS") == "in_progress"


def test_map_status_terminal_success():
    assert map_status("SUCCESS") == "completed"
    assert map_status("SUCCEEDED") == "completed"
    assert map_status("COMPLETED") == "completed"


def test_map_status_terminal_failure():
    assert map_status("FAILED") == "failed"
    assert map_status("FAILURE") == "failed"
    assert map_status("ERROR") == "failed"
    assert map_status("CANCELLED") == "failed"
    assert map_status("CANCELED") == "failed"


def test_map_status_unknown_non_terminal():
    # Anything we don't recognise should keep clients polling.
    assert map_status("STARTING") == "in_progress"
    assert map_status("WARMING_UP") == "in_progress"


# ---------------------------------------------------------------- build_upstream_body


def test_build_upstream_body_drops_model():
    out = build_upstream_body({"model": "wf-1", "prompt": "x"})
    assert "model" not in out
    assert out == {"prompt": "x"}


def test_build_upstream_body_seconds_to_duration():
    out = build_upstream_body({"model": "wf", "prompt": "x", "seconds": 5})
    assert out["duration"] == 5
    assert "seconds" not in out


def test_build_upstream_body_size_to_resolution():
    out = build_upstream_body({"model": "wf", "prompt": "x", "size": "1280x720"})
    assert out["resolution"] == "1280x720"
    assert "size" not in out


def test_build_upstream_body_explicit_wins():
    body = {
        "model": "wf",
        "prompt": "x",
        "seconds": 5,
        "duration": 12,
        "size": "640x360",
        "resolution": "1920x1080",
    }
    out = build_upstream_body(body)
    assert out["duration"] == 12
    assert out["resolution"] == "1920x1080"


def test_build_upstream_body_metadata_merge():
    body = {
        "model": "wf",
        "prompt": "x",
        "metadata": {"seed": 42, "image_url": "https://x.test/r.png"},
    }
    out = build_upstream_body(body)
    assert out["seed"] == 42
    assert out["image_url"] == "https://x.test/r.png"
    assert "metadata" not in out


def test_build_upstream_body_metadata_collision_loses():
    body = {"model": "wf", "prompt": "x", "metadata": {"prompt": "overwritten"}}
    out = build_upstream_body(body)
    assert out["prompt"] == "x"


def test_build_upstream_body_metadata_non_dict_ignored():
    body = {"model": "wf", "prompt": "x", "metadata": "not-a-dict"}
    out = build_upstream_body(body)
    assert out == {"prompt": "x"}


def test_build_upstream_body_passthrough_extra_fields():
    body = {
        "model": "wf",
        "prompt": "x",
        "reference_image_url": "https://x.test/r.png",
        "image_base64": "data:image/png;base64,AAAA",
        "negative_prompt": "blurry",
    }
    out = build_upstream_body(body)
    assert out["reference_image_url"] == "https://x.test/r.png"
    assert out["image_base64"] == "data:image/png;base64,AAAA"
    assert out["negative_prompt"] == "blurry"


# ---------------------------------------------------------------- extract_result_urls


def test_extract_result_urls_preferred_keys():
    results = [
        {
            "url": "https://cdn.example/a.png",
            "image_url": "https://cdn.example/b.png",
        }
    ]
    urls = extract_result_urls(results)
    assert urls == ["https://cdn.example/a.png", "https://cdn.example/b.png"]


def test_extract_result_urls_recurses_into_dicts():
    results = {"nested": {"video_url": "https://cdn.example/v.mp4"}}
    assert extract_result_urls(results) == ["https://cdn.example/v.mp4"]


def test_extract_result_urls_recurses_into_lists():
    results = [[{"audio_url": "https://cdn.example/a.mp3"}]]
    assert extract_result_urls(results) == ["https://cdn.example/a.mp3"]


def test_extract_result_urls_dedup():
    results = [
        {"url": "https://cdn.example/a.png"},
        {"extra": {"deep": {"url": "https://cdn.example/a.png"}}},
    ]
    assert extract_result_urls(results) == ["https://cdn.example/a.png"]


def test_extract_result_urls_ignores_non_http():
    results = [
        {"url": "ftp://nope"},
        {"path": "/local/file.png"},
        {"url": "https://cdn.example/ok.png"},
    ]
    assert extract_result_urls(results) == ["https://cdn.example/ok.png"]


def test_extract_result_urls_empty():
    assert extract_result_urls(None) == []
    assert extract_result_urls({}) == []
    assert extract_result_urls([]) == []


# ---------------------------------------------------------------- video_object


def test_video_object_minimal():
    obj = video_object(
        task_id="t-1",
        model="wf",
        upstream={"status": "QUEUED"},
        created_at=1700000000,
    )
    assert obj == {
        "id": "t-1",
        "object": "video",
        "created_at": 1700000000,
        "model": "wf",
        "status": "queued",
        "progress": 0,
    }


def test_video_object_completed_with_url():
    obj = video_object(
        task_id="t-1",
        model="wf",
        upstream={
            "status": "SUCCESS",
            "results": [{"url": "https://cdn.example/v.mp4"}],
            "prompt": "a cat",
            "seconds": 5,
            "size": "1280x720",
            "duration_seconds": 5.7,
        },
        created_at=1700000000,
    )
    assert obj["status"] == "completed"
    assert obj["url"] == "https://cdn.example/v.mp4"
    assert obj["prompt"] == "a cat"
    assert obj["seconds"] == 5
    assert obj["size"] == "1280x720"
    assert obj["metadata"] == {"duration_seconds": 5.7}


def test_video_object_progress_from_upstream():
    obj = video_object(
        task_id="t-1",
        model="wf",
        upstream={"status": "RUNNING", "progress": 0.42},
        created_at=1,
    )
    assert obj["progress"] == 42


def test_video_object_progress_capped():
    obj = video_object(
        task_id="t-1",
        model="wf",
        upstream={"status": "RUNNING", "percent": 250},
        created_at=1,
    )
    assert obj["progress"] == 100


# ---------------------------------------------------------------- map_size_to_resolution

_LABELS = ("480p竖", "768p竖", "480p横", "768p横", "480p(1:1)", "768p(1:1)")


def test_map_size_square_480():
    assert map_size_to_resolution("480x480", _LABELS) == "480p(1:1)"


def test_map_size_square_768():
    assert map_size_to_resolution("768x768", _LABELS) == "768p(1:1)"


def test_map_size_portrait_480():
    assert map_size_to_resolution("496x864", _LABELS) == "480p竖"


def test_map_size_portrait_768():
    assert map_size_to_resolution("1080x1920", _LABELS) == "768p竖"


def test_map_size_landscape_480():
    assert map_size_to_resolution("864x496", _LABELS) == "480p横"


def test_map_size_landscape_768():
    assert map_size_to_resolution("1920x1080", _LABELS) == "768p横"


def test_map_size_already_a_label_returns_none():
    # If the value is already a label, the caller should not call this;
    # but if it does, no "WxH" parse → None.
    assert map_size_to_resolution("480p竖", _LABELS) is None


def test_map_size_garbage_returns_none():
    assert map_size_to_resolution("not-a-size", _LABELS) is None
    assert map_size_to_resolution("", _LABELS) is None


def test_map_size_no_matching_label_returns_none():
    # Square size but the whitelist only has landscape labels → no match.
    assert map_size_to_resolution("2048x2048", ("768p横",)) is None
    # Empty whitelist → None.
    assert map_size_to_resolution("480x480", ()) is None


def test_map_size_dynamic_tier_736p():
    # minimax_h3_b99_001 only offers 736p tiers.
    labels = ("736p竖", "736p横", "736p(1:1)")
    assert map_size_to_resolution("480x480", labels) == "736p(1:1)"
    assert map_size_to_resolution("496x864", labels) == "736p竖"
    assert map_size_to_resolution("1280x720", labels) == "736p横"


def test_coerce_field_types_integer_and_float():
    body = {"duration": "6", "seed": "42", "prompt": "hi", "resolution": "736p竖"}
    field_types = {"duration": "integer", "seed": "integer", "prompt": "string", "resolution": "enum"}
    coerce_field_types(body, field_types)
    assert body["duration"] == 6
    assert body["seed"] == 42
    assert body["prompt"] == "hi"          # string untouched
    assert body["resolution"] == "736p竖"  # enum untouched


def test_coerce_field_types_float():
    body = {"cfg": "7.5"}
    coerce_field_types(body, {"cfg": "float"})
    assert body["cfg"] == 7.5


def test_coerce_field_types_invalid_kept():
    body = {"duration": "abc"}
    coerce_field_types(body, {"duration": "integer"})
    assert body["duration"] == "abc"  # unconvertible -> left as-is


def test_coerce_field_types_missing_key_untouched():
    body = {"prompt": "hi"}
    coerce_field_types(body, {"duration": "integer"})
    assert body == {"prompt": "hi"}


# ---------------------------------------------------------------- expand_multipart_files


def _png_bytes() -> bytes:
    # 1x1 transparent PNG, smallest valid PNG (~70 bytes).
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae"
        "426082"
    )


def test_expand_multipart_files_passthrough_without_files():
    out = expand_multipart_files({"model": "wf", "prompt": "x"})
    assert out == {"model": "wf", "prompt": "x"}


def test_expand_multipart_files_strips_placeholder():
    out = expand_multipart_files(
        {
            "model": "wf",
            "prompt": "x",
            "__form_files__": [
                {
                    "field": "input_reference[]",
                    "filename": "a.png",
                    "content_type": "image/png",
                    "size": 1,
                    "data": b"x",
                }
            ],
        }
    )
    assert "__form_files__" not in out


def test_expand_multipart_files_single_image_becomes_ref_image_0():
    png = _png_bytes()
    out = expand_multipart_files(
        {
            "model": "wf",
            "prompt": "x",
            "__form_files__": [
                {
                    "field": "input_reference[]",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size": len(png),
                    "data": png,
                }
            ],
        }
    )
    assert set(out.keys()) == {"model", "prompt", "ref_image_0"}
    import base64

    expected = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    assert out["ref_image_0"] == expected


def test_expand_multipart_files_multiple_images_numbered_in_order():
    png_a = _png_bytes()
    png_b = b"\x89PNG\r\n\x1a\n" + b"second-image-bytes"
    out = expand_multipart_files(
        {
            "model": "wf",
            "__form_files__": [
                {
                    "field": "input_reference[]",
                    "filename": "a.png",
                    "content_type": "image/png",
                    "size": len(png_a),
                    "data": png_a,
                },
                {
                    "field": "input_reference[]",
                    "filename": "b.jpg",
                    "content_type": "image/jpeg",
                    "size": len(png_b),
                    "data": png_b,
                },
            ],
        }
    )
    import base64

    assert "ref_image_0" in out and "ref_image_1" in out
    assert out["ref_image_0"].startswith("data:image/png;base64,")
    assert out["ref_image_1"].startswith("data:image/jpeg;base64,")
    assert png_a in base64.b64decode(out["ref_image_0"].split(",", 1)[1])
    assert png_b in base64.b64decode(out["ref_image_1"].split(",", 1)[1])


def test_expand_multipart_files_ignores_unrelated_file_fields():
    """A file with a non-reference field name (e.g. a side-channel
    attachment) must NOT become a ref_image_<N>. The whitelist filter
    on the upstream side would drop it anyway, but expander must be
    defensive too."""

    png = _png_bytes()
    out = expand_multipart_files(
        {
            "__form_files__": [
                {
                    "field": "random_other[]",
                    "filename": "x.png",
                    "content_type": "image/png",
                    "size": len(png),
                    "data": png,
                }
            ],
        }
    )
    assert "ref_image_0" not in out
    assert "__form_files__" not in out


def test_expand_multipart_files_accepts_both_field_names():
    """``input_reference[]`` (browser form) and ``input_reference``
    (single-file) both count, concatenated in field order."""

    out = expand_multipart_files(
        {
            "__form_files__": [
                {
                    "field": "input_reference",
                    "filename": "a.png",
                    "content_type": "image/png",
                    "size": 1,
                    "data": b"A",
                },
                {
                    "field": "input_reference[]",
                    "filename": "b.png",
                    "content_type": "image/png",
                    "size": 1,
                    "data": b"B",
                },
            ],
        }
    )
    assert "ref_image_0" in out and "ref_image_1" in out


def test_expand_multipart_files_falls_back_to_png_when_mime_missing():
    out = expand_multipart_files(
        {
            "__form_files__": [
                {
                    "field": "input_reference[]",
                    "filename": "noext",
                    "content_type": None,
                    "size": 1,
                    "data": b"\x89",
                }
            ],
        }
    )
    assert out["ref_image_0"].startswith("data:image/png;base64,")


def test_expand_multipart_files_skips_empty_data():
    out = expand_multipart_files(
        {
            "__form_files__": [
                {
                    "field": "input_reference[]",
                    "filename": "empty.png",
                    "content_type": "image/png",
                    "size": 0,
                    "data": b"",
                }
            ],
        }
    )
    assert "ref_image_0" not in out
