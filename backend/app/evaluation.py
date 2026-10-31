"""Measure the assistant on evaluation/questions.jsonl and store the result in evaluation_runs.

Two modes:
- "retrieval": seconds. Recall@k, MRR and the refusal gate, no LLM. Used for tuning experiments.
- "full":      tens of minutes on CPU. Also generates every answer and has an LLM judge check
               that each answer is supported by the passages it cites.
"""
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.answering import REMINDER, SYSTEM_PROMPT, answer_question
from app.config import PROJECT_DIR, settings
from app.models import DocumentChunk, EvaluationRun, User
from app.retrieval import retrieve

QUESTIONS = PROJECT_DIR / "evaluation" / "questions.jsonl"

JUDGE_PROMPT = """You check whether an answer is supported by its sources.

SOURCES:
<<<
{sources}
>>>

ANSWER:
<<<
{answer}
>>>

Is every factual statement in the answer supported by the sources?
- "supported": every statement is backed by the sources
- "partially_supported": some statements are backed, at least one is not
- "unsupported": the main claim is not backed by the sources"""


class Verdict(BaseModel):
    verdict: Literal["supported", "partially_supported", "unsupported"]
    reason: str


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]


def missing_patterns(text: str, patterns: list[str]) -> list[str]:
    return [p for p in patterns if not re.search(p, text, re.I)]


def judge(answer: str, sources: list[str]) -> Verdict:
    """An LLM judge (the same local model, so expect some leniency: it is a signal, not proof)."""
    prompt = JUDGE_PROMPT.format(sources="\n\n".join(sources), answer=answer)
    r = httpx.post(f"{settings.ollama_url}/api/chat", timeout=900, json={
        "model": settings.chat_model, "stream": False, "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "format": Verdict.model_json_schema(),
        "options": {"temperature": 0, "num_ctx": settings.num_ctx},
    })
    r.raise_for_status()
    return Verdict.model_validate_json(r.json()["message"]["content"])


def evaluate_question(db: Session, user: User, q: dict, mode: str) -> dict:
    gold = {tuple(g) for g in q["gold"]}
    row = {k: q[k] for k in ("id", "type", "user", "question", "expect")}

    start = time.perf_counter()
    hits = retrieve(db, user, q["question"])
    row["retrieval_ms"] = round((time.perf_counter() - start) * 1000)
    ranks = [i for i, h in enumerate(hits, start=1) if (h.filename, h.heading) in gold]
    row["gold_rank"] = ranks[0] if ranks else None
    row["best_distance"] = round(hits[0].distance, 4) if hits else None
    row["gate_refuses"] = not hits or hits[0].distance > settings.max_distance
    row["top"] = [[h.filename, h.heading, round(h.distance, 3)] for h in hits[:3]]
    if mode == "retrieval":
        return row

    a = answer_question(db, user, q["question"])
    row.update(answer=a.text, abstained=a.abstained, generation_ms=a.generation_ms,
               cited=[[s["filename"], s["heading"]] for s in a.sources],
               invalid_citations=a.invalid_citations, uncited=a.uncited)
    if not a.abstained:
        row["missing"] = missing_patterns(a.text, q.get("must_include", []))
        row["forbidden"] = [p for p in q.get("must_not_include", []) if re.search(p, a.text, re.I)]
        row["cited_gold"] = any(tuple(c) in gold for c in row["cited"])
        # Cited numbers index the retrieved list (usable passages are its closest prefix).
        # Keep the [n] labels so the judge can match each citation in the answer to its source.
        sources = [f"[{s['n']}] {a.retrieved[s['n'] - 1].content}" for s in a.sources]
        if sources:
            v = judge(a.text, sources)
            row["grounded"], row["judge_reason"] = v.verdict, v.reason
        else:
            row["grounded"], row["judge_reason"] = "unsupported", "No valid citations."
    return row


def _share(items: list, test: Callable) -> float | None:
    return round(sum(1 for x in items if test(x)) / len(items), 3) if items else None


def _pct(values: list[float], p: int) -> int | None:
    values = sorted(v for v in values if v is not None)
    return round(values[max(0, math.ceil(p / 100 * len(values)) - 1)]) if values else None


def summarize(rows: list[dict], mode: str) -> dict:
    answerable = [r for r in rows if r["expect"] == "answer"]
    refusable = [r for r in rows if r["expect"] == "refuse"]
    with_gold = answerable  # every answerable question has gold sections
    k = settings.top_k
    m = {
        "questions": len(rows),
        "recall@1": _share(with_gold, lambda r: r["gold_rank"] == 1),
        "recall@3": _share(with_gold, lambda r: r["gold_rank"] is not None and r["gold_rank"] <= 3),
        f"recall@{k}": _share(with_gold, lambda r: r["gold_rank"] is not None),
        "mrr": round(sum(1 / r["gold_rank"] for r in with_gold if r["gold_rank"]) / len(with_gold), 3) if with_gold else None,
        "gate_correct_refusals": _share(refusable, lambda r: r["gate_refuses"]),
        "gate_false_refusals": _share(answerable, lambda r: r["gate_refuses"]),
        "retrieval_ms_p50": _pct([r["retrieval_ms"] for r in rows], 50),
        "retrieval_ms_p95": _pct([r["retrieval_ms"] for r in rows], 95),
    }
    if mode == "full":
        answered = [r for r in answerable if not r["abstained"]]
        injection = [r for r in rows if r["type"] == "injection"]
        m.update({
            "answer_rate": _share(answerable, lambda r: not r["abstained"]),
            "correct_refusals": _share(refusable, lambda r: r["abstained"]),
            "facts_correct": _share(answerable, lambda r: not r["abstained"] and not r["missing"]),
            "citation_valid": _share(answered, lambda r: not r["uncited"] and not r["invalid_citations"]),
            "citation_correct": _share(answered, lambda r: r["cited_gold"]),
            "grounded": _share(answered, lambda r: r["grounded"] == "supported"),
            "injection_resisted": _share(injection, lambda r: not r["abstained"] and not r["forbidden"]),
            "generation_ms_p50": _pct([r["generation_ms"] for r in answered], 50),
            "generation_ms_p95": _pct([r["generation_ms"] for r in answered], 95),
        })
    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    m["by_type"] = {
        t: {"n": len(rs),
            "found": _share(rs, lambda r: r["gold_rank"] is not None) if rs[0]["expect"] == "answer" else None,
            **({"correct": _share(rs, lambda r: (r["abstained"] if r["expect"] == "refuse"
                                                 else not r["abstained"] and not r["missing"] and not r.get("forbidden")))}
               if mode == "full" else {})}
        for t, rs in sorted(by_type.items())}
    return m


def current_config(db: Session) -> dict:
    """What was measured: settings now, and chunk statistics of what is actually stored."""
    chunks, avg_len = db.execute(select(func.count(), func.avg(func.length(DocumentChunk.content)))).one()
    return {"chat_model": settings.chat_model, "embed_model": settings.embed_model, "top_k": settings.top_k,
            "max_distance": settings.max_distance, "num_ctx": settings.num_ctx,
            "chunk_max_chars": settings.chunk_max_chars, "chunk_overlap_chars": settings.chunk_overlap_chars,
            "embed_with_title": settings.embed_with_title,
            "stored_chunks": chunks, "avg_chunk_chars": round(avg_len or 0),
            "prompt_sha": hashlib.sha256((SYSTEM_PROMPT + REMINDER).encode()).hexdigest()[:8]}


def run_evaluation(db: Session, run: EvaluationRun, on_progress: Callable[[int, int, dict], None] | None = None,
                   types: set[str] | None = None) -> None:
    """Fill in an already-saved EvaluationRun, committing progress after each question.
    `types` limits the run to some question types (a quick check, not a comparable full result)."""
    start = time.perf_counter()
    questions = [q for q in load_questions() if not types or q["type"] in types]
    users = {u.email: u for u in db.scalars(select(User))}
    run.total, run.config, rows = len(questions), current_config(db), []
    try:
        for i, q in enumerate(questions, start=1):
            rows.append(evaluate_question(db, users[q["user"]], q, run.mode))
            run.progress = i
            db.commit()
            if on_progress:
                on_progress(i, len(questions), rows[-1])
        run.results, run.metrics, run.status = rows, summarize(rows, run.mode), "done"
    except Exception as e:
        db.rollback()
        run.status, run.error, run.results = "failed", f"{type(e).__name__}: {e}"[:500], rows
        raise
    finally:
        run.duration_s = round(time.perf_counter() - start, 1)
        db.commit()
