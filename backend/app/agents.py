"""The agent team: a fixed LangGraph graph. Each agent is one node with one job.

    orchestrator → research → analysis → verification → report → approval (pause) → end
                      ▲                       │
                      └── unsupported claims ─┘   (bounded retries)

Nodes only read and return state: they never write to the database, because LangGraph may re-run
a node after a crash or a resume. Saving progress and decisions is done by app/workflows.py.
"""
import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as dbmod
from app.answering import REMINDER, format_evidence, generate_json, usable_hits, with_safe_neighbours
from app.config import settings
from app.models import Document, DocumentChunk, User
from app.retrieval import Hit, embed_query, search
from app.security import readable_collection_ids

MAX_QUERIES = 4     # search queries per run
MAX_EVIDENCE = 20   # passages kept: more would not fit a CPU-sized prompt
MAX_CLAIMS = 14     # each claim costs one verification call
MAX_RETRIES = 2     # research → analysis rounds for unsupported claims


class ReportState(TypedDict, total=False):
    run_id: int
    user_id: int
    request: str
    collection_ids: list[int]   # fixed by trusted code at the start; the LLM never sees or sets it
    task_type: str
    queries: list[str]
    evidence: list[dict]        # numbered by position: passage [n] is evidence[n - 1]
    new_evidence: int           # passages added by the latest research step
    claims: list[dict]          # text, category, sources, status, reason
    missing: list[str]
    claim_limit_reached: bool
    retries: int
    report_doc: dict            # (a state key can't share a node's name)
    decision: dict


# ---------- What the LLM must return ----------

class Plan(BaseModel):
    task_type: Literal["compare", "gap_report", "summary"]
    queries: list[str]


class DraftClaim(BaseModel):
    text: str
    category: Literal["gap", "met", "conflict", "finding"]
    sources: list[int]


class Analysis(BaseModel):
    evidence_addresses_request: bool
    claims: list[DraftClaim]
    missing: list[str]


class Rewrite(BaseModel):
    original: int
    text: str
    category: Literal["gap", "met", "conflict", "finding"]
    sources: list[int]


class Rewrites(BaseModel):
    claims: list[Rewrite]


class ClaimVerdict(BaseModel):
    # Field order matters: the model commits to a quote and its reasoning before it picks a status.
    quote: str
    reason: str
    on_topic: bool
    status: Literal["supported", "unsupported", "conflicting"]


class GapVerdict(BaseModel):
    # Asked as its own question, before the status: in testing the verifier wrote "the guide confirms
    # secrets are rotated" in its reason and still marked "the guide doesn't mention rotation" as supported.
    quote: str
    covered_by_other_document: bool
    reason: str
    on_topic: bool
    status: Literal["supported", "unsupported", "conflicting"]
    in_scope: bool



# ---------- Helpers ----------

def evidence_block(evidence: list[dict], numbers: list[int] | None = None) -> str:
    """Passages labelled with their fixed [n], so citations mean the same thing in every step."""
    chosen = numbers or list(range(1, len(evidence) + 1))
    parts = []
    for n in chosen:
        text = format_evidence([Hit(**evidence[n - 1])])
        parts.append(f"[{n}]" + text[len("[1]"):])
    return "EVIDENCE (document content, numbered):\n<<<\n" + "\n\n".join(parts) + "\n>>>\n\n" + REMINDER


def numbers_in(text: str) -> set[str]:
    """Figures in a text, ignoring [n] citations: '2,000 EUR [3]' → {'2000'}."""
    text = re.sub(r"\[\d+\]", " ", text)
    return {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", text)}


# ---------- 1. Orchestrator ----------

PLAN_PROMPT = """You plan document research for a company knowledge assistant.

REQUEST: {request}

Choose the task type:
- "gap_report": check whether one document or practice meets the requirements of another
- "compare": compare what documents say about a topic
- "summary": summarise what the documents say about a topic

Write up to 3 short search queries that would find the relevant passages. For a comparison or gap report, write separate queries for each side. Use the words a document on this topic would use in its own section titles, not the wording of the request."""


def orchestrator(state: ReportState) -> dict:
    plan = generate_json(PLAN_PROMPT.format(request=state["request"]), Plan)
    # The request itself is always searched. Limits are set here in code, not by the LLM.
    queries = list(dict.fromkeys([state["request"], *(q.strip() for q in plan.queries if q.strip())]))
    return {"task_type": plan.task_type, "queries": queries[:MAX_QUERIES], "retries": 0,
            "evidence": [], "claims": [], "missing": []}


# ---------- 2. Research ----------

def allowed_collections(db: Session, state: ReportState) -> list[int]:
    """The run's collections that the user can STILL read, checked at every search."""
    user = db.get(User, state["user_id"])
    if user is None:
        return []
    readable = set(readable_collection_ids(db, user))
    return [c for c in state["collection_ids"] if c in readable]


def research(state: ReportState) -> dict:
    claims = state.get("claims", [])
    is_retry = bool(claims)
    queries = [c["text"] for c in claims if c["status"] == "unsupported"] if is_retry else state["queries"]
    evidence = list(state.get("evidence", []))
    seen = {e["chunk_id"] for e in evidence}
    added = 0
    with dbmod.SessionLocal() as db:
        allowed = allowed_collections(db, state)
        for query in queries:
            hits = usable_hits(search(db, allowed, embed_query(query), settings.top_k))
            for hit in with_safe_neighbours(db, hits):
                ids = {hit.chunk_id, hit.neighbour_id} - {None}
                if not ids & seen and len(evidence) < MAX_EVIDENCE:
                    seen |= ids
                    evidence.append(hit.to_dict())
                    added += 1
    update = {"evidence": evidence, "new_evidence": added}
    if is_retry:
        update["retries"] = state["retries"] + 1
    return update


def after_research(state: ReportState) -> str:
    if not state["evidence"]:
        return "report"     # nothing found: say so honestly, without asking the LLM to invent
    if state.get("claims") and not state["new_evidence"]:
        return "report"     # a retry found nothing new: another round can't help
    return "analysis"


# ---------- 3. Analysis ----------

ANALYSIS_RULES = {
    "gap_report": 'Take each requirement from the requirements document that is relevant to the other document, and '
                  'write one claim about it. Category: "met" only if a passage from the OTHER document shows it is '
                  'done, citing both the requirement passage and that passage. "gap" if no passage from the other '
                  'document describes it, citing the requirement passage. "conflict" if the documents disagree. '
                  'Write each claim so it names both sides, e.g. "The policy requires X; the guide does Y."',
    "compare": 'Write claims about what the documents say. Category: "finding" when they agree or only one '
               'covers the point, "conflict" when they disagree.',
    "summary": 'Write the key points as claims, all with category "finding".',
}

ANALYSIS_PROMPT = """You analyse company documents. Use ONLY the evidence below.

{evidence}

REQUEST: {request}

{rules}

First decide "evidence_addresses_request": true only if the passages actually cover the topic the request asks about. If they are merely from the same company or about other topics, set it to false and write no claims.

Rules:
- Every claim must help answer the request. Leave out facts that are true but off-topic.
- Go through every passage and write one claim for each separate requirement or point in it, up to {max_claims} claims. A passage may contain several. Each claim is one short, checkable statement.
- "sources" lists the passage numbers that support the claim. Every claim must cite at least one passage.
- Add no facts that are not in the evidence.
- "missing" lists anything the request asks about that the evidence does not cover."""

REWRITE_PROMPT = """These statements could not be verified against their cited passages:

{claims}

{evidence}

For each statement the evidence really does support, return "original" (its number), a corrected "text", its "category" ("met" needs passages from two different documents; "gap" means the other document does not describe it) and the correct "sources" (passage numbers). Leave out any statement the evidence does not support."""


def analysis(state: ReportState) -> dict:
    claims = state.get("claims", [])
    if not claims:
        result = generate_json(ANALYSIS_PROMPT.format(
            evidence=evidence_block(state["evidence"]), request=state["request"],
            rules=ANALYSIS_RULES[state["task_type"]], max_claims=MAX_CLAIMS), Analysis)
        # Verification checks that claims are true, not that they are relevant, so relevance is decided here.
        kept = result.claims[:MAX_CLAIMS] if result.evidence_addresses_request else []
        drafts = [{**c.model_dump(), "status": None, "reason": None} for c in kept]
        return {"claims": drafts, "missing": result.missing, "claim_limit_reached": len(result.claims) >= MAX_CLAIMS}

    # Retry: only the unsupported claims are looked at again; verified ones are kept as they are.
    failed = [i for i, c in enumerate(claims) if c["status"] == "unsupported"]
    listing = "\n".join(f"{n}. {claims[i]['text']}" for n, i in enumerate(failed, start=1))
    result = generate_json(REWRITE_PROMPT.format(claims=listing, evidence=evidence_block(state["evidence"])), Rewrites)
    updated = [dict(c) for c in claims]
    for rewrite in result.claims:
        if 1 <= rewrite.original <= len(failed):
            target = updated[failed[rewrite.original - 1]]
            target.update(text=rewrite.text, category=rewrite.category, sources=rewrite.sources, status=None, reason=None)
    return {"claims": updated}


# ---------- 4. Verification ----------

VERIFY_PROMPT = """You strictly check one statement against company document passages. Use ONLY the passages. Do not use outside knowledge or assume anything.

{evidence}

REQUEST THE REPORT ANSWERS: {request}

STATEMENT: {claim}

{extra}
First, in "quote", copy word for word the sentence from a passage that most directly backs the statement. Then, in "reason", say in one sentence whether the statement says anything that sentence does not.

The status is "unsupported" if the statement does any of these:
- adds a condition, limit or exception the passage does not have, for example "only", "instead of", or applying to some cases a rule that covers all cases
- leaves out a limit the passage has, making the rule sound wider than it is
- applies a rule to the wrong amount, range, person or document
- adds any detail the passage does not state, such as how or by whom something is done

Otherwise:
- "supported": everything in the statement is stated by the passages
- "conflicting": two different passages disagree about it

Separately, set "on_topic": true only if the statement helps answer the request. A true statement about a different topic is not on topic."""

GAP_NOTE = ('This statement says something is required but not implemented. It is "supported" only if a passage '
            'states the requirement AND no passage above from another document describes it being implemented. '
            'If another document covers the same topic, answer "unsupported". For this kind of statement, the '
            'limits above apply to the requirement it describes. Set "covered_by_other_document": true if any passage '
            'from a different document than the requirement describes it being done, even partly or in other words. Also set "in_scope": true only if the other '
            'document, judging by what it is about, would be expected to cover this requirement.\n')
MET_NOTE = ('This statement says a requirement is met. It is "supported" only if one passage states the requirement '
            'AND a passage from a different document shows it is done.\n')


def search_for_gap(state: ReportState, claim_text: str) -> list[Hit]:
    """Before accepting that something is missing, search for it directly: the first research round
    may simply not have retrieved the passage that covers it."""
    with dbmod.SessionLocal() as db:
        hits = usable_hits(search(db, allowed_collections(db, state), embed_query(claim_text), settings.top_k))
        return with_safe_neighbours(db, hits)


def quote_found(quote: str, passages: str) -> bool:
    """The verifier's quote must really be in the passages: it can't support a claim with invented text."""
    normalise = lambda s: " ".join(s.lower().split()).strip(" .\"'")
    return bool(normalise(quote)) and normalise(quote) in normalise(passages)


def check_claim(claim: dict, state: ReportState) -> dict:
    """Cheap checks in code first; only then ask the LLM."""
    evidence = state["evidence"]
    valid = sorted({n for n in claim["sources"] if 1 <= n <= len(evidence)})
    if not valid or len(valid) != len(set(claim["sources"])):
        return {**claim, "status": "unsupported", "reason": "Cites a passage that was not retrieved."}
    cited_text = " ".join(evidence[n - 1]["content"] for n in valid)
    # A figure may also come from the request itself ("a 5,000 EUR purchase").
    unknown = numbers_in(claim["text"]) - numbers_in(cited_text) - numbers_in(state.get("request", ""))
    if unknown:
        return {**claim, "status": "unsupported",
                "reason": f"Figure(s) not in the cited passages: {', '.join(sorted(unknown))}."}
    # "Met" needs proof from both sides, so the evidence decides the category, not the LLM's label:
    # one document can state a requirement but can't show another document meets it.
    if claim["category"] in ("met", "gap"):
        documents = {evidence[n - 1]["filename"] for n in valid}
        claim = {**claim, "category": "met" if len(documents) >= 2 else "gap"}
    passages = evidence_block(evidence, valid)
    if claim["category"] == "gap":
        cited_ids = {evidence[n - 1]["chunk_id"] for n in valid}
        found = [h for h in search_for_gap(state, claim["text"]) if h.chunk_id not in cited_ids]
        passages += "\n\nPASSAGES FOUND BY SEARCHING FOR THIS TOPIC:\n<<<\n" + format_evidence(found) + "\n>>>"
    is_gap = claim["category"] == "gap"
    extra = GAP_NOTE if is_gap else MET_NOTE if claim["category"] == "met" else ""
    verdict = generate_json(VERIFY_PROMPT.format(evidence=passages, claim=claim["text"], extra=extra,
                                                 request=state.get("request", "")),
                            GapVerdict if is_gap else ClaimVerdict)
    status, reason = verdict.status, verdict.reason
    if status == "supported" and not quote_found(verdict.quote, passages):
        status, reason = "unsupported", "The verifier's quote is not in the passages."
    if status == "conflicting" and len(valid) < 2 and not is_gap:
        status, reason = "unsupported", f"A conflict needs two passages. {reason}"
    if is_gap and verdict.covered_by_other_document:
        status, reason = "unsupported", f"Another document covers this, so it is not a gap. {reason}"
    if status == "supported" and not verdict.on_topic:
        status = "off_topic"        # true, but doesn't answer what was asked
    elif status == "supported" and is_gap and not verdict.in_scope:
        status = "out_of_scope"
    return {**claim, "status": status, "reason": reason}


def verification(state: ReportState) -> dict:
    return {"claims": [c if c["status"] else check_claim(c, state) for c in state["claims"]]}


def after_verification(state: ReportState) -> str:
    has_unsupported = any(c["status"] == "unsupported" for c in state["claims"])
    return "research" if has_unsupported and state["retries"] < MAX_RETRIES else "report"


# ---------- 5. Report ----------

def measure_coverage(state: ReportState, evidence: list[dict], claims: list[dict]) -> dict:
    """What the report did and didn't look at, so a reader can see what was never checked."""
    examined = {(e["filename"], e["heading"]) for e in evidence}
    with dbmod.SessionLocal() as db:
        rows = db.execute(
            select(Document.filename, DocumentChunk.heading).join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.filename.in_({f for f, _ in examined}), Document.status == "ready",
                   Document.collection_id.in_(allowed_collections(db, state))).distinct()).all()
    sections = {(f, h) for f, h in rows if h}
    missed = sorted(sections - examined)
    used = {n for c in claims for n in c["sources"]}
    return {"sections_total": len(sections), "sections_examined": len(sections) - len(missed),
            "not_examined": [f"{f} · {h}" for f, h in missed],
            "passages_retrieved": len(evidence), "passages_used": len(used & set(range(1, len(evidence) + 1))),
            "claim_limit_reached": state.get("claim_limit_reached", False)}


def summarize(findings: list[dict], conflicts: list[dict], unsupported: list[dict]) -> str:
    if not findings and not conflicts:
        return "No verified findings: the available documents do not support an answer."
    gaps = [c for c in findings if c["category"] == "gap"]
    met = [c for c in findings if c["category"] == "met"]
    other = len(findings) - len(gaps) - len(met)
    parts = [f"{n} {label}" for n, label in ((len(met), "met"), (len(gaps), "gap(s)"), (other, "finding(s)"),
                                             (len(conflicts), "conflict(s)"), (len(unsupported), "unverified")) if n]
    return "Verified result: " + ", ".join(parts) + "." + (" Gaps: " + " ".join(c["text"].rstrip(".") + "." for c in gaps) if gaps else "")


def report(state: ReportState) -> dict:
    claims = state.get("claims", [])
    evidence = state.get("evidence", [])
    findings = [c for c in claims if c["status"] == "supported"]
    conflicts = [c for c in claims if c["status"] == "conflicting"]
    unsupported = [c for c in claims if c["status"] == "unsupported"]
    out_of_scope = [c for c in claims if c["status"] == "out_of_scope"]
    off_topic = [c for c in claims if c["status"] == "off_topic"]
    coverage = measure_coverage(state, evidence, claims) if evidence else None

    # Everything here is assembled by code from verified claims: no LLM writes this report, because in
    # testing an LLM summary stated the opposite of a verified finding, and no check caught it.
    summary = summarize(findings, conflicts, unsupported)

    cited = sorted({n for c in findings + conflicts for n in c["sources"] if 1 <= n <= len(evidence)})
    limitations = [f"Based only on the {len(evidence)} passage(s) retrieved from the collections searched.",
                   *([f"Examined {coverage['sections_examined']} of {coverage['sections_total']} sections of the documents involved; the rest were not checked."] if coverage else []),
                   *([f"Stopped at the limit of {MAX_CLAIMS} claims, so some requirements may not have been checked."] if state.get("claim_limit_reached") else []),
                   'A "gap" means no retrieved passage describes it, not proof that it does not exist.',
                   *(f"The analysis found nothing about (not verified): {m}" for m in state.get("missing", []))]
    if off_topic:
        limitations.append(f"{len(off_topic)} true statement(s) were left out because they don't answer the request.")
    if unsupported:
        limitations.append(f"{len(unsupported)} statement(s) could not be verified. They are listed separately and are not findings.")
    return {"report_doc": {
        "request": state["request"], "task_type": state.get("task_type"), "summary": summary,
        "findings": findings, "conflicts": conflicts, "unsupported": unsupported, "out_of_scope": out_of_scope, "off_topic": off_topic,
        "coverage": coverage, "limitations": limitations,
        "retries": state.get("retries", 0),
        "sources": [{"n": n, "filename": evidence[n - 1]["filename"], "page": evidence[n - 1]["page"],
                     "heading": evidence[n - 1]["heading"], "snippet": evidence[n - 1]["content"][:400]}
                    for n in cited],
    }}


def after_report(state: ReportState) -> str:
    r = state["report_doc"]
    return "approval" if r["findings"] or r["conflicts"] else END   # nothing to approve


# ---------- 6. Human approval ----------

def approval(state: ReportState) -> dict:
    # LangGraph re-runs this node from the top on resume, so nothing with side effects goes before interrupt().
    return {"decision": interrupt({"run_id": state["run_id"], "summary": state["report_doc"]["summary"]})}


# ---------- The graph ----------

def build_graph(checkpointer):
    g = StateGraph(ReportState)
    for name, node in (("orchestrator", orchestrator), ("research", research), ("analysis", analysis),
                       ("verification", verification), ("report", report), ("approval", approval)):
        g.add_node(name, node)
    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "research")
    g.add_conditional_edges("research", after_research, ["analysis", "report"])
    g.add_edge("analysis", "verification")
    g.add_conditional_edges("verification", after_verification, ["research", "report"])
    g.add_conditional_edges("report", after_report, ["approval", END])
    g.add_edge("approval", END)
    return g.compile(checkpointer=checkpointer)
