"""Unit tests for the pure mapping helpers."""

from __future__ import annotations

from app.mapping import (
    build_upstream_body,
    coerce_field_types,
    expand_multipart_files,
    expand_references,
    extract_result_urls,
    map_size_to_resolution,
    map_status,
    normalize_references,
    summarize_upstream_response,
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
    # `image_url` is now normalised to `ref_image_0` so the whitelist
    # filter doesn't drop the reference image. The original alias key
    # is gone — that's the point of the fix.
    assert "image_url" not in out
    assert out["ref_image_0"] == "https://x.test/r.png"
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
    # Reference-image aliases are now normalised into ``ref_image_<N>``
    # slots so the whitelist filter can't silently drop them.
    assert "reference_image_url" not in out
    assert "image_base64" not in out
    assert out["ref_image_0"] == "https://x.test/r.png"
    assert out["ref_image_1"] == "data:image/png;base64,AAAA"
    # Genuinely orthogonal fields still pass through unchanged.
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


# ---------------------------------------------------------------- expand_references


def test_expand_references_missing_key_returns_copy():
    out = expand_references({"model": "wf", "prompt": "x"})
    assert out == {"model": "wf", "prompt": "x"}
    # defensive: must be a new dict so callers don't mutate the input
    assert out is not {"model": "wf", "prompt": "x"}


def test_expand_references_single_url_becomes_ref_image_0():
    out = expand_references(
        {
            "model": "wf",
            "prompt": "x",
            "references": ["https://cdn.example/a.png"],
        }
    )
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert "references" not in out


def test_expand_references_multiple_urls_numbered_in_order():
    out = expand_references(
        {
            "model": "wf",
            "prompt": "x",
            "references": [
                "https://cdn.example/a.png",
                "https://cdn.example/b.png",
                "data:image/png;base64,AAAA",
            ],
        }
    )
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"
    assert out["ref_image_2"] == "data:image/png;base64,AAAA"
    assert "references" not in out


def test_expand_references_skips_non_string_entries():
    """A misbehaving client may stuff objects/None into the list. The
    helper must ignore them rather than crash or send garbage upstream."""

    out = expand_references(
        {
            "model": "wf",
            "references": [
                "https://cdn.example/keep.png",
                None,
                {"url": "https://cdn.example/should-not-leak.png"},
                42,
                "https://cdn.example/keep2.png",
            ],
        }
    )
    assert out["ref_image_0"] == "https://cdn.example/keep.png"
    assert out["ref_image_1"] == "https://cdn.example/keep2.png"
    assert "ref_image_2" not in out
    assert "references" not in out


def test_expand_references_skips_empty_strings():
    out = expand_references(
        {
            "model": "wf",
            "references": ["", "https://cdn.example/keep.png", "   "],
        }
    )
    assert "ref_image_0" in out
    assert out["ref_image_0"] == "https://cdn.example/keep.png"
    # Empty / whitespace entries must not consume a numbered slot.
    assert "ref_image_1" not in out


def test_expand_references_does_not_overwrite_existing_slots():
    """If the caller already supplied ref_image_0 (e.g. via multipart
    upload or hand-written JSON), the references list must continue
    from the next free slot rather than clobbering it."""

    out = expand_references(
        {
            "model": "wf",
            "ref_image_0": "https://already-set.example/a.png",
            "references": [
                "https://cdn.example/b.png",
                "https://cdn.example/c.png",
            ],
        }
    )
    assert out["ref_image_0"] == "https://already-set.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"
    assert out["ref_image_2"] == "https://cdn.example/c.png"


def test_expand_references_empty_list_clears_key():
    out = expand_references({"model": "wf", "references": []})
    assert "references" not in out
    assert out == {"model": "wf"}


def test_expand_references_non_list_value_kept_as_is_then_removed():
    """A non-list ``references`` value is malformed client input —
    we drop the key so the whitelist filter doesn't reject the request
    for an unknown field, but we do NOT try to coerce it into a ref."""

    out = expand_references({"model": "wf", "references": "not-a-list"})
    assert "references" not in out
    assert "ref_image_0" not in out


# ---------------------------------------------------------------- normalize_references


def test_normalize_references_input_reference_string_url():
    """OpenAI official single-reference field, JSON-string URL variant."""

    out = normalize_references(
        {"model": "wf", "input_reference": "https://cdn.example/a.png"}
    )
    assert "input_reference" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"


def test_normalize_references_input_reference_object_with_image_url():
    """OpenAI official JSON object with ``image_url``."""

    out = normalize_references(
        {
            "model": "wf",
            "input_reference": {"image_url": "https://cdn.example/a.png"},
        }
    )
    assert "input_reference" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"


def test_normalize_references_input_reference_object_with_file_id():
    """OpenAI official JSON object with ``file_id`` — we keep the token
    so the upstream error is more actionable than a silent drop."""

    out = normalize_references(
        {"model": "wf", "input_reference": {"file_id": "file_abc123"}}
    )
    assert "input_reference" not in out
    assert out["ref_image_0"] == "file_abc123"


def test_normalize_references_input_reference_object_with_url_key():
    """Some clients wrap the URL under a generic ``url`` key."""

    out = normalize_references(
        {"model": "wf", "input_reference": {"url": "https://cdn.example/a.png"}}
    )
    assert out["ref_image_0"] == "https://cdn.example/a.png"


def test_normalize_references_image_url_string():
    out = normalize_references(
        {"model": "wf", "image_url": "https://cdn.example/a.png"}
    )
    assert "image_url" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"


def test_normalize_references_reference_image_url():
    out = normalize_references(
        {"model": "wf", "reference_image_url": "https://cdn.example/a.png"}
    )
    assert "reference_image_url" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"


def test_normalize_references_image_base64():
    out = normalize_references(
        {"model": "wf", "image_base64": "data:image/png;base64,AAAA"}
    )
    assert "image_base64" not in out
    assert out["ref_image_0"] == "data:image/png;base64,AAAA"


def test_normalize_references_image_string():
    """OpenAI single-image shorthand (``image``)."""

    out = normalize_references(
        {"model": "wf", "image": "https://cdn.example/a.png"}
    )
    assert "image" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"


def test_normalize_references_input_references_array_of_strings():
    out = normalize_references(
        {
            "model": "wf",
            "input_references": [
                "https://cdn.example/a.png",
                "https://cdn.example/b.png",
                "data:image/png;base64,ZZZ",
            ],
        }
    )
    assert "input_references" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"
    assert out["ref_image_2"] == "data:image/png;base64,ZZZ"


def test_normalize_references_input_references_array_of_objects():
    """JSON array of OpenAI-style objects (``image_url`` or ``file_id``)."""

    out = normalize_references(
        {
            "model": "wf",
            "input_references": [
                {"image_url": "https://cdn.example/a.png"},
                {"file_id": "file_abc"},
                {"url": "https://cdn.example/c.png"},
            ],
        }
    )
    assert "input_references" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert out["ref_image_1"] == "file_abc"
    assert out["ref_image_2"] == "https://cdn.example/c.png"


def test_normalize_references_input_reference_brackets_array():
    """The Python openai SDK often emits ``input_reference[]`` as a
    top-level JSON-array name (mirroring the multipart form)."""

    out = normalize_references(
        {
            "model": "wf",
            "input_reference[]": [
                "https://cdn.example/a.png",
                "https://cdn.example/b.png",
            ],
        }
    )
    assert "input_reference[]" not in out
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"


def test_normalize_references_preserves_existing_ref_image_slots():
    """If the caller already wrote a ``ref_image_<N>`` slot, aliases
    fill the *next* free slot — explicit wins."""

    out = normalize_references(
        {
            "model": "wf",
            "ref_image_0": "https://already.example/a.png",
            "input_reference": "https://cdn.example/b.png",
            "image_url": "https://cdn.example/c.png",
        }
    )
    assert out["ref_image_0"] == "https://already.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"
    assert out["ref_image_2"] == "https://cdn.example/c.png"


def test_normalize_references_multiple_single_aliases_become_separate_slots():
    """If a client accidentally sends BOTH ``image_url`` and
    ``image_base64``, both must contribute — they're separate images."""

    out = normalize_references(
        {
            "model": "wf",
            "image_url": "https://cdn.example/a.png",
            "image_base64": "data:image/png;base64,BBBB",
        }
    )
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert out["ref_image_1"] == "data:image/png;base64,BBBB"


def test_normalize_references_skips_empty_string():
    out = normalize_references(
        {"model": "wf", "input_reference": "   "}
    )
    assert "input_reference" not in out
    assert "ref_image_0" not in out


def test_normalize_references_skips_unusable_object():
    """An object with no recognised inner key is silently dropped."""

    out = normalize_references(
        {"model": "wf", "input_reference": {"something_else": "x"}}
    )
    assert "input_reference" not in out
    assert "ref_image_0" not in out


def test_normalize_references_skips_non_string_non_mapping():
    out = normalize_references({"model": "wf", "image_url": 42})
    assert "image_url" not in out
    assert "ref_image_0" not in out


def test_normalize_references_no_aliases_returns_copy():
    body = {"model": "wf", "prompt": "x"}
    out = normalize_references(body)
    assert out == body
    # Defensive: must be a new dict, never the same object.
    assert out is not body


def test_normalize_references_does_not_touch_existing_keys():
    """The helper only removes the alias keys it knows about. Other
    keys — ``prompt``, ``seed``, custom workflow params — must pass
    through verbatim."""

    out = normalize_references(
        {
            "model": "wf",
            "prompt": "x",
            "seed": 7,
            "negative_prompt": "blurry",
            "image_url": "https://cdn.example/a.png",
        }
    )
    assert out["prompt"] == "x"
    assert out["seed"] == 7
    assert out["negative_prompt"] == "blurry"
    assert out["ref_image_0"] == "https://cdn.example/a.png"


# ---------------------------------------------------------------- build_upstream_body reference aliases


def test_build_upstream_body_normalises_input_reference_object_in_metadata():
    """``metadata.input_reference`` must also be normalised — the
    infinite-canvas sometimes wraps its aliases inside ``metadata``."""

    body = {
        "model": "wf",
        "prompt": "x",
        "metadata": {
            "input_reference": {"image_url": "https://cdn.example/a.png"},
            "seed": 99,
        },
    }
    out = build_upstream_body(body)
    assert out["seed"] == 99
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert "input_reference" not in out
    assert "metadata" not in out


def test_build_upstream_body_normalises_top_level_input_reference_array():
    body = {
        "model": "wf",
        "prompt": "x",
        "input_references": [
            "https://cdn.example/a.png",
            {"image_url": "https://cdn.example/b.png"},
        ],
    }
    out = build_upstream_body(body)
    assert out["ref_image_0"] == "https://cdn.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"
    assert "input_references" not in out


def test_build_upstream_body_combines_references_and_input_reference():
    """A request that uses both ``references`` and ``input_reference``
    must combine them into a single contiguous slot block. Single-value
    aliases fill slot 0 first, then ``references`` continues from
    slot 1 — the deterministic order of :data:`_SINGLE_KEYS` and
    :data:`_PLURAL_KEYS` decides it."""

    body = {
        "model": "wf",
        "prompt": "x",
        "references": ["https://cdn.example/a.png"],
        "input_reference": "https://cdn.example/b.png",
    }
    out = build_upstream_body(body)
    assert out["ref_image_0"] == "https://cdn.example/b.png"  # input_reference first
    assert out["ref_image_1"] == "https://cdn.example/a.png"  # references[0] next
    assert "references" not in out
    assert "input_reference" not in out


def test_build_upstream_body_combines_metadata_ref_image_and_top_level_alias():
    """If the caller pre-populates a ``ref_image_<N>`` slot AND uses
    aliases for the others, everything must end up in the right slot
    without collisions."""

    body = {
        "model": "wf",
        "prompt": "x",
        "metadata": {"ref_image_0": "https://already.example/a.png"},
        "image_url": "https://cdn.example/b.png",
    }
    out = build_upstream_body(body)
    assert out["ref_image_0"] == "https://already.example/a.png"
    assert out["ref_image_1"] == "https://cdn.example/b.png"


# ---------------------------------------------------------------- summarize_upstream_response


def test_summarize_response_basic():
    s = summarize_upstream_response(
        {"task_id": "abc", "status": "QUEUED", "model": "wf-x"},
        endpoint="submit",
    )
    assert s["endpoint"] == "submit"
    assert s["task_id"] == "abc"
    assert s["status"] == "QUEUED"
    assert s["model"] == "wf-x"
    assert s["result_count"] == 0
    assert s["result_urls"] == []
    # task_id / status / model are the only "known" keys; no extras.
    assert "extra_keys" not in s


def test_summarize_response_progress_percent_aliases():
    """``progress`` / ``percent`` / ``percentage`` are recognised as the
    progress alias set; ``model`` is its own scalar."""

    s = summarize_upstream_response(
        {"status": "RUNNING", "progress": 50, "model": "wf-x"},
        endpoint="retrieve",
    )
    assert s["status"] == "RUNNING"
    assert s["progress"] == 50
    assert s["model"] == "wf-x"


def test_summarize_response_results_reduced_to_count_and_urls():
    s = summarize_upstream_response(
        {
            "task_id": "abc",
            "status": "SUCCESS",
            "results": [
                {"url": "https://cdn.example/a.mp4", "filename": "a.mp4"},
                {
                    "url": "https://cdn.example/b.mp4",
                    # NEVER log the body / base64 / image_url base64 if any
                    "image_url": "data:image/png;base64,HUGE_PAYLOAD",
                },
            ],
        },
        endpoint="retrieve",
    )
    assert s["result_count"] == 2
    assert s["result_urls"] == [
        "https://cdn.example/a.mp4",
        "https://cdn.example/b.mp4",
    ]
    # Base64 payload must not appear anywhere in the summary.
    assert "HUGE_PAYLOAD" not in repr(s)
    assert "filename" not in s


def test_summarize_response_results_non_list_counts_as_one():
    """A single result object (not wrapped in a list) still produces
    a count of 1."""

    s = summarize_upstream_response(
        {
            "status": "SUCCESS",
            "results": {"url": "https://cdn.example/a.mp4"},
        },
        endpoint="retrieve",
    )
    assert s["result_count"] == 1
    assert s["result_urls"] == ["https://cdn.example/a.mp4"]


def test_summarize_response_extra_keys_listed_by_name_only():
    """Unknown top-level keys are recorded by name but NEVER by value,
    so a future schema addition with base64 / PII never leaks into logs."""

    s = summarize_upstream_response(
        {
            "status": "SUCCESS",
            "secret_blob": "data:image/png;base64,HUGE_PAYLOAD",
            "internal_id": 12345,
            "another_field": {"nested": "value"},
        },
        endpoint="retrieve",
    )
    # Names are listed, sorted, for stable diffing.
    assert s["extra_keys"] == ["another_field", "internal_id", "secret_blob"]
    # Values NEVER appear — base64 / dict payload / PII must not leak.
    assert "HUGE_PAYLOAD" not in repr(s)
    assert "nested" not in repr(s)
    assert "12345" not in repr(s)


def test_summarize_response_unknown_keys_filtered_from_extras():
    """``task_id`` and ``results`` are not in ``extra_keys`` even though
    they're "extra" — they're handled by their dedicated extractors."""

    s = summarize_upstream_response(
        {"task_id": "abc", "results": [], "status": "QUEUED", "prompt": "x"},
        endpoint="submit",
    )
    assert "task_id" not in s.get("extra_keys", [])
    assert "results" not in s.get("extra_keys", [])
    assert "status" not in s.get("extra_keys", [])
    assert "prompt" in s["extra_keys"]


def test_summarize_response_truncates_long_strings():
    """Status / progress / model values longer than 200 chars are
    truncated so a misbehaving upstream never blows up the log file."""

    long_value = "x" * 5_000
    s = summarize_upstream_response(
        {"status": long_value, "model": "wf-x"}, endpoint="submit"
    )
    assert len(s["status"]) <= 201
    assert s["status"].endswith("…")


def test_summarize_response_non_dict_input():
    """Defensive: if upstream somehow returns a non-dict (a bare
    string, list, None), summarise it without crashing."""

    s_none = summarize_upstream_response(None, endpoint="submit")
    assert s_none == {"endpoint": "submit", "type": "NoneType"}

    s_list = summarize_upstream_response([1, 2, 3], endpoint="submit")
    assert s_list == {"endpoint": "submit", "type": "list"}

    s_str = summarize_upstream_response("oops", endpoint="submit")
    assert s_str == {"endpoint": "submit", "type": "str"}


def test_summarize_response_drops_non_scalar_known_keys():
    """If a "known" key carries a non-scalar value (e.g. a dict where
    we'd expect a string status), skip it rather than dumping a dict."""

    s = summarize_upstream_response(
        {"status": {"raw": "SUCCESS"}, "model": "wf-x"},
        endpoint="submit",
    )
    assert "status" not in s
    assert s["model"] == "wf-x"


def test_summarize_response_keeps_explicit_none_for_known_keys():
    """A None on a known key is a real signal ("explicitly no status"),
    so keep it; do not invent a value."""

    s = summarize_upstream_response(
        {"model": None, "status": "QUEUED"}, endpoint="submit"
    )
    assert s["model"] is None
    assert s["status"] == "QUEUED"


def test_summarize_response_is_json_serialisable():
    """The output must round-trip through json.dumps — middleware that
    serialises log records expects this."""

    import json

    s = summarize_upstream_response(
        {
            "task_id": "abc",
            "status": "SUCCESS",
            "progress": 100,
            "results": [{"url": "https://x.test/a.mp4"}],
            "extra_blob": {"nested": "value"},
        },
        endpoint="retrieve",
    )
    encoded = json.dumps(s, ensure_ascii=False, default=str)
    assert "abc" in encoded
    assert "https://x.test/a.mp4" in encoded
    # The nested dict was filtered by name only.
    assert "extra_blob" in encoded  # name only
    assert "nested" not in encoded  # value dropped
