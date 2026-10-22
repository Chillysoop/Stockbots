"""Start Setup over (new keys, new names, forgotten password).

    docker compose exec app python -m stockbots.reset
    docker compose restart app

Trade history in the database is kept.
"""
from __future__ import annotations

import os

from .config import Config


def main() -> None:
    cfg = Config()
    if os.path.exists(cfg.settings_path):
        os.remove(cfg.settings_path)
        print("Setup cleared. Restart the app container, then open the site to run Setup again.")
    else:
        print("Nothing to reset: Setup hasn't been run yet.")


if __name__ == "__main__":
    main()
