# app/logging_config.py
import logging
import logging.handlers
import os
from .config import LOG_DIR, LOG_FILE, LOG_LEVEL

LOG_DIR.mkdir(parents=True, exist_ok=True)

def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logger = logging.getLogger("whatsapp_manager")
    logger.setLevel(level)

    # formatter
    fmt = logging.Formatter(fmt="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # console handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # rotating file handler
    fh = logging.handlers.RotatingFileHandler(str(LOG_FILE), maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # silence noisy libs if needed
    logging.getLogger("playwright").setLevel(logging.WARNING)
    return logger

logger = setup_logging()