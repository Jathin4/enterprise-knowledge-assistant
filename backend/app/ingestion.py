"""Turn an uploaded file into searchable chunks: extract → clean → chunk → embed → store."""
import hashlib
import io
import logging
import re
import threading
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
from docx import Document as DocxFile
from docx.table import Table
from pypdf import PdfReader
from sqlalchemy import delete, select
from tenacity import retry, stop_after_attempt, wait_exponential

from app import db as dbmod  # module reference so tests can swap SessionLocal
from app.config import settings
from app.models import Document, DocumentChunk

log = logging.getLogger(__name__)

DOC_PREFIX = "search_document: "  # nomic-embed-text task prefix (questions use "search_query: ")
MAX_DOCX_UNZIPPED = 200 * 1024 * 1024  # guards against "zip bomb" files

PAGE_NUMBER = re.compile(r"(page\s*)?\d+(\s*(of|/)\s*\d+)?", re.I)
# A numbered heading on its own line, e.g. "4. API security" or "2.1 Request signing" (no final full stop).
NUMBERED_HEADING = re.compile(r"\d+(\.\d+)*\.?\s+[A-Z][^.!?]{0,70}")


# ---------- 1. Validate: what kind of file is this really? ----------

def detect_file_type(data: bytes, filename: str) -> str:
    """Check the file's content ("magic bytes"), not just its name, which anyone can fake."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):  # DOCX is a ZIP archive containing word/document.xml
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNZIPPED:
                        raise ValueError("Word document is too large when unpacked")
                    return "docx"
        except zipfile.BadZipFile:
            pass
        raise ValueError("ZIP file is not a Word (.docx) document")
    if filename.lower().endswith(".txt"):
        if b"\x00" in data:
            raise ValueError("File named .txt contains binary data")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Text files must be UTF-8 encoded")
        return "txt"
    raise ValueError("Unsupported file type: only PDF, DOCX and TXT are accepted")


class DuplicateDocument(Exception):
    def __init__(self, existing_id: int):
        super().__init__(f"This file is already in the collection (document {existing_id})")
        self.existing_id = existing_id


def create_document(db, owner_id: int, collection_id: int, filename: str, data: bytes) -> Document:
    """Validate, store the file under a random name and add a 'queued' Document (not yet committed).
    Raises ValueError (bad file) or DuplicateDocument. Used by the upload API and manage.py."""
    file_type = detect_file_type(data, filename)
    checksum = hashlib.sha256(data).hexdigest()
    duplicate = db.scalar(select(Document.id).where(Document.collection_id == collection_id,
                                                    Document.checksum == checksum))
    if duplicate:
        raise DuplicateDocument(duplicate)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / f"{uuid.uuid4().hex}.{file_type}"
    path.write_bytes(data)
    display_name = re.split(r"[\\/]", filename)[-1][:255] or f"upload.{file_type}"
    doc = Document(owner_id=owner_id, collection_id=collection_id, filename=display_name, file_type=file_type,
                   size_bytes=len(data), checksum=checksum, storage_path=str(path))
    db.add(doc)
    try:
        db.flush()  # the unique (collection, checksum) constraint is checked here
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return doc


# ---------- 2. Extract + clean ----------

@dataclass
class Block:
    text: str
    page: int | None = None
    heading: str | None = None


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)  # re-join words hyphenated across lines
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def text_to_blocks(text: str, page: int | None = None, heading: str | None = None) -> tuple[list[Block], str | None]:
    """Split plain text into paragraphs. Blank lines end a paragraph, single line breaks inside one
    become spaces, and numbered heading lines set the heading for what follows.
    Returns the blocks and the last heading (so a section can continue onto the next PDF page)."""
    blocks, lines = [], []

    def end_paragraph():
        if lines:
            blocks.append(Block(" ".join(lines), page, heading))
            lines.clear()

    for line in clean_text(text).split("\n"):
        if not line:
            end_paragraph()
        elif NUMBERED_HEADING.fullmatch(line):
            end_paragraph()
            heading = line
        else:
            lines.append(line)
    end_paragraph()
    return blocks, heading


def remove_headers_footers(pages: list[str]) -> list[str]:
    """Drop page numbers, and lines repeated on most pages (company name, 'Confidential', ...)."""
    counts = Counter(line.strip() for p in pages for line in set(p.splitlines()) if line.strip())
    repeated = {line for line, n in counts.items() if len(pages) >= 3 and n >= 0.6 * len(pages)}
    return ["\n".join(line for line in p.splitlines()
                      if line.strip() not in repeated and not PAGE_NUMBER.fullmatch(line.strip()))
            for p in pages]


def extract(path: Path, file_type: str) -> tuple[list[Block], int | None]:
    """Return text blocks with their page/heading, plus the page count (PDF only)."""
    if file_type == "pdf":
        pages = remove_headers_footers([p.extract_text() or "" for p in PdfReader(path).pages])
        blocks, heading = [], None
        for number, text in enumerate(pages, start=1):
            page_blocks, heading = text_to_blocks(text, page=number, heading=heading)
            blocks += page_blocks
        return blocks, len(pages)

    if file_type == "docx":
        blocks, heading = [], None
        for item in DocxFile(path).iter_inner_content():  # paragraphs and tables, in document order
            if isinstance(item, Table):
                rows = [" | ".join(c.text.strip() for c in row.cells) for row in item.rows]
                text = clean_text("\n".join(rows))
                if text:
                    blocks.append(Block(text, heading=heading))
                continue
            text = clean_text(item.text)
            if not text:
                continue
            style = item.style.name if item.style is not None else ""
            if style.startswith(("Heading", "Title")):
                heading = text  # becomes the heading of the paragraphs that follow
            else:
                blocks.append(Block(text, heading=heading))
        return blocks, None

    text = path.read_bytes().decode("utf-8-sig")  # -sig strips a byte-order mark if present
    return text_to_blocks(text)[0], None


# ---------- 3. Chunk ----------

@dataclass
class Chunk:
    text: str
    page: int | None
    heading: str | None

    @property
    def content(self) -> str:
        """Stored and embedded text: the heading gives the chunk context ("2.1 Request signing")."""
        return f"{self.heading}\n\n{self.text}" if self.heading else self.text


def split_long(text: str, limit: int) -> list[str]:
    """Split text longer than `limit` at sentence ends; hard-cut only a single giant sentence."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def overlap_tail(text: str, n: int) -> str:
    """The last ~n characters of text, starting cleanly at a sentence (or at least a word)."""
    if len(text) <= n:
        return text
    tail = text[-n:]
    sentence_start = re.search(r"(?<=[.!?])\s+", tail)
    return tail[sentence_start.end():] if sentence_start else tail[tail.find(" ") + 1:]


def chunk_blocks(blocks: list[Block], max_chars: int | None = None, overlap: int | None = None) -> list[Chunk]:
    """Pack paragraphs into chunks of up to max_chars. A new heading or a new page always starts
    a new chunk (so a chunk's page number is always right for citations); within one section,
    each new chunk repeats the end of the previous one (overlap). Defaults come from settings."""
    max_chars = max_chars or settings.chunk_max_chars
    overlap = settings.chunk_overlap_chars if overlap is None else overlap
    chunks: list[Chunk] = []
    current: Chunk | None = None
    for block in blocks:
        for piece in split_long(block.text, max_chars - overlap):
            same_section = current is not None and (block.heading, block.page) == (current.heading, current.page)
            if same_section and len(current.text) + 2 + len(piece) <= max_chars:
                current.text += "\n\n" + piece
                continue
            if current:
                chunks.append(current)
            start = overlap_tail(current.text, overlap) + "\n\n" if same_section else ""
            current = Chunk(start + piece, block.page, block.heading)
    if current:
        chunks.append(current)
    return chunks


# ---------- 4. Embed ----------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
def embed_batch(texts: list[str]) -> list[list[float]]:
    r = httpx.post(f"{settings.ollama_url}/api/embed",
                   json={"model": settings.embed_model, "input": texts}, timeout=300)
    r.raise_for_status()
    vectors = r.json()["embeddings"]
    if len(vectors) != len(texts) or any(len(v) != settings.embed_dim for v in vectors):
        raise ValueError("Embedding model returned an unexpected number or size of vectors")
    return vectors


def embed_texts(texts: list[str], batch_size: int = 16) -> list[list[float]]:
    return [v for i in range(0, len(texts), batch_size) for v in embed_batch(texts[i:i + batch_size])]


# ---------- 5. Store, with a status that always says where the document is ----------

ALLOWED = {
    "queued": {"extracting", "failed"},
    "extracting": {"embedding", "failed"},
    "embedding": {"ready", "failed"},
    "ready": {"queued"},    # re-index
    "failed": {"queued"},   # retry
}


def set_status(db, doc: Document, new: str, error: str | None = None) -> None:
    if new not in ALLOWED[doc.status]:
        raise ValueError(f"Status change {doc.status} -> {new} is not allowed")
    doc.status, doc.error = new, error
    db.commit()


def ingest_document(doc_id: int) -> None:
    """Background job. Safe to run again for the same document: old chunks are replaced."""
    with dbmod.SessionLocal() as db:
        doc = db.get(Document, doc_id)
        if doc is None:  # deleted before the job started
            return
        try:
            set_status(db, doc, "extracting")
            blocks, doc.page_count = extract(Path(doc.storage_path), doc.file_type)
            chunks = chunk_blocks(blocks)
            if not chunks:
                raise ValueError("No text could be extracted (scanned PDF? OCR isn't supported yet)")

            set_status(db, doc, "embedding")
            title = f"{Path(doc.filename).stem.replace('_', ' ')}\n" if settings.embed_with_title else ""
            vectors = embed_texts([DOC_PREFIX + title + c.content for c in chunks])

            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
            db.add_all(DocumentChunk(document_id=doc.id, chunk_index=i, page_number=c.page,
                                     heading=c.heading, content=c.content, embedding=v)
                       for i, (c, v) in enumerate(zip(chunks, vectors)))
            doc.chunk_count = len(chunks)
            set_status(db, doc, "ready")  # chunks and "ready" are committed together
            log.info("Document %s ready: %s chunks", doc_id, len(chunks))
        except Exception as e:
            log.exception("Document %s failed", doc_id)
            db.rollback()
            doc = db.get(Document, doc_id)
            if doc is not None:  # may have been deleted while processing
                doc.status, doc.error = "failed", f"{type(e).__name__}: {e}"[:500]
                db.commit()


def resume_unfinished() -> list[int]:
    """At server start, restart documents a previous run left half-processed."""
    with dbmod.SessionLocal() as db:
        docs = db.scalars(select(Document).where(Document.status.in_(("queued", "extracting", "embedding")))).all()
        for doc in docs:
            doc.status = "queued"  # deliberate reset, outside the normal transitions
        db.commit()
        ids = [d.id for d in docs]
    if ids:
        threading.Thread(target=lambda: [ingest_document(i) for i in ids], daemon=True).start()
    return ids
