"""Admin commands (there is deliberately no public sign-up endpoint).

    python manage.py create-user alice@example.com --role employee
    python manage.py create-collection Engineering
    python manage.py add-member alice@example.com Engineering
    python manage.py list
"""
import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ROLES, Collection, CollectionMember, User
from app.security import audit, hash_password


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
    args = p.parse_args()

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
