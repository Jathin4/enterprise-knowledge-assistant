"""Retrieval must only ever return chunks from collections the user can read."""
from sqlalchemy import select

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


def test_next_passage_is_added_on_the_same_page_only(client):
    from app.models import Document, DocumentChunk
    from app.retrieval import Hit, with_neighbours
    with client.db() as db:
        doc = Document(owner_id=1, collection_id=ENGINEERING, filename="list.pdf", file_type="pdf", size_bytes=1,
                       checksum="x", storage_path="x", status="ready")
        db.add(doc); db.flush()
        for i, (page, heading, text) in enumerate([(1, "2.4", "Three models: central,"), (1, "2.4", "distributed, coordinating."),
                                                   (2, "2.4", "Next page."), (2, "2.5", "Next section.")]):
            db.add(DocumentChunk(document_id=doc.id, chunk_index=i, page_number=page, heading=heading, content=text,
                                 embedding=[0.1] * settings.embed_dim))
        db.commit()
        ids = db.scalars(select(DocumentChunk.id).order_by(DocumentChunk.chunk_index)).all()
        hit = lambda i, page: Hit(ids[i], doc.id, "list.pdf", page, "2.4", "x", 0.1, chunk_index=i)
        first, second = with_neighbours(db, [hit(0, 1)])[0], with_neighbours(db, [hit(1, 1)])[0]
        assert first.content == "x\n\ndistributed, coordinating." and first.neighbour_id == ids[1]
        assert second.content == "x" and second.neighbour_id is None      # next chunk is on page 2
        assert with_neighbours(db, [hit(2, 2)])[0].neighbour_id is None           # next chunk is another section
        assert with_neighbours(db, [hit(0, 1), Hit(ids[1], doc.id, "list.pdf", 1, "2.4", "y", 0.1, chunk_index=1)])[0].neighbour_id is None  # already a hit
