"""
App/debug logging setup for the Streamlit dashboard — a plain stdlib
`logging` config writing to logs/dashboard.log (rotating) plus console.

Streamlit reruns the whole script on every widget interaction, so
setup_logging() guards against re-adding handlers on each rerun (which
would otherwise duplicate every log line).

Usage, in any dashboard_lib module or page:
    import logging
    logger = logging.getLogger(__name__)
    logger.info(...)/.warning(...)/.exception(...)

dashboard.py calls setup_logging() once; every other module just grabs
its own logger via logging.getLogger(__name__) as usual — no per-module
setup needed, they all feed into the handlers configured here.
"""
import logging
import logging.handlers
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "dashboard.log")

_CONFIGURED = False


def setup_logging(level=logging.INFO):
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Scryfall calls are chatty at INFO (one line per card during a
    # full refresh/migration) — quiet the underlying HTTP library down
    # to warnings so it doesn't drown out the app's own log lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
