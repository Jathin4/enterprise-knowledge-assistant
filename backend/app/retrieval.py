"""Find the chunks closest in meaning to a question, among documents the user is allowed to read."""
from dataclasses import asdict, dataclass

from sqlalchemy import select
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
               DocumentChunk.heading, DocumentChunk.content, distance)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.collection_id.in_(allowed_collection_ids), Document.status == "ready")
        .order_by(distance)
        .limit(k)
    ).all()
    return [Hit(*row) for row in rows]


def retrieve(db: Session, user: User, question: str, k: int | None = None,
             collection_id: int | None = None) -> list[Hit]:
    """Permission check happens here, in code, before any text can reach the LLM."""
    allowed = readable_collection_ids(db, user)
    if collection_id is not None:
        allowed = [c for c in allowed if c == collection_id]
    return search(db, allowed, embed_query(question), k or settings.top_k)
