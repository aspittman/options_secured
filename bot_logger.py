import logging
from pathlib import Path

from config import LOG_FILE


def setup_logging():
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ],
        force=True
    )


def bot_log(message, level=logging.INFO):
    logging.getLogger("options_bot").log(level, message)
