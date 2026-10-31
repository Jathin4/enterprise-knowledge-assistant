from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.ingestion import DuplicateDocument, create_document, ingest_document
from app.models import Collection, Document, User
from app.retrieval import retrieve
from app.schemas import CollectionOut, DocumentOut
from app.security import audit, can_delete, get_current_user, get_document_for_user, readable_collection_ids

router = APIRouter(prefix="/api", tags=["documents"])


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/collections", response_model=list[CollectionOut])
def list_collections(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Collections the current user may read and upload into."""
    ids = readable_collection_ids(db, user)
    return db.scalars(select(Collection).where(Collection.id.in_(ids)).order_by(Collection.name)).all()


@router.get("/search")
def search_chunks(q: str, k: int = 5, collection_id: int | None = None,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Raw retrieval results (no LLM): useful for checking what the assistant will see."""
    if not q.strip() or not 1 <= k <= 20:
        raise HTTPException(422, "q must not be empty and k must be between 1 and 20")
    return [h.to_dict() for h in retrieve(db, user, q, k=k, collection_id=collection_id)]


@router.post("/documents", response_model=DocumentOut, status_code=202)
def upload_document(request: Request, tasks: BackgroundTasks,
                    collection_id: int = Form(...), file: UploadFile = File(...),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Upload a PDF, DOCX or TXT file. Returns immediately (202); processing runs in the background."""
    if collection_id not in readable_collection_ids(db, user):
        raise HTTPException(404, "Collection not found")

    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)  # read one byte past the limit to detect oversize files
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
    if not data:
        raise HTTPException(400, "File is empty")
    try:
        # Stored under a random name; the user's filename is only kept as display text.
        doc = create_document(db, user.id, collection_id, file.filename or "", data)
        audit(db, user.id, "document.upload", "document", doc.id, ip=client_ip(request),
              filename=doc.filename, collection_id=collection_id)
        db.commit()
    except ValueError as e:
        raise HTTPException(415, str(e))
    except DuplicateDocument as e:
        raise HTTPException(409, str(e))
    except IntegrityError:  # the same file uploaded twice at the same moment
        db.rollback()
        raise HTTPException(409, "This file is already in the collection")

    tasks.add_task(ingest_document, doc.id)
    return doc


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(collection_id: int | None = None,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ids = readable_collection_ids(db, user)
    if collection_id is not None:
        ids = [i for i in ids if i == collection_id]
    return db.scalars(select(Document).where(Document.collection_id.in_(ids))
                      .order_by(Document.created_at.desc())).all()


@router.get("/documents/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_document_for_user(db, user, doc_id)


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, request: Request,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete a document, its chunks (database cascade) and its stored file. Owner or admin only."""
    doc = get_document_for_user(db, user, doc_id)
    if not can_delete(user, doc):
        raise HTTPException(403, "Only the uploader or an admin can delete this document")
    path = Path(doc.storage_path)
    audit(db, user.id, "document.delete", "document", doc.id, ip=client_ip(request), filename=doc.filename)
    db.delete(doc)
    db.commit()
    path.unlink(missing_ok=True)
    return Response(status_code=204)
