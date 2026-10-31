"""Weeks 1-2 milestone: embed 3 paragraphs, retrieve the closest one, answer from it with a citation."""
import os

import httpx
import psycopg
from dotenv import load_dotenv
from pgvector import Vector
from pgvector.psycopg import register_vector

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
DB_URL = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
OLLAMA = os.environ["OLLAMA_URL"]
CHAT_MODEL = os.environ["CHAT_MODEL"]
EMBED_MODEL = os.environ["EMBED_MODEL"]

PARAGRAPHS = [
    "Expense reports must be approved by the employee's line manager within 5 working days.",
    "API requests are authenticated with an HMAC-SHA256 signature sent in the X-Signature header.",
    "The office is closed on public holidays; the help desk remains reachable by email.",
]
QUESTION = "How do we sign API requests?"


def embed(texts: list[str]) -> list[list[float]]:
    r = httpx.post(f"{OLLAMA}/api/embed", json={"model": EMBED_MODEL, "input": texts}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]


def main():
    vectors = embed(PARAGRAPHS)
    dim = len(vectors[0])
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        register_vector(conn)
        conn.execute("DROP TABLE IF EXISTS hello_chunks")
        conn.execute(f"CREATE TABLE hello_chunks (id int PRIMARY KEY, content text, embedding vector({dim}))")
        for i, (text, vec) in enumerate(zip(PARAGRAPHS, vectors), start=1):
            conn.execute("INSERT INTO hello_chunks VALUES (%s, %s, %s)", (i, text, Vector(vec)))

        q = Vector(embed([QUESTION])[0])
        rows = conn.execute(
            "SELECT id, content, embedding <=> %s AS distance FROM hello_chunks ORDER BY distance LIMIT 3", (q,)
        ).fetchall()

    print(f"Embedding dimensions: {dim}\nQuestion: {QUESTION}\n\nRetrieved (closest first):")
    for id_, content, dist in rows:
        print(f"  [{id_}] distance={dist:.3f}  {content}")
    assert rows[0][0] == 2, "retrieval should rank the API-signing paragraph first"

    best_id, best_text, _ = rows[0]
    prompt = (
        "Answer using ONLY the evidence below and cite it like [1]. If the evidence does not contain "
        'the answer, reply exactly: "I could not find sufficient evidence in the available documents."\n\n'
        f"EVIDENCE:\n[{best_id}] {best_text}\n\nQUESTION: {QUESTION}"
    )
    r = httpx.post(
        f"{OLLAMA}/api/chat",
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False, "think": False},
        timeout=600,
    )
    r.raise_for_status()
    answer = r.json()["message"]["content"].strip()
    print(f"\nAnswer:\n  {answer}")
    assert f"[{best_id}]" in answer, "answer should cite the retrieved passage"
    print("\nHELLO RAG: OK")


if __name__ == "__main__":
    main()
