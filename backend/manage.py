"""Admin commands (there is deliberately no public sign-up endpoint).

    python manage.py create-user alice@example.com --role employee
    python manage.py create-collection Engineering
    python manage.py add-member alice@example.com Engineering
    python manage.py list
    python manage.py load-samples      # (re)load sample-docs/ into their collections, process them now
    python manage.py reindex           # re-process every document (e.g. after changing chunk size)
    python manage.py evaluate --mode retrieval --label baseline   # run the benchmark (mode: retrieval|full)
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.ingestion import create_document, ingest_document
from app.models import ROLES, Collection, CollectionMember, Document, User
from app.security import audit, hash_password

SAMPLES = Path(__file__).resolve().parents[1] / "sample-docs"


def load_samples(db):
    """Replace each sample document (matched by collection + filename) with the current file."""
    admin = db.scalar(select(User).where(User.role == "admin").order_by(User.id))
    if admin is None:
        sys.exit("Create an admin user first.")
    ids = []
    for col_name, files in json.loads((SAMPLES / "collections.json").read_text()).items():
        col = db.scalar(select(Collection).where(Collection.name == col_name))
        if col is None:
            col = Collection(name=col_name)
            db.add(col)
            db.flush()
        for name in files:
            for old in db.scalars(select(Document).where(Document.collection_id == col.id, Document.filename == name)):
                Path(old.storage_path).unlink(missing_ok=True)
                db.delete(old)
            db.flush()
            ids.append(create_document(db, admin.id, col.id, name, (SAMPLES / name).read_bytes()).id)
    db.commit()
    return ids


def evaluate(mode, label, types=None):
    from app.evaluation import run_evaluation
    from app.models import EvaluationRun

    def show(i, total, row):
        status = "" if mode == "retrieval" else (" refused" if row["abstained"] else
                                                  f" missing={row['missing']}" if row["missing"] else " ok")
        print(f"  [{i:>2}/{total}] {row['id']} {row['type']:<12} rank={row['gold_rank']} "
              f"best={row['best_distance']}{status}", flush=True)

    with SessionLocal() as db:
        run = EvaluationRun(label=label, mode=mode)
        db.add(run)
        db.commit()
        run_evaluation(db, run, show, types)
        print(f"\nRun #{run.id} '{label}' ({mode}) finished in {run.duration_s}s")
        for key, value in run.metrics.items():
            if key != "by_type":
                print(f"  {key:<24} {value}")
        print("  by type:")
        for t, v in run.metrics["by_type"].items():
            print(f"    {t:<13} {v}")


def process(ids):
    with SessionLocal() as db:
        for doc_id in ids:
            ingest_document(doc_id)
            doc = db.get(Document, doc_id)
            db.refresh(doc)
            print(f"  #{doc.id:<3} {doc.filename:<34} {doc.status:<7} chunks={doc.chunk_count} {doc.error or ''}")


def find(db, model, **by):
    obj = db.scalar(select(model).filter_by(**by))
    if obj is None:
        sys.exit(f"Not found: {model.__name__} {by}")
    return obj


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("create-user")
    u.add_argument("email")
    u.add_argument("--role", choices=ROLES, default="employee")
    u.add_argument("--password", help="omit to be prompted (safer: not saved in shell history)")
    sub.add_parser("create-collection").add_argument("name")
    m = sub.add_parser("add-member")
    m.add_argument("email")
    m.add_argument("collection")
    sub.add_parser("list")
    sub.add_parser("load-samples")
    sub.add_parser("reindex")
    ev = sub.add_parser("evaluate")
    ev.add_argument("--mode", choices=("retrieval", "full"), default="retrieval")
    ev.add_argument("--label", required=True)
    ev.add_argument("--types", help="comma-separated question types, e.g. injection,permission (quick check)")
    args = p.parse_args()

    if args.cmd == "evaluate":
        evaluate(args.mode, args.label, set(args.types.split(",")) if args.types else None)
        return

    if args.cmd in ("load-samples", "reindex"):
        with SessionLocal() as db:
            if args.cmd == "load-samples":
                ids = load_samples(db)
            else:
                docs = db.scalars(select(Document).order_by(Document.id)).all()
                for doc in docs:
                    doc.status = "queued"  # deliberate reset so the pipeline runs again
                db.commit()
                ids = [d.id for d in docs]
        process(ids)
        return

    with SessionLocal() as db:
        if args.cmd == "create-user":
            password = args.password or getpass.getpass("Password: ")
            if len(password) < 12:
                sys.exit("Password must be at least 12 characters.")
            user = User(email=args.email.strip().lower(), password_hash=hash_password(password), role=args.role)
            db.add(user)
            db.flush()
            audit(db, None, "admin.create_user", "user", user.id, role=args.role)
            print(f"Created user {user.id}: {user.email} ({user.role})")
        elif args.cmd == "create-collection":
            col = Collection(name=args.name)
            db.add(col)
            db.flush()
            audit(db, None, "admin.create_collection", "collection", col.id, name=args.name)
            print(f"Created collection {col.id}: {col.name}")
        elif args.cmd == "add-member":
            user = find(db, User, email=args.email.strip().lower())
            col = find(db, Collection, name=args.collection)
            db.merge(CollectionMember(collection_id=col.id, user_id=user.id))
            audit(db, None, "admin.add_member", "collection", col.id, user_id=user.id)
            print(f"{user.email} is now a member of {col.name}")
        else:
            for user in db.scalars(select(User).order_by(User.id)):
                cols = db.scalars(select(Collection.name).join(CollectionMember)
                                  .where(CollectionMember.user_id == user.id)).all()
                print(f"{user.id:>3}  {user.email:<28} {user.role:<9} {', '.join(cols) or '-'}")
        db.commit()


if __name__ == "__main__":
    main()
