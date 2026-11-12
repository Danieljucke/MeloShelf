"""Keeps credentials, the session token and private paths out of every log record.

The redaction runs when a record is created (``logging.setLogRecordFactory``), so it covers every logger and
handler and the text of tracebacks, including call sites written later. Diagnostic value is kept: the message,
the exception class and the traceback shape remain; only the sensitive values are replaced.

- secrets registered at runtime (the session token, the AcoustID key, the MusicBrainz contact) become [redacted]
- a ``client=`` query value (the AcoustID key in a request URL) becomes [redacted] even if it was never registered
- the home directory becomes ~, so the account name is not written to the log
"""
import logging
import re
import threading
import traceback
from pathlib import Path

REDACTED = '[redacted]'
_lock = threading.Lock()
_secrets: set[str] = set()
_installed = False
_CLIENT = re.compile(r'(\bclient=)[^&\s\'"<>]+')
_TOKEN_HEADER = re.compile(r'(X-MeloShelf-Token["\']?\s*[:=]\s*["\']?)[^\s"\',}]+', re.IGNORECASE)


def _home_pattern():
    home = str(Path.home())
    variants = {home, home.replace('\\', '/')}
    return re.compile('|'.join(re.escape(v) for v in sorted(variants, key=len, reverse=True)), re.IGNORECASE)


_HOME = _home_pattern()


def register(value) -> None:
    """Treat ``value`` as a secret in every later log record. Short values are ignored to avoid erasing text."""
    text = str(value or '').strip()
    if len(text) >= 6:
        with _lock:
            _secrets.add(text)


def forget_all() -> None:
    """For tests: drop the registered secrets."""
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = _CLIENT.sub(r'\1' + REDACTED, text)
    text = _TOKEN_HEADER.sub(r'\1' + REDACTED, text)
    return _HOME.sub('~', text)


def install() -> None:
    """Install the redacting record factory once per process."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # a malformed format string: keep the raw template rather than lose the record
            message = str(record.msg)
        record.msg, record.args = redact(message), None
        if record.exc_info:
            record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)).rstrip('\n'))
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    logging.setLogRecordFactory(factory)
