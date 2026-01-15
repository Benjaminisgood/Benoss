import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.routes.album import reindex_all


def main():
    force = "--force" in sys.argv
    app = create_app()
    with app.app_context():
        result = reindex_all(force=force)
        print(result)


if __name__ == "__main__":
    main()
