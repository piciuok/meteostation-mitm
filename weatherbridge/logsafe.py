"""Global log redaction: station credentials must never reach any log output.

Application code masks explicitly; this is a safety net for third-party log records
(e.g. aiohttp's HTTP parser logs the raw request line on malformed requests).
"""

from __future__ import annotations

import logging
import re

_PATTERNS = (
    (re.compile(r"(?i)(?<![A-Za-z0-9_])(ID|PASSWORD|wid|key)=([^&\s'\"\\]+)"), r"\1=***"),
    (re.compile(r"(?i)/(wid|key)/([^/\s?'\"\\]+)"), r"/\1/***"),
)

_installed = False


def scrub(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def install() -> None:
    """Wrap the LogRecord factory so every record's message and traceback is scrubbed."""
    global _installed
    if _installed:
        return
    _installed = True
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        try:
            record.msg = scrub(record.getMessage())
            record.args = None
            if record.exc_info and record.exc_info[1] is not None:
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        except Exception:  # never break logging
            pass
        return record

    logging.setLogRecordFactory(factory)
