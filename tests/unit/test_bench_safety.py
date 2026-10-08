from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from bench_admission import client_from_environment  # noqa: E402


@pytest.mark.parametrize("name,value", [
    ("DEPLOY_ENV", "prod"),
    ("TEST_ES_HOST", "other.elasticsearch.aliyuncs.com"),
    ("TEST_ES_PORT", "9201"),
    ("TEST_ES_SCHEME", "https"),
])
def test_benchmark_refuses_any_non_approved_endpoint(monkeypatch, name, value):
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", "es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com")
    monkeypatch.setenv("TEST_ES_PORT", "9200")
    monkeypatch.setenv("TEST_ES_SCHEME", "http")
    monkeypatch.setenv("TEST_ES_USER", "fixture-user")
    monkeypatch.setenv("TEST_ES_PASS", "fixture-password")
    monkeypatch.setenv(name, value)
    with pytest.raises((RuntimeError, ValueError)):
        client_from_environment()
