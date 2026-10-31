"""API tests: upload → ready, validation, permissions (IDOR), delete, failures."""
from pathlib import Path

from sqlalchemy import func, select

from app import ingestion
from app.config import settings
from app.models import AuditEvent, DocumentChunk
from tests.conftest import SAMPLES, auth, upload

ENGINEERING, FINANCE = 1, 2


def test_requires_login(client):
    assert client.get("/api/documents").status_code == 401
    r = client.post("/api/auth/login", data={"username": "alice@example.com", "password": "wrong-password"})
    assert r.status_code == 401


def test_upload_all_three_types_reach_ready(client):
    alice = auth(client, "alice@example.com")
    for name in ("api_authentication_guide.docx", "information_security_policy.pdf", "expense_policy.txt"):
        r = upload(client, alice, ENGINEERING, SAMPLES / name)
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "queued"
        doc = client.get(f"/api/documents/{r.json()['id']}", headers=alice).json()
        assert doc["status"] == "ready", doc
        assert doc["chunk_count"] > 0

    with client.db() as db:
        pdf_pages = db.scalars(select(DocumentChunk.page_number).where(DocumentChunk.document_id == 2)).all()
        assert set(pdf_pages) == {1, 2, 3}
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "document.upload")) == 3


def test_stored_under_random_name(client):
    alice = auth(client, "alice@example.com")
    r = upload(client, alice, ENGINEERING, name="..\\..\\evil.txt", data=b"Hello there.")
    assert r.json()["filename"] == "evil.txt"
    stored = list(Path(settings.upload_dir).iterdir())
    assert len(stored) == 1 and stored[0].name != "evil.txt" and stored[0].suffix == ".txt"


def test_validation_errors(client, monkeypatch):
    alice = auth(client, "alice@example.com")
    assert upload(client, alice, ENGINEERING, name="x.exe", data=b"MZ\x90\x00").status_code == 415
    assert upload(client, alice, ENGINEERING, name="empty.txt", data=b"").status_code == 400
    assert upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt").status_code == 202
    assert upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt").status_code == 409  # duplicate
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    assert upload(client, alice, ENGINEERING, name="big.txt", data=b"x").status_code == 413


def test_cannot_upload_into_other_collection(client):
    alice = auth(client, "alice@example.com")
    assert upload(client, alice, FINANCE, SAMPLES / "expense_policy.txt").status_code == 404


def test_users_only_see_their_collections(client):
    alice, bob, admin = (auth(client, e) for e in ("alice@example.com", "bob@example.com", "admin@example.com"))
    doc_id = upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt").json()["id"]

    assert [c["name"] for c in client.get("/api/collections", headers=bob).json()] == ["Finance"]
    assert client.get("/api/documents", headers=bob).json() == []
    assert client.get(f"/api/documents/{doc_id}", headers=bob).status_code == 404     # IDOR blocked
    assert client.delete(f"/api/documents/{doc_id}", headers=bob).status_code == 404
    assert client.get(f"/api/documents/{doc_id}", headers=admin).status_code == 200   # admin sees all


def test_delete_removes_chunks_and_file(client):
    alice = auth(client, "alice@example.com")
    doc_id = upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt").json()["id"]
    assert client.delete(f"/api/documents/{doc_id}", headers=alice).status_code == 204
    assert client.get(f"/api/documents/{doc_id}", headers=alice).status_code == 404
    with client.db() as db:
        assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert list(Path(settings.upload_dir).iterdir()) == []


def test_embedding_failure_marks_document_failed(client, monkeypatch):
    def broken(texts):
        raise ConnectionError("Ollama is not running")
    monkeypatch.setattr(ingestion, "embed_texts", broken)
    alice = auth(client, "alice@example.com")
    doc_id = upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt").json()["id"]
    doc = client.get(f"/api/documents/{doc_id}", headers=alice).json()
    assert doc["status"] == "failed"
    assert "Ollama is not running" in doc["error"]
    with client.db() as db:
        assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_title_is_embedded_but_not_stored(client, monkeypatch):
    seen = []
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: seen.extend(texts) or [[0.1] * settings.embed_dim for _ in texts])
    monkeypatch.setattr(settings, "embed_with_title", True)
    alice = auth(client, "alice@example.com")
    upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt")
    assert seen and all(t.startswith("search_document: expense policy\n") for t in seen)
    with client.db() as db:
        assert not any(c.startswith("expense policy") for c in db.scalars(select(DocumentChunk.content)))


def test_pdf_without_text_fails_clearly(client):
    from pypdf import PdfWriter
    import io
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    alice = auth(client, "alice@example.com")
    doc_id = upload(client, alice, ENGINEERING, name="scan.pdf", data=buf.getvalue()).json()["id"]
    doc = client.get(f"/api/documents/{doc_id}", headers=alice).json()
    assert doc["status"] == "failed" and "OCR" in doc["error"]
