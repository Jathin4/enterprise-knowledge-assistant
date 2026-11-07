"""Find the chunks closest in meaning to a question, among documents the user is allowed to read."""
from dataclasses import asdict, dataclass, replace

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.config import settings
from app.ingestion import embed_batch
from app.models import Document, DocumentChunk, User
from app.security import readable_collection_ids

QUERY_PREFIX = "search_query: "  # nomic-embed-text task prefix for questions


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    filename: str
    page: int | None
    heading: str | None
    content: str
    distance: float
    chunk_index: int | None = None
    neighbour_id: int | None = None   # the following chunk, when it was added to this one

    def to_dict(self) -> dict:
        return asdict(self)


def embed_query(question: str) -> list[float]:
    return embed_batch([QUERY_PREFIX + question])[0]


def search(db: Session, allowed_collection_ids: list[int], query_vector: list[float], k: int) -> list[Hit]:
    """Top-k chunks by cosine distance, only from ready documents in the allowed collections."""
    if not allowed_collection_ids:
        return []
    distance = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")
    rows = db.execute(
        select(DocumentChunk.id, Document.id, Document.filename, DocumentChunk.page_number,
               DocumentChunk.heading, DocumentChunk.content, distance, DocumentChunk.chunk_index)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.collection_id.in_(allowed_collection_ids), Document.status == "ready")
        .order_by(distance)
        .limit(k)
    ).all()
    return [Hit(*row) for row in rows]


def with_neighbours(db: Session, hits: list[Hit], allow=lambda text: True) -> list[Hit]:
    """Add the passage that follows each hit, so a list or rule split across two chunks arrives whole.
    Only within the same section and page, so every citation's heading and page stay exact (gluing the
    next section on hid it under the wrong heading, and a report then called it missing). A following passage that
    fails `allow` (e.g. a prompt-injection check) is simply not attached."""
    wanted = {(h.document_id, h.chunk_index + 1) for h in hits if h.chunk_index is not None}
    if not wanted:
        return hits
    rows = db.execute(select(DocumentChunk.id, DocumentChunk.document_id, DocumentChunk.chunk_index,
                             DocumentChunk.page_number, DocumentChunk.heading, DocumentChunk.content)
                      .where(tuple_(DocumentChunk.document_id, DocumentChunk.chunk_index).in_(wanted))).all()
    following = {(r.document_id, r.chunk_index): r for r in rows}
    already = {h.chunk_id for h in hits}
    expanded = []
    for h in hits:
        nxt = following.get((h.document_id, (h.chunk_index or 0) + 1)) if h.chunk_index is not None else None
        if (nxt is None or nxt.id in already or nxt.page_number != h.page or nxt.heading != h.heading
                or not allow(nxt.content)):
            expanded.append(h)
            continue
        text = nxt.content
        if nxt.heading and text.startswith(nxt.heading):
            text = text[len(nxt.heading):].lstrip()     # don't repeat the same heading
        expanded.append(replace(h, content=f"{h.content}\n\n{text}", neighbour_id=nxt.id))
    return expanded


def retrieve(db: Session, user: User, question: str, k: int | None = None,
             collection_id: int | None = None) -> list[Hit]:
    """Permission check happens here, in code, before any text can reach the LLM."""
    allowed = readable_collection_ids(db, user)
    if collection_id is not None:
        allowed = [c for c in allowed if c == collection_id]
    return search(db, allowed, embed_query(question), k or settings.top_k)
