import os

# Set test credentials before any `backend.*` module is imported, since
# backend.config builds its Settings() singleton at import time and would
# otherwise read real values out of a developer's local .env file.
os.environ["YOUTUBE_API_KEY"] = "test-youtube-api-key"
os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-api-key"
os.environ["JWT_SECRET_KEY"] = "0123456789" * 4  # 40 chars, passes the >=32 validator
os.environ["CORS_ORIGINS"] = "*"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import get_db
from backend.main import app, limiter
from backend.models_db import Base

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(bind=test_engine)


def _override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(autouse=True)
def _clean_state():
    """Give every test a fresh database and a reset rate limiter."""
    Base.metadata.create_all(bind=test_engine)
    limiter.reset()
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()
