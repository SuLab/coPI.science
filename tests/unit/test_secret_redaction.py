"""R1.1: one redaction helper, applied to every log record whatever its logger name,
traceback text included (the 2026-10-15 publication-handling design, SEC-1/2/4)."""
import logging
import re
from pathlib import Path

import httpx
import pytest

from src.services.secret_redaction import (
    RedactApiKeyFilter,
    install_log_redaction,
    redact_api_keys,
)

ROOT = Path(__file__).resolve().parents[2]


def test_redact_api_keys_is_case_insensitive_and_idempotent():
    once = redact_api_keys("API_KEY=abc&x=1 api_key=REDACTED")
    assert once == "API_KEY=REDACTED&x=1 api_key=REDACTED"
    assert redact_api_keys(once) == once


@pytest.mark.parametrize("name", ["tests.redaction.elsewhere", "httpx._client"])
def test_a_record_on_any_logger_name_is_redacted(name, caplog):
    install_log_redaction()
    logging.getLogger(name).warning(
        "GET %s", httpx.URL("https://x.org/e?api_key=SECRETKEY0123&db=1")
    )
    message = caplog.records[-1].getMessage()
    assert "api_key=REDACTED&db=1" in message
    assert "SECRETKEY0123" not in message


def test_a_traceback_carrying_a_key_is_redacted(caplog):
    install_log_redaction()
    logger = logging.getLogger("tests.redaction.traceback")
    try:
        raise RuntimeError("for url 'https://x.org/e?api_key=SECRETKEY0123'")
    except RuntimeError:
        logger.exception("boom")
    text = logging.Formatter().format(caplog.records[-1])
    assert "api_key=REDACTED" in text
    assert "SECRETKEY0123" not in text


def test_an_int_argument_still_formats(caplog):
    install_log_redaction()
    caplog.set_level(logging.INFO)
    logging.getLogger("tests.redaction.int").info("%s %d", "api_key=SECRETKEY0123", 404)
    assert caplog.records[-1].getMessage() == "api_key=REDACTED 404"


def test_an_argument_whose_str_raises_does_not_break_logging(monkeypatch):
    """The factory builds (and redacts) the record before any handler runs. pytest's
    capture handlers re-raise a formatting error themselves, so this logger has none."""
    install_log_redaction()

    class Boom:
        def __str__(self):
            raise ValueError("unprintable")

    logger = logging.getLogger("tests.redaction.boom")
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(logger, "level", logging.INFO)
    logger.info("%s", Boom())  # returns normally


def test_install_is_idempotent():
    install_log_redaction("httpx")
    factory = logging.getLogRecordFactory()
    install_log_redaction("httpx")
    assert logging.getLogRecordFactory() is factory
    filters = logging.getLogger("httpx").filters
    assert sum(isinstance(f, RedactApiKeyFilter) for f in filters) == 1


def test_only_pubmed_reads_the_ncbi_key():
    """SEC-4: no script or other module reads the key, so none can log or store it."""
    hits = sorted(
        p.relative_to(ROOT).as_posix()
        for base in ("src", "scripts")
        for p in (ROOT / base).rglob("*.py")
        if re.search(r"ncbi_api_key", p.read_text(encoding="utf-8"))
    )
    assert hits == ["src/config.py", "src/services/pubmed.py"]
