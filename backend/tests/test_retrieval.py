"""Retrieval must only ever return chunks from collections the user can read."""
from app import retrieval
from app.config import settings
from tests.conftest import SAMPLES, auth, upload

ENGINEERING, FINANCE = 1, 2


def test_search_never_crosses_collections(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [0.1] * settings.embed_dim)
    alice, bob, admin = (auth(client, e) for e in ("alice@example.com", "bob@example.com", "admin@example.com"))
    upload(client, alice, ENGINEERING, SAMPLES / "api_authentication_guide.docx")
    upload(client, bob, FINANCE, SAMPLES / "expense_policy.txt")

    def files(headers, **params):
        r = client.get("/api/search", headers=headers, params={"q": "anything", "k": 20, **params})
        assert r.status_code == 200, r.text
        return {h["filename"] for h in r.json()}

    assert files(alice) == {"api_authentication_guide.docx"}
    assert files(bob) == {"expense_policy.txt"}
    assert files(bob, collection_id=ENGINEERING) == set()       # asking for a forbidden collection gives nothing
    assert files(admin) == {"api_authentication_guide.docx", "expense_policy.txt"}


def test_search_skips_documents_that_are_not_ready(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [0.1] * settings.embed_dim)
    alice = auth(client, "alice@example.com")
    doc_id = upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt").json()["id"]
    with client.db() as db:
        from app.models import Document
        db.get(Document, doc_id).status = "embedding"   # e.g. being re-indexed
        db.commit()
    assert client.get("/api/search", headers=alice, params={"q": "x"}).json() == []
