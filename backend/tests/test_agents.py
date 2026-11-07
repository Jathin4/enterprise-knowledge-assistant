"""Agent workflows with a scripted LLM: routing, retry limits, approval, recovery and permissions."""
import json
from collections import Counter

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select, text

from app import agents, workflows
from app.agents import Analysis, ClaimVerdict, DraftClaim, GapVerdict, Plan, Rewrite, Rewrites
from app.config import settings
from app.models import CollectionMember, User, WorkflowRun
from app.routers import chat as chat_routes
from app.routers import workflows as workflow_routes
from app.schemas import ChatRequest
from app.retrieval import Hit
from tests.conftest import SAMPLES, auth, upload

ENGINEERING, FINANCE, ALICE = 1, 2, 1
REAL_QUOTE_FOUND = agents.quote_found
REQUEST = {"request": "Compare the policy with the guide and prepare a gap report."}


def hit(n: int, text: str = "Secrets are rotated every 90 days.") -> Hit:
    return Hit(chunk_id=n, document_id=1, filename="guide.docx", page=None, heading=f"S{n}", content=text, distance=0.1)


class FakeLLM:
    """Answers by schema and counts calls. `verdict(prompt)` scripts the verifier."""

    def __init__(self, claims=None, verdict=None, rewrites=None, relevant=True, in_scope=True, on_topic=True, covered=False):
        self.relevant = relevant
        self.in_scope = in_scope
        self.on_topic = on_topic
        self.covered = covered
        self.calls = Counter()
        self.prompts = []
        self.claims = claims if claims is not None else [DraftClaim(text="Secrets rotate every 90 days.", category="met", sources=[1])]
        self.verdict = verdict or (lambda prompt: "supported")
        self.rewrites = rewrites or []

    def __call__(self, prompt, schema):
        self.calls[schema.__name__] += 1
        self.prompts.append(prompt)
        if schema is Plan:
            return Plan(task_type="gap_report", queries=["policy requirements", "guide implementation"])
        if schema is Analysis:
            return Analysis(evidence_addresses_request=self.relevant, claims=self.claims, missing=[])
        if schema is Rewrites:
            return Rewrites(claims=self.rewrites)
        if schema is ClaimVerdict:
            return ClaimVerdict(quote="scripted", status=self.verdict(prompt), reason="Scripted.", on_topic=self.on_topic)
        if schema is GapVerdict:
            self.calls["ClaimVerdict"] += 1
            del self.calls["GapVerdict"]
            return GapVerdict(quote="scripted", status=self.verdict(prompt), reason="Scripted.", in_scope=self.in_scope,
                              on_topic=self.on_topic, covered_by_other_document=self.covered)
        raise AssertionError(schema)


@pytest.fixture
def setup(client, monkeypatch):
    """Runs execute inline with in-memory checkpoints. Returns a function that installs a FakeLLM."""
    graph = agents.build_graph(InMemorySaver())
    monkeypatch.setattr(workflows, "get_graph", lambda: graph)
    monkeypatch.setattr(workflows, "enqueue", lambda fn, *args: fn(*args))
    monkeypatch.setattr(agents, "embed_query", lambda q: [0.1] * settings.embed_dim)
    monkeypatch.setattr(agents, "quote_found", lambda quote, passages: True)   # scripted quotes aren't real
    monkeypatch.setattr(agents, "measure_coverage", lambda state, evidence, claims: {"sections_total": 1, "sections_examined": 1, "not_examined": []})

    def install(llm=None, hits=None, gap_search=None):
        llm = llm or FakeLLM()
        monkeypatch.setattr(agents, "generate_json", llm)
        if hits is not None:     # scripted research rounds; candidate-gap searches are scripted separately
            monkeypatch.setattr(agents, "search_for_gap", gap_search or (lambda state, text: []))
        if hits is not None:
            monkeypatch.setattr(agents, "search", hits)
        return llm
    return install


def scripted_hits(*rounds):
    """A search that returns new passages for each research round's first query, then nothing."""
    remaining = list(rounds)
    return lambda db, allowed, vector, k: remaining.pop(0) if remaining else []


def start(client, headers, **body):
    r = client.post("/api/reports", headers=headers, json=REQUEST | body)
    assert r.status_code == 202, r.text
    return client.get(f"/api/workflows/{r.json()['id']}", headers=headers).json()


def test_full_flow_pauses_then_reviewer_approves(client, setup):
    llm = setup(hits=scripted_hits([hit(1)]))
    alice, carol, bob = (auth(client, f"{n}@example.com") for n in ("alice", "carol", "bob"))
    run = start(client, alice)

    assert run["status"] == "awaiting_approval" and run["task_type"] == "gap_report"
    assert [e["step"] for e in run["events"]] == ["orchestrator", "research", "analysis", "verification", "report", "approval"]
    assert run["report"]["findings"][0]["text"] == "Secrets rotate every 90 days."
    assert run["report"]["sources"][0]["filename"] == "guide.docx"
    assert llm.calls == {"Plan": 1, "Analysis": 1, "ClaimVerdict": 1}

    # who can see and decide
    assert [r["id"] for r in client.get("/api/approvals", headers=carol).json()] == [run["id"]]
    assert client.get("/api/approvals", headers=alice).json() == []
    assert client.get(f"/api/workflows/{run['id']}", headers=bob).status_code == 404
    assert client.post(f"/api/approvals/{run['id']}", headers=alice, json={"decision": "approved"}).status_code == 403  # own run
    assert client.post(f"/api/approvals/{run['id']}", headers=bob, json={"decision": "approved"}).status_code == 404

    r = client.post(f"/api/approvals/{run['id']}", headers=carol, json={"decision": "approved", "comment": "Looks right"})
    assert r.status_code == 200, r.text
    done = client.get(f"/api/workflows/{run['id']}", headers=alice).json()
    assert done["status"] == "approved" and done["approval"]["comment"] == "Looks right" and done["finished_at"]
    assert client.post(f"/api/approvals/{run['id']}", headers=carol, json={"decision": "rejected"}).status_code == 409  # once only


def test_reviewer_can_reject(client, setup):
    setup(hits=scripted_hits([hit(1)]))
    alice, carol = auth(client, "alice@example.com"), auth(client, "carol@example.com")
    run = start(client, alice)
    client.post(f"/api/approvals/{run['id']}", headers=carol, json={"decision": "rejected", "comment": "Too thin"})
    assert client.get(f"/api/workflows/{run['id']}", headers=alice).json()["status"] == "rejected"


def test_employee_cannot_review_and_analyst_needs_access(client, setup):
    setup(hits=scripted_hits([hit(1)]))
    bob, carol, admin = (auth(client, f"{n}@example.com") for n in ("bob", "carol", "admin"))
    run = start(client, bob)    # searches Finance, which carol can't read
    assert client.get("/api/approvals", headers=carol).json() == []
    assert client.post(f"/api/approvals/{run['id']}", headers=carol, json={"decision": "approved"}).status_code == 404
    assert [r["id"] for r in client.get("/api/approvals", headers=admin).json()] == [run["id"]]


def test_bad_claims_are_rejected_in_code_without_asking_the_llm(client, setup):
    llm = setup(FakeLLM(claims=[DraftClaim(text="Cites nothing real.", category="finding", sources=[9]),
                                DraftClaim(text="No citation.", category="finding", sources=[]),
                                DraftClaim(text="Secrets rotate every 30 days.", category="met", sources=[1])]),
                hits=scripted_hits([hit(1)]))
    run = start(client, auth(client, "alice@example.com"))
    assert llm.calls["ClaimVerdict"] == 0
    reasons = [c["reason"] for c in run["report"]["unsupported"]]
    assert reasons[0] == reasons[1] == "Cites a passage that was not retrieved."
    assert "30" in reasons[2]                     # invented figure
    assert run["report"]["findings"] == [] and run["status"] == "completed"    # nothing to approve
    assert "No verified findings" in run["report"]["summary"]


def test_unsupported_claims_never_become_findings_and_retries_are_limited(client, setup):
    claims = [DraftClaim(text="Rotation happens.", category="met", sources=[1]),
              DraftClaim(text="Keys are stored in a vault.", category="met", sources=[1])]
    llm = FakeLLM(claims=claims, verdict=lambda p: "unsupported" if "vault" in p.split("STATEMENT:")[1] else "supported",
                  rewrites=[Rewrite(original=1, text="Keys are stored in a vault.", category="met", sources=[2])])
    setup(llm, hits=scripted_hits([hit(1)], [], [], [hit(2)], [hit(3)], [hit(4)], []))
    run = start(client, auth(client, "alice@example.com"))
    report = run["report"]
    assert report["retries"] == agents.MAX_RETRIES
    assert llm.calls["Rewrites"] == agents.MAX_RETRIES
    assert [c["text"] for c in report["findings"]] == ["Rotation happens."]
    assert [c["text"] for c in report["unsupported"]] == ["Keys are stored in a vault."]
    assert any("could not be verified" in x for x in report["limitations"])
    assert llm.calls["ClaimVerdict"] == 1 + 1 + agents.MAX_RETRIES   # verified claims aren't re-checked
    assert run["status"] == "awaiting_approval"


def test_retry_can_fix_a_claim(client, setup):
    answers = iter(["unsupported", "supported"])
    llm = FakeLLM(verdict=lambda p: next(answers),
                  rewrites=[Rewrite(original=1, text="Secrets rotate every 90 days.", category="met", sources=[2])])
    setup(llm, hits=scripted_hits([hit(1, "Backups last 90 days.")], [], [], [hit(2)]))
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert report["unsupported"] == [] and report["findings"][0]["sources"] == [2] and report["retries"] == 1


def test_retry_stops_early_when_nothing_new_is_found(client, setup):
    llm = setup(FakeLLM(verdict=lambda p: "unsupported"), hits=scripted_hits([hit(1)]))
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert report["retries"] == 1 and llm.calls["Rewrites"] == 0


def test_no_evidence_finishes_without_analysis(client, setup):
    llm = setup(hits=scripted_hits())
    run = start(client, auth(client, "alice@example.com"))
    assert run["status"] == "completed" and llm.calls["Analysis"] == 0


def two_docs(hits_a=1):
    return scripted_hits([hit(1), Hit(chunk_id=2, document_id=2, filename="policy.pdf", page=2, heading="API",
                                      content="Secrets must rotate every 90 days.", distance=0.1)])


def test_true_but_irrelevant_claims_are_not_reported(client, setup):
    llm = setup(FakeLLM(relevant=False), hits=scripted_hits([hit(1)]))
    run = start(client, auth(client, "alice@example.com"))
    assert run["status"] == "completed" and run["report"]["findings"] == []
    assert llm.calls["ClaimVerdict"] == 0        # nothing to verify, nothing to approve
    assert "No verified findings" in run["report"]["summary"]


def test_met_needs_evidence_from_two_documents(client, setup):
    llm = FakeLLM(claims=[DraftClaim(text="Rotation every 90 days is met.", category="met", sources=[1]),
                          DraftClaim(text="The guide omits the 90 days.", category="gap", sources=[1, 2])])
    setup(llm, hits=two_docs())
    findings = start(client, auth(client, "alice@example.com"))["report"]["findings"]
    # The evidence decides: one document can't prove "met"; two documents can't be a "gap".
    assert [c["category"] for c in findings] == ["gap", "met"]
    gap_prompt, met_prompt = [p for p in llm.prompts if "STATEMENT:" in p]
    assert "not implemented" in gap_prompt and "is met" in met_prompt


def test_candidate_gap_is_searched_for_before_it_is_accepted(client, setup):
    # Research didn't retrieve the section that covers rotation; the gap's own search does.
    covering = Hit(chunk_id=7, document_id=2, filename="guide.docx", page=None, heading="2.2 Secret rotation",
                   content="Secrets are rotated every 90 days.", distance=0.1)
    searched = []

    def gap_search(state, text):
        searched.append(text)
        return [covering]
    llm = FakeLLM(claims=[DraftClaim(text="The guide does not mention rotation.", category="gap", sources=[1])],
                  verdict=lambda p: "unsupported" if "2.2 Secret rotation" in p else "supported")
    setup(llm, hits=scripted_hits([hit(1, "The policy requires rotation.")]), gap_search=gap_search)
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert searched == ["The guide does not mention rotation."]
    assert report["findings"] == []
    assert [c["text"] for c in report["unsupported"]] == ["The guide does not mention rotation."]


def test_verifier_quote_must_really_be_in_the_passages():
    passages = "[1] (a.pdf)\nSecrets must be   rotated every 90 days.\nLogs are kept."
    assert agents.quote_found("secrets must be rotated every 90 days", passages)
    assert not agents.quote_found("Secrets are rotated automatically.", passages)
    assert not agents.quote_found("", passages)


def test_invented_quote_means_unsupported(client, setup, monkeypatch):
    llm = setup(hits=scripted_hits([hit(1)]))
    monkeypatch.setattr(agents, "quote_found", REAL_QUOTE_FOUND)
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert report["findings"] == [] and "quote" in report["unsupported"][0]["reason"]
    assert llm.calls["ClaimVerdict"] >= 1


def test_conflict_needs_two_passages(client, setup):
    setup(FakeLLM(claims=[DraftClaim(text="One passage only.", category="finding", sources=[1])],
                  verdict=lambda p: "conflicting"), hits=scripted_hits([hit(1)]))
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert report["conflicts"] == [] and report["unsupported"][0]["text"] == "One passage only."


def test_figures_from_the_request_are_allowed(client, setup):
    llm = setup(FakeLLM(claims=[DraftClaim(text="A 5,000 EUR purchase needs two quotes.", category="finding", sources=[1])]),
                hits=scripted_hits([hit(1, "Purchases need two written quotes.")]))
    r = client.post("/api/reports", headers=auth(client, "alice@example.com"), json={"request": "What does a 5,000 EUR purchase need?"})
    assert r.status_code == 202 and llm.calls["ClaimVerdict"] == 1


def test_out_of_scope_gaps_are_kept_apart(client, setup):
    llm = FakeLLM(claims=[DraftClaim(text="The guide omits leavers.", category="gap", sources=[1])], in_scope=False)
    setup(llm, hits=scripted_hits([hit(1)]))
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert report["findings"] == [] and report["out_of_scope"][0]["text"] == "The guide omits leavers."


def test_coverage_lists_sections_not_examined(client, monkeypatch):
    alice = auth(client, "alice@example.com")
    upload(client, alice, ENGINEERING, SAMPLES / "expense_policy.txt")
    evidence = [{"filename": "expense_policy.txt", "heading": "1. Approval"}]
    state = {"user_id": ALICE, "collection_ids": [ENGINEERING]}
    claims = [{"sources": [1]}]
    coverage = agents.measure_coverage(state, evidence, claims)
    assert coverage["sections_examined"] == 1 and coverage["sections_total"] == 5
    assert "expense_policy.txt · 2. Travel" in coverage["not_examined"]


def test_summary_is_built_only_from_verified_claims(client, setup):
    llm = FakeLLM(claims=[DraftClaim(text="The guide omits rotation.", category="gap", sources=[1]),
                          DraftClaim(text="Secrets rotate in 90 days everywhere.", category="met", sources=[1, 2]),
                          DraftClaim(text="Invented claim.", category="finding", sources=[1])],
                  verdict=lambda p: "unsupported" if "Invented" in p.split("STATEMENT:")[1] else "supported")
    setup(llm, hits=two_docs())
    summary = start(client, auth(client, "alice@example.com"))["report"]["summary"]
    assert summary == "Verified result: 1 met, 1 gap(s), 1 unverified. Gaps: The guide omits rotation."
    assert "Invented" not in summary


# ---------- Permissions and hostile documents, using real database search ----------

def test_search_only_covers_collections_the_user_can_read(client, setup):
    llm = setup()
    alice, bob = auth(client, "alice@example.com"), auth(client, "bob@example.com")
    upload(client, alice, ENGINEERING, SAMPLES / "api_authentication_guide.docx")
    upload(client, bob, FINANCE, SAMPLES / "expense_policy.txt")
    start(client, alice)
    analysis_prompt = next(p for p in llm.prompts if "EVIDENCE" in p)
    assert "api_authentication_guide.docx" in analysis_prompt and "expense_policy.txt" not in analysis_prompt
    # asking for a collection you can't read is refused before anything runs
    assert client.post("/api/reports", headers=alice, json=REQUEST | {"collection_id": FINANCE}).status_code == 404


def test_removed_member_loses_access_during_a_run(client, setup, monkeypatch):
    llm = setup()
    alice = auth(client, "alice@example.com")
    upload(client, alice, ENGINEERING, SAMPLES / "api_authentication_guide.docx")
    original = agents.orchestrator

    def remove_then_plan(state):
        with client.db() as db:
            db.execute(delete(CollectionMember).where(CollectionMember.user_id == ALICE))
            db.commit()
        return original(state)
    monkeypatch.setattr(agents, "orchestrator", remove_then_plan)
    monkeypatch.setattr(workflows, "get_graph", lambda g=agents.build_graph(InMemorySaver()): g)
    run_id = client.post("/api/reports", headers=alice, json=REQUEST).json()["id"]
    with client.db() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "completed" and run.report["findings"] == []
    assert llm.calls["Analysis"] == 0


def test_poisoned_passage_never_reaches_any_agent(client, setup):
    llm = setup()
    bob = auth(client, "bob@example.com")
    upload(client, bob, FINANCE, SAMPLES / "vendor_onboarding_notes.txt")
    start(client, bob)
    assert any("Bank detail changes" in p for p in llm.prompts)
    assert not any("ignore all previous instructions" in p.lower() for p in llm.prompts)


# ---------- Recovery ----------

class Boom(FakeLLM):
    """Fails on the verifier the first time, like Ollama stopping mid-run."""
    failed = False

    def __call__(self, prompt, schema):
        if schema in (ClaimVerdict, GapVerdict) and not self.failed:
            self.failed = True
            raise ConnectionError("Ollama is not running")
        return super().__call__(prompt, schema)


def test_failed_run_continues_from_last_saved_step(client, setup):
    llm = setup(Boom(), hits=scripted_hits([hit(1)]))
    alice, carol = auth(client, "alice@example.com"), auth(client, "carol@example.com")
    run = start(client, alice)
    assert run["status"] == "failed" and "Ollama" in run["error"]
    assert llm.calls["Plan"] == 1 and llm.calls["Analysis"] == 1

    assert client.post(f"/api/workflows/{run['id']}/retry", headers=carol).status_code == 403
    assert client.post(f"/api/workflows/{run['id']}/retry", headers=alice).status_code == 202
    resumed = client.get(f"/api/workflows/{run['id']}", headers=alice).json()
    assert resumed["status"] == "awaiting_approval" and resumed["error"] is None
    assert llm.calls["Plan"] == 1 and llm.calls["Analysis"] == 1     # earlier work was not repeated


def test_server_restart_continues_unfinished_run(client, setup):
    llm = setup(Boom(), hits=scripted_hits([hit(1)]))
    run = start(client, auth(client, "alice@example.com"))
    with client.db() as db:                # as if the process died mid-step instead of failing cleanly
        db.get(WorkflowRun, run["id"]).status = "running"
        db.commit()
    assert workflows.resume_unfinished() == [run["id"]]
    with client.db() as db:
        assert db.get(WorkflowRun, run["id"]).status == "awaiting_approval"
    assert llm.calls["Plan"] == 1


def test_decision_recorded_before_a_crash_is_not_lost(client, setup, monkeypatch):
    setup(hits=scripted_hits([hit(1)]))
    alice, carol = auth(client, "alice@example.com"), auth(client, "carol@example.com")
    run = start(client, alice)
    real_enqueue = workflows.enqueue
    monkeypatch.setattr(workflows, "enqueue", lambda fn, *args: None)      # dies right after the decision is saved
    client.post(f"/api/approvals/{run['id']}", headers=carol, json={"decision": "approved"})
    monkeypatch.setattr(workflows, "enqueue", real_enqueue)
    assert workflows.resume_unfinished() == [run["id"]]
    assert client.get(f"/api/workflows/{run['id']}", headers=alice).json()["status"] == "approved"


def test_paused_run_is_not_restarted_on_server_start(client, setup):
    setup(hits=scripted_hits([hit(1)]))
    start(client, auth(client, "alice@example.com"))
    assert workflows.resume_unfinished() == []


# ---------- The stall: an open transaction makes LangGraph's first-time CREATE INDEX CONCURRENTLY wait forever ----------

def open_transactions(client) -> int:
    with client.db() as db:
        return db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                              "AND state = 'idle in transaction'"))


def test_worker_holds_no_transaction_while_setting_up_the_graph(client, setup, monkeypatch):
    setup(hits=scripted_hits())
    graph = workflows.get_graph()
    seen = []

    def spying_get_graph():
        seen.append(open_transactions(client))
        return graph
    monkeypatch.setattr(workflows, "get_graph", spying_get_graph)
    start(client, auth(client, "alice@example.com"))
    assert seen and seen[0] == 0


def test_progress_stream_does_not_hold_a_transaction_open(client, setup):
    setup(hits=scripted_hits([hit(1)]))
    alice = auth(client, "alice@example.com")
    run = start(client, alice)
    with client.db() as db:
        user = db.get(User, ALICE)
        response = workflow_routes.stream_events(run["id"], 0, user, db)
        assert not db.in_transaction()
    assert response.media_type == "text/event-stream"


def test_chat_stream_does_not_hold_a_transaction_open(client, setup):
    alice = auth(client, "alice@example.com")
    assert alice
    with client.db() as db:
        user = db.get(User, ALICE)
        chat_routes.chat_stream(ChatRequest(question="Hello?"), user, db)
        assert not db.in_transaction()


# ---------- Progress stream ----------

def test_event_stream_replays_and_ends_when_paused(client, setup):
    setup(hits=scripted_hits([hit(1)]))
    alice, bob = auth(client, "alice@example.com"), auth(client, "bob@example.com")
    run = start(client, alice)
    r = client.get(f"/api/workflows/{run['id']}/events", headers=alice)
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert [e.get("step") for e in events[:-1]][:2] == ["orchestrator", "research"]
    assert events[-1] == {"type": "status", "status": "awaiting_approval"}

    last = events[-3]["id"]      # reconnecting after an event only sends what came later
    later = client.get(f"/api/workflows/{run['id']}/events?after={last}", headers=alice).text
    assert later.count('"type": "event"') == 1
    assert client.get(f"/api/workflows/{run['id']}/events", headers=bob).status_code == 404


def test_runs_are_only_listed_for_owner_and_reviewers(client, setup):
    setup(hits=scripted_hits([hit(1)]))
    alice, bob, carol = (auth(client, f"{n}@example.com") for n in ("alice", "bob", "carol"))
    run = start(client, alice)
    assert [r["id"] for r in client.get("/api/workflows", headers=alice).json()] == [run["id"]]
    assert [r["id"] for r in client.get("/api/workflows", headers=carol).json()] == [run["id"]]
    assert client.get("/api/workflows", headers=bob).json() == []
    with client.db() as db:
        assert db.scalars(select(WorkflowRun)).first().collection_ids == [ENGINEERING]


def test_true_but_off_topic_claims_are_left_out(client, setup):
    llm = FakeLLM(claims=[DraftClaim(text="Roles are assigned by the lead.", category="finding", sources=[1])], on_topic=False)
    setup(llm, hits=scripted_hits([hit(1)]))
    run = start(client, auth(client, "alice@example.com"))
    assert run["report"]["findings"] == [] and run["report"]["off_topic"][0]["text"] == "Roles are assigned by the lead."
    assert run["status"] == "completed"                       # nothing on topic: nothing to approve
    assert "REQUEST THE REPORT ANSWERS:" in [p for p in llm.prompts if "STATEMENT:" in p][0]


def test_gap_covered_by_another_document_is_rejected(client, setup):
    llm = FakeLLM(claims=[DraftClaim(text="The guide does not mention rotation.", category="gap", sources=[1])], covered=True)
    setup(llm, hits=scripted_hits([hit(1)]))
    report = start(client, auth(client, "alice@example.com"))["report"]
    assert report["findings"] == [] and "Another document covers this" in report["unsupported"][0]["reason"]