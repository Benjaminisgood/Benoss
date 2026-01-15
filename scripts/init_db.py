import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.extensions import db
from app.models import User


def seed_admin():
    import os

    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        return
    if User.query.filter_by(username=username).first():
        return
    user = User(username=username, role="admin")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        seed_admin()


if __name__ == "__main__":
    main()
