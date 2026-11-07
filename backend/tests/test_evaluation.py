"""Metric calculations and the evaluation API."""
from app.evaluation import check_workflow, missing_patterns, summarize


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


def test_workflow_checks():
    claim = lambda text, category: {"text": text, "category": category}
    report = {"summary": "s", "unsupported": [claim("All expenses are approved automatically.", "finding")],
              "findings": [claim("Rotation is every 90 days.", "gap"), claim("MFA is required.", "met")], "conflicts": []}
    task = {"expect": "findings", "must_find": ["90", "vault"], "must_be_gap": ["MFA"], "must_be_met": ["MFA"],
            "must_not_be_gap": ["rotation"], "must_not_find": ["approved automatically"]}
    assert check_workflow(task, "awaiting_approval", report) == [
        "missing: vault", "not reported as a gap: MFA", "wrongly reported as a gap: rotation",
        "contains forbidden text: approved automatically"]   # forbidden text counts even when marked unverified
    assert check_workflow({"expect": "no_findings"}, "completed", {"findings": []}) == []
    assert check_workflow({"expect": "no_findings"}, "awaiting_approval", report) == ["expected no_findings, got 2 finding(s)"]
    assert check_workflow({"expect": "findings"}, "failed", None) == ["ended with status failed"]


def test_workflow_benchmark_file_is_valid():
    import json, re
    from app.config import PROJECT_DIR
    tasks = [json.loads(l) for l in (PROJECT_DIR / "evaluation" / "workflows.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len({t["id"] for t in tasks}) == len(tasks) >= 5
    for t in tasks:
        assert t["expect"] in ("findings", "no_findings")
        for key in ("must_find", "must_not_find", "must_be_gap", "must_be_met", "must_not_be_gap"):
            for pattern in t.get(key, []):
                re.compile(pattern)


def test_evaluation_api_is_admin_only(client):
    from tests.conftest import auth
    alice, admin = auth(client, "alice@example.com"), auth(client, "admin@example.com")
    assert client.get("/api/evaluations", headers=alice).status_code == 403
    assert client.post("/api/evaluations", headers=alice, json={"label": "x"}).status_code == 403
    assert client.get("/api/evaluations", headers=admin).json() == []
