"""The benchmark file must be valid, or every evaluation number built on it is wrong."""
import json
import re
from pathlib import Path

from app.ingestion import chunk_blocks, detect_file_type, extract
from tests.conftest import SAMPLES

QUESTIONS = Path(__file__).resolve().parents[2] / "evaluation" / "questions.jsonl"
TYPES = {"lookup", "paraphrase", "table", "multi_part", "cross_doc", "unanswerable", "permission", "injection"}
USERS = {"admin@example.com", "alice@example.com", "bob@example.com"}


def test_benchmark_is_valid_and_gold_sections_exist():
    sections = {}
    for f in SAMPLES.iterdir():
        if f.suffix in {".pdf", ".docx", ".txt"}:
            blocks, _ = extract(f, detect_file_type(f.read_bytes(), f.name))
            sections[f.name] = {c.heading for c in chunk_blocks(blocks)}

    rows = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert 50 <= len(rows) <= 100
    assert len({r["id"] for r in rows}) == len(rows), "duplicate ids"
    for r in rows:
        assert r["type"] in TYPES and r["user"] in USERS and r["expect"] in {"answer", "refuse"}, r["id"]
        assert (r["expect"] == "refuse") == (r["gold"] == []), r["id"]
        for doc, section in r["gold"]:
            assert section in sections[doc], f"{r['id']}: {doc} has no section {section!r}"
        for pattern in r.get("must_include", []) + r.get("must_not_include", []):
            re.compile(pattern)
