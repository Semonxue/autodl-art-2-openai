"""Tests for the Bearer token extractor."""

from __future__ import annotations

import pytest

from app.auth import extract_bearer_token
from app.errors import AuthError


class _FakeRequest:
    def __init__(self, authorization: str | None):
        self.headers = {} if authorization is None else {"authorization": authorization}


def test_extract_bearer_token_happy_path():
    req = _FakeRequest("Bearer secret-token")
    assert extract_bearer_token(req) == "secret-token"


def test_extract_bearer_token_lowercase_scheme():
    req = _FakeRequest("bearer secret-token")
    assert extract_bearer_token(req) == "secret-token"


def test_extract_bearer_token_extra_whitespace_in_token_trimmed():
    req = _FakeRequest("Bearer   secret-token   ")
    assert extract_bearer_token(req) == "secret-token"


def test_extract_bearer_token_missing_header():
    with pytest.raises(AuthError):
        extract_bearer_token(_FakeRequest(None))


def test_extract_bearer_token_empty_header():
    with pytest.raises(AuthError):
        extract_bearer_token(_FakeRequest(""))


def test_extract_bearer_token_no_scheme():
    with pytest.raises(AuthError):
        extract_bearer_token(_FakeRequest("secret-token"))


def test_extract_bearer_token_wrong_scheme():
    with pytest.raises(AuthError):
        extract_bearer_token(_FakeRequest("Basic dXNlcjpwYXNz"))


def test_extract_bearer_token_empty_after_bearer():
    with pytest.raises(AuthError):
        extract_bearer_token(_FakeRequest("Bearer "))


def test_extract_bearer_token_only_bearer_keyword():
    with pytest.raises(AuthError):
        extract_bearer_token(_FakeRequest("Bearer"))


def test_extract_bearer_token_capitalization_header():
    req = _FakeRequest.__new__(_FakeRequest)
    req.headers = {"Authorization": "Bearer tok"}
    assert extract_bearer_token(req) == "tok"
