import logging
from logging import Logger
from pathlib import Path

from config import load_config

_cfg = load_config()
PROJECT_ROOT = Path(_cfg["_project_root"])
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

_LOG_FILE = LOG_DIR / "application.log"
_FORMAT = "[ %(asctime)s ] %(name)s - %(levelname)s - %(message)s"


def configLogger(logger_name: str) -> Logger:
    """
    Configure and return a logger. All loggers share logs/application.log.
    Log level comes from config['logging']['level'].
    Safe to call repeatedly — handlers are attached only once.
    """
    level = getattr(logging, _cfg["logging"]["level"])

    # Clean, stable logger name derived from the module path
    try:
        rel = Path(logger_name).resolve().relative_to(PROJECT_ROOT)
        logger_name = str(rel.with_suffix("")).replace("\\", ".").replace("/", ".")
    except Exception:
        logger_name = Path(logger_name).stem

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    if logger.hasHandlers():
        return logger

    formatter = logging.Formatter(_FORMAT)

    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger