"""Reset an EXAM account's password from this computer (for a forgotten administrator password).

    python -m tools.reset_password ranjith

The new password is typed at a hidden prompt; it is stored only as a hash.
"""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from app import db
from app.models import AuthSession, User
from app.security import audit, hash_password


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m tools.reset_password <username>")
    db.configure()
    with db.new_session() as s:
        user = s.scalars(select(User).where(User.username == sys.argv[1].strip().lower())).first()
        if user is None:
            names = ", ".join(u.username for u in s.scalars(select(User)))
            raise SystemExit(f"no user {sys.argv[1]!r}; existing usernames: {names}")
        first = getpass.getpass("New password (8+ characters): ")
        if len(first) < 8:
            raise SystemExit("too short; nothing changed")
        if getpass.getpass("Repeat it: ") != first:
            raise SystemExit("the two entries differ; nothing changed")
        user.password_hash = hash_password(first)
        user.active = True
        for row in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
            s.delete(row)  # sign out everywhere
        audit(s, user, "password_reset_locally", "user", user.id)
        s.commit()
    print(f"Password for {user.username} reset. Sign in at http://localhost:8004")


if __name__ == "__main__":
    main()
