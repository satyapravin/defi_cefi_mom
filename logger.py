import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "ts": record.created,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if hasattr(record, "_extra"):
            log_data.update(record._extra)
        return json.dumps(log_data)


class StructuredLogger:
    """Thin wrapper that supports ``logger.info("msg", key=val)`` style calls
    with structured JSON output."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, msg: str, **kwargs) -> None:
        record = self._logger.makeRecord(
            self._logger.name, level, "(structured)", 0, msg, (), None
        )
        record._extra = kwargs  # type: ignore[attr-defined]
        self._logger.handle(record)

    def debug(self, msg: str, **kwargs) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs) -> None:
        self._log(logging.ERROR, msg, **kwargs)

    def exception(self, msg: str, **kwargs) -> None:
        self._log(logging.ERROR, msg, **kwargs)


def setup_logger(level: str = "INFO") -> StructuredLogger:
    logger = logging.getLogger("momentum")
    if not logger.handlers:
        logger.setLevel(getattr(logging, level.upper()))
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return StructuredLogger(logger)
