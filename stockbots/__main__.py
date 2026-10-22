"""Start the bots and the dashboard: python -m stockbots"""
from __future__ import annotations

import logging
import re
import sys

import uvicorn

from .app import App
from .config import Config, env_bool
from .web import create_web

KEYISH = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|[A-Z0-9]{20,}|[A-Za-z0-9]{32,})")


class Redact(logging.Filter):
    """Never let anything that looks like a key reach the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = KEYISH.sub("[redacted]", msg)
        if clean != msg:
            record.msg = clean
            record.args = ()
        return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(Redact())
    cfg = Config()
    if env_bool("DEMO", False):
        from .demo import build_demo_app

        app = build_demo_app(cfg)
    else:
        app = App(cfg)
        if app.settings.setup_done:
            app.start_bots()
        else:
            logging.getLogger("stockbots").info(
                "Waiting for Setup: open this server's address in a browser (open for %d minutes).",
                cfg.setup_window_min,
            )
    web = create_web(app)
    uvicorn.run(web, host="0.0.0.0", port=cfg.port, proxy_headers=True, forwarded_allow_ips="*",
                log_level="warning")


if __name__ == "__main__":
    main()
