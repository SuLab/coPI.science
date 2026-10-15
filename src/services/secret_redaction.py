"""The one rule for keeping API keys out of logs and stored error text (R1.1 of the
2026-10-15 publication-handling design; SEC-1, SEC-2).

``API_KEY_QUERY`` matches an ``api_key=<value>`` query parameter in any case, and
``redact_api_keys`` rewrites the value to ``REDACTED``. It is idempotent, and it is the
only definition of the rule: ``src.services.pubmed``, ``src.services.job_queue`` and
``scripts/scrub_job_error_api_keys.py`` all call it.

``install_log_redaction`` wraps the process's log-record factory, so a record is redacted
when it is created, whatever its logger name, before any handler formats it: its
message, its string arguments, the traceback text of ``exc_info`` and ``stack_info``. It
also adds ``RedactApiKeyFilter`` to the loggers it names, as a second layer for records
built outside the factory. A leaf module: stdlib only (tests/unit/test_leaf_modules.py).
"""

import logging
import re
from collections.abc import Mapping

API_KEY_QUERY = re.compile(r"(?i)(\bapi_key=)[^&\s\"']+")

#: Set on the wrapping factory, so a second install does not wrap it again.
_FACTORY_MARK = "_copi_api_key_redaction"


def redact_api_keys(text: str) -> str:
    """``text`` with every ``api_key=`` value replaced by ``REDACTED``. Idempotent."""
    return API_KEY_QUERY.sub(r"\1REDACTED", text)


def _redacted_value(value: object) -> object:
    """``value`` unchanged unless its ``str()`` carries a key, then that ``str()``
    redacted. Numbers are never touched, so ``%d`` still formats; a ``__str__`` that
    raises leaves the value as it was."""
    if isinstance(value, int | float | bool):
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - logging must never raise from a bad argument
        return value
    return redact_api_keys(text) if API_KEY_QUERY.search(text) else value


def _redacted_args(args: object) -> object:
    if isinstance(args, tuple):
        return tuple(_redacted_value(arg) for arg in args)
    if isinstance(args, Mapping):
        return {key: _redacted_value(value) for key, value in args.items()}
    return args


def _redact_exc_text(record: logging.LogRecord) -> None:
    """Preset ``exc_text`` to the redacted traceback when the traceback carries a key.
    ``logging.Formatter.format`` uses a preset ``exc_text`` instead of rendering one."""
    if not record.exc_info:
        return
    try:
        text = logging.Formatter().formatException(record.exc_info)
    except Exception:  # noqa: BLE001 - an unrenderable traceback is left to the formatter
        return
    if API_KEY_QUERY.search(text):
        record.exc_text = redact_api_keys(text)


def redact_log_record(record: logging.LogRecord) -> None:
    """Redact ``record`` in place; never raises."""
    if isinstance(record.msg, str):
        record.msg = redact_api_keys(record.msg)
    else:
        record.msg = _redacted_value(record.msg)
    record.args = _redacted_args(record.args)
    _redact_exc_text(record)
    if isinstance(record.stack_info, str) and API_KEY_QUERY.search(record.stack_info):
        record.stack_info = redact_api_keys(record.stack_info)


class RedactApiKeyFilter(logging.Filter):
    """``redact_log_record`` as a logger filter; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_log_record(record)
        return True


def install_log_redaction(*logger_names: str) -> None:
    """Wrap the log-record factory once per process, and add one ``RedactApiKeyFilter``
    to each named logger that has none. Idempotent."""
    previous = logging.getLogRecordFactory()
    if not getattr(previous, _FACTORY_MARK, False):

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            redact_log_record(record)
            return record

        setattr(factory, _FACTORY_MARK, True)
        logging.setLogRecordFactory(factory)
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactApiKeyFilter) for f in logger.filters):
            logger.addFilter(RedactApiKeyFilter())
