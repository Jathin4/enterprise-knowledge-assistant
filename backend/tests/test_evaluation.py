"""Metric calculations and the evaluation API."""
from app.evaluation import missing_patterns, summarize


def row(**kw):
    base = {"id": "q", "type": "lookup", "expect": "answer", "gold_rank": 1, "gate_refuses": False,
            "retrieval_ms": 50, "abstained": False, "missing": [], "forbidden": [], "uncited": False,
            "invalid_citations": [], "cited_gold": True, "grounded": "supported", "generation_ms": 5000}
    return base | kw


def test_retrieval_metrics():
    rows = [row(gold_rank=1), row(gold_rank=2), row(gold_rank=None),
            row(type="unanswerable", expect="refuse", gold_rank=None, gate_refuses=True)]
    m = summarize(rows, "retrieval")
    assert m["recall@1"] == 0.333 and m["recall@3"] == 0.667 and m["recall@5"] == 0.667
    assert m["mrr"] == 0.5                          # (1 + 1/2 + 0) / 3
    assert m["gate_correct_refusals"] == 1.0 and m["gate_false_refusals"] == 0.0


def test_full_metrics():
    rows = [
        row(),                                                   # perfect answer
        row(missing=["90 days"], grounded="partially_supported"),  # wrong fact
        row(abstained=True),                                     # refused although answerable
        row(type="unanswerable", expect="refuse", abstained=True),
        row(type="unanswerable", expect="refuse", abstained=False),
        row(type="injection", forbidden=["no manager approval"]),
    ]
    m = summarize(rows, "full")
    assert m["answer_rate"] == 0.75                 # 3 of 4 answerable were answered
    assert m["correct_refusals"] == 0.5
    assert m["facts_correct"] == 0.5                # the perfect one + the injection one (facts present)
    assert m["grounded"] == 0.667
    assert m["injection_resisted"] == 0.0
    assert m["by_type"]["unanswerable"] == {"n": 2, "found": None, "correct": 0.5}


def test_patterns_are_case_insensitive_alternatives():
    assert missing_patterns("Rotated every 90 days by the CISO", ["90 days", "ciso|officer", "vault"]) == ["vault"]


def test_evaluation_api_is_admin_only(client):
    from tests.conftest import auth
    alice, admin = auth(client, "alice@example.com"), auth(client, "admin@example.com")
    assert client.get("/api/evaluations", headers=alice).status_code == 403
    assert client.post("/api/evaluations", headers=alice, json={"label": "x"}).status_code == 403
    assert client.get("/api/evaluations", headers=admin).json() == []
