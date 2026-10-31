"""Unit tests for the pure pipeline logic (no database, no Ollama)."""
import io
import zipfile

import pytest

from app.ingestion import (ALLOWED, Block, chunk_blocks, detect_file_type, remove_headers_footers,
                           split_long, text_to_blocks)


def test_detects_real_type_not_name():
    assert detect_file_type(b"%PDF-1.7 ...", "anything.bin") == "pdf"
    assert detect_file_type("Hello world".encode(), "notes.txt") == "txt"
    with pytest.raises(ValueError):
        detect_file_type(b"MZ\x90\x00 program", "virus.pdf")      # .exe renamed to .pdf
    with pytest.raises(ValueError):
        detect_file_type(b"MZ\x90\x00 program", "virus.txt")      # binary named .txt


def test_zip_that_is_not_word_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.md", "hi")
    with pytest.raises(ValueError, match="not a Word"):
        detect_file_type(buf.getvalue(), "file.docx")


def test_headers_footers_and_page_numbers_removed():
    pages = [f"ACME Internal\nBody text {i}\nPage {i} of 3" for i in (1, 2, 3)]
    cleaned = remove_headers_footers(pages)
    assert cleaned == ["Body text 1", "Body text 2", "Body text 3"]


def test_numbered_headings_and_hyphenation():
    blocks, last = text_to_blocks("Intro line\n\n2. Travel\nEconomy class is used for short in-\nternational flights.")
    assert [(b.heading, b.text) for b in blocks] == [
        (None, "Intro line"), ("2. Travel", "Economy class is used for short international flights.")]
    assert last == "2. Travel"


def test_long_text_split_at_sentences_within_limit():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    parts = split_long(text, 500)
    assert all(len(p) <= 500 for p in parts)
    assert all(p.endswith(".") for p in parts)


def test_chunks_respect_size_and_overlap():
    blocks = [Block(f"Paragraph {i}. " + "word " * 150, page=1, heading="H") for i in range(10)]
    chunks = chunk_blocks(blocks, max_chars=3000, overlap=400)
    assert len(chunks) > 1
    assert all(len(c.text) <= 3000 + 2 for c in chunks)
    # each later chunk starts with text from the end of the previous one
    assert chunks[1].text[:50] in chunks[0].text


def test_new_heading_or_page_starts_new_chunk():
    blocks = [Block("a.", 1, "1. One"), Block("b.", 1, "2. Two"), Block("c.", 2, "2. Two")]
    chunks = chunk_blocks(blocks)
    assert [(c.page, c.heading, c.text) for c in chunks] == [(1, "1. One", "a."), (1, "2. Two", "b."), (2, "2. Two", "c.")]
    assert chunks[0].content == "1. One\n\na."


def test_status_transitions():
    assert "ready" not in ALLOWED["queued"]          # can't skip processing
    assert ALLOWED["ready"] == {"queued"}             # only re-index
