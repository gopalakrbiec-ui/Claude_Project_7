from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Provide minimal env so Settings can be instantiated without a real .env file.
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("RAZORPAY_KEY_ID", "test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")


@pytest.fixture()
def client():
    # Import after env vars are set so Settings loads correctly.
    from app.main import create_app

    return TestClient(create_app())


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "env" in body
