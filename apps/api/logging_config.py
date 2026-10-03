import errno
import logging

from forma_core.config import config
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from forma_core.debug import debug_mode_enabled
from forma_core.logs import resolve_backend_log_path


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
SERVERLESS_ENV_VARS = ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV")


class _LoggerNamespaceFilter(logging.Filter):
    def __init__(self, namespaces: Tuple[str, ...]) -> None:
        super().__init__()
        self.namespaces = namespaces

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.namespaces:
            return True
        logger_name = record.name or ""
        return any(logger_name == namespace or logger_name.startswith(f"{namespace}.") for namespace in self.namespaces)


def _log_level(value: Optional[str]) -> int:
    if not value:
        return logging.INFO
    normalized = value.strip().upper()
    return getattr(logging, normalized, logging.INFO)


def _env_bool(name: str, default: bool = False) -> bool:
    return config.boolean(name, default)


def _serverless_runtime_detected() -> bool:
    return any(config.get(name) for name in SERVERLESS_ENV_VARS)


def _tmp_log_fallback_path(path: Path) -> Optional[Path]:
    if not _env_bool("BACKEND_LOG_TMP_FALLBACK", True):
        return None

    tmp_dir = Path(config.get("TMPDIR") or "/tmp").expanduser()
    try:
        resolved_tmp_dir = tmp_dir.resolve()
        resolved_path = path.resolve()
    except OSError:
        resolved_tmp_dir = tmp_dir
        resolved_path = path

    if resolved_path.is_relative_to(resolved_tmp_dir):
        return None
    if not _serverless_runtime_detected():
        return None
    return (resolved_tmp_dir / path.name).resolve()


def _log_namespaces() -> Tuple[str, ...]:
    raw_value = config.get("BACKEND_LOG_NAMESPACES") or config.get("FORMA_LOG_NAMESPACES") or ""
    if not raw_value.strip():
        return ()
    namespaces = []
    for namespace in re.split(r"[,\s]+", raw_value.strip()):
        normalized = namespace.strip().rstrip(".")
        if normalized and normalized != "*":
            namespaces.append(normalized)
    return tuple(dict.fromkeys(namespaces))


def _ensure_namespace_filter(handler: logging.Handler, namespaces: Tuple[str, ...]) -> None:
    if not namespaces:
        return
    if getattr(handler, "_forma_log_namespaces", None) == namespaces:
        return
    handler.addFilter(_LoggerNamespaceFilter(namespaces))
    handler._forma_log_namespaces = namespaces  # type: ignore[attr-defined]


def _handler_for_path(logger: logging.Logger, path: Path) -> Optional[logging.Handler]:
    target = str(path)
    for handler in logger.handlers:
        if getattr(handler, "_forma_log_file", None) == target:
            return handler
    return None


def _build_file_handler(
    path: Path,
    level: int,
    formatter: logging.Formatter,
    namespaces: Tuple[str, ...],
) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(formatter)
    _ensure_namespace_filter(handler, namespaces)
    handler._forma_log_file = str(path)  # type: ignore[attr-defined]
    return handler


def _attach_file_handlers(
    log_path: Path,
    level: int,
    formatter: logging.Formatter,
    namespaces: Tuple[str, ...],
) -> None:
    # Uvicorn child loggers do not consistently propagate to root under uvicorn's
    # own logging config. Attach directly to the child loggers, but not to the
    # parent "uvicorn" logger; otherwise startup lines can be duplicated.
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_error_logger.propagate = False
    uvicorn_access_logger.propagate = False
    target_loggers = [
        logging.getLogger(),
        uvicorn_error_logger,
        uvicorn_access_logger,
    ]
    handler = next(
        (
            existing
            for logger in target_loggers
            if (existing := _handler_for_path(logger, log_path)) is not None
        ),
        None,
    )
    if handler is None:
        handler = _build_file_handler(log_path, level, formatter, namespaces)

    for logger in target_loggers:
        logger.setLevel(level)
        if _handler_for_path(logger, log_path) is None:
            logger.addHandler(handler)


def _configure_file_logging(
    log_path: Path,
    level: int,
    formatter: logging.Formatter,
    namespaces: Tuple[str, ...],
) -> Optional[Path]:
    try:
        _attach_file_handlers(log_path, level, formatter, namespaces)
        return log_path
    except OSError as exc:
        logger = logging.getLogger(__name__)
        fallback_path = _tmp_log_fallback_path(log_path)
        if fallback_path is not None:
            logger.warning(
                "Backend log file %s is not writable (%s); falling back to %s.",
                log_path,
                exc,
                fallback_path,
            )
            config.set("BACKEND_LOG_FILE", str(fallback_path))
            try:
                _attach_file_handlers(fallback_path, level, formatter, namespaces)
                return fallback_path
            except OSError as fallback_exc:
                logger.warning(
                    "Backend log file fallback %s is not writable (%s); file logging disabled.",
                    fallback_path,
                    fallback_exc,
                )
                return None

        reason = "read-only filesystem" if exc.errno == errno.EROFS else str(exc)
        logger.warning("Backend log file %s is not writable (%s); file logging disabled.", log_path, reason)
        return None


def _ensure_console_handler(
    root_logger: logging.Logger,
    level: int,
    formatter: logging.Formatter,
    namespaces: Tuple[str, ...],
) -> None:
    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.setLevel(min(handler.level or level, level))
            _ensure_namespace_filter(handler, namespaces)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    _ensure_namespace_filter(handler, namespaces)
    root_logger.addHandler(handler)


def configure_backend_logging() -> None:
    """Configure Forma backend logging for console and optional file output."""
    level = _log_level(config.get("LOG_LEVEL") or ("DEBUG" if debug_mode_enabled() else None))
    formatter = logging.Formatter(config.get("BACKEND_LOG_FORMAT", DEFAULT_LOG_FORMAT))
    namespaces = _log_namespaces()
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    _ensure_console_handler(root_logger, level, formatter, namespaces)

    log_path = resolve_backend_log_path()
    if log_path is None:
        return

    # Vercel's deployment directory is read-only. Select its writable temporary
    # directory before opening the configured file instead of warning on every
    # cold start. Keep the existing opt-out and non-Vercel fallback behavior.
    if config.get("VERCEL"):
        fallback_path = _tmp_log_fallback_path(log_path)
        if fallback_path is not None:
            log_path = fallback_path
            config.set("BACKEND_LOG_FILE", str(log_path))

    configured_log_path = _configure_file_logging(log_path, level, formatter, namespaces)
    if configured_log_path is None:
        return

    logging.getLogger(__name__).info(
        "Backend logging configured; file=%s level=%s namespaces=%s",
        configured_log_path,
        logging.getLevelName(level),
        ",".join(namespaces) if namespaces else "*",
    )
