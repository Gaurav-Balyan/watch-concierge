"""
Logging for the MCP servers.

Why a file: an MCP server runs as a subprocess of the agent, so its stderr is not
reliably visible in your terminal (on your Windows setup it was not shown at all).
A log file always works, survives the run, and is what you read when something
fails, for example when play_title returns TV_UNREACHABLE.

    logs/device_mcp.log
"""
import logging
import sys
from logging.handlers import RotatingFileHandler

import config

LOG_DIR = config.BASE_DIR / "logs"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        LOG_DIR.mkdir(exist_ok=True)
        fh = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass   # logging must never stop the server
    sh = logging.StreamHandler(sys.stderr)   # stderr only; never stdout (that is the protocol channel)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


class LogStream:
    """A file-like object that turns print() output into log lines.

    Used to capture the progress prints made by tv.py ("waiting for TV...",
    "key: ENTER") so they land in the log, not on the protocol channel.
    """

    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self.logger, self.level, self._buf = logger, level, ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, "tv: %s", line.strip())
        return len(text)

    def flush(self):
        if self._buf.strip():
            self.logger.log(self.level, "tv: %s", self._buf.strip())
        self._buf = ""