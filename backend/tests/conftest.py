"""Test setup: a separate database (knowledge_assistant_test), fake embeddings, temp upload folder."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import ingestion
from app.config import settings
from app.db import Base
from app.models import Collection, CollectionMember, User
from app.security import hash_password

SAMPLES = Path(__file__).resolve().parents[2] / "sample-docs"
PASSWORD = "correct-horse-battery"
TEST_DB = "knowledge_assistant_test"


@pytest.fixture(scope="session")
def engine():
    url = make_url(settings.database_url)
    server = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with server.connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}):
            c.execute(text(f"CREATE DATABASE {TEST_DB}"))
    server.dispose()

    eng = create_engine(url.set(database=TEST_DB))
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def fake_embed(texts):
    return [[0.1] * settings.embed_dim for _ in texts]


@pytest.fixture
def client(engine, monkeypatch, tmp_path):
    with engine.begin() as c:  # every test starts from empty tables
        c.execute(text("TRUNCATE " + ", ".join(t.name for t in Base.metadata.sorted_tables)
                       + " RESTART IDENTITY CASCADE"))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", session_factory)
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setattr(ingestion, "embed_texts", fake_embed)

    with session_factory() as db:  # alice in Engineering, bob in Finance, plus an admin
        eng, fin = Collection(name="Engineering"), Collection(name="Finance")
        users = [User(email=e, password_hash=hash_password(PASSWORD), role=r)
                 for e, r in (("alice@example.com", "employee"), ("bob@example.com", "employee"),
                              ("admin@example.com", "admin"))]
        db.add_all([eng, fin, *users])
        db.flush()
        db.add_all([CollectionMember(collection_id=eng.id, user_id=users[0].id),
                    CollectionMember(collection_id=fin.id, user_id=users[1].id)])
        db.commit()

    from app.main import app
    with TestClient(app) as c:
        c.db = session_factory
        yield c


def auth(client, email):
    r = client.post("/api/auth/login", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def upload(client, headers, collection_id, path=None, name=None, data=None):
    files = {"file": (name or path.name, data if data is not None else path.read_bytes())}
    return client.post("/api/documents", headers=headers, data={"collection_id": collection_id}, files=files)
