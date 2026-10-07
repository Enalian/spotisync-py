import contextvars
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.core.config import settings

current_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_ctx", default="MAIN"
)

# Дополнительные уровни логирования
TRACE_LEVEL = 5
SUCCESS_LEVEL = 25
logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class CustomLogger(logging.Logger):
    def trace(self, msg: str, *args, **kwargs) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, msg, args, **kwargs)

    def success(self, msg: str, *args, **kwargs) -> None:
        if self.isEnabledFor(SUCCESS_LEVEL):
            self._log(SUCCESS_LEVEL, msg, args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("SpotiSync")
logger.setLevel(TRACE_LEVEL)
logger.propagate = False

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class MaskingFormatter(logging.Formatter):
    """Форматтер для перехвата и скрытия секретных ключей в логах"""

    def __init__(self, use_colors: bool = False):
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")
        self.use_colors = use_colors

        self.secrets_to_mask = []
        if settings.azuracast_api_key and len(settings.azuracast_api_key) > 5:
            self.secrets_to_mask.append(settings.azuracast_api_key)

    def _mask_message(self, message: str) -> str:
        for secret in self.secrets_to_mask:
            message = message.replace(secret, "[MASKED_KEY]")
        message = re.sub(
            r"(Bearer\s+)[A-Za-z0-9\-\._~+/]+", r"\1[MASKED_TOKEN]", message
        )
        return message

    def format(self, record: logging.LogRecord) -> str:
        ctx = current_ctx.get()
        asctime = self.formatTime(record, self.datefmt)
        levelname = f"{record.levelname:<7}"
        raw_msg = self._mask_message(record.getMessage())

        if self.use_colors:
            colors = {
                TRACE_LEVEL: "\033[90m",
                logging.DEBUG: "\033[36m",
                logging.INFO: "\033[37m",
                SUCCESS_LEVEL: "\033[1;32m",
                logging.WARNING: "\033[1;33m",
                logging.ERROR: "\033[1;31m",
            }
            color = colors.get(record.levelno, "")
            reset = "\033[0m"
            ctx_color = "\033[35m"
            return f"\033[90m{asctime}{reset} | {color}{levelname}{reset} | {ctx_color}[{ctx}]{reset} {color}{raw_msg}{reset}"

        return f"{asctime} | {levelname} | [{ctx}] {ANSI_ESCAPE_RE.sub('', raw_msg)}"


def setup_logger(clear_logs: bool = False) -> None:
    """Инициализация логгера"""
    level_map = {
        "TRACE": TRACE_LEVEL,
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "SUCCESS": SUCCESS_LEVEL,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
    }

    target_level = level_map.get(settings.log_level.upper(), logging.INFO)

    console_level = logging.WARNING if settings.log_quiet else target_level
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(MaskingFormatter(use_colors=settings.log_colors))
    logger.addHandler(console_handler)

    if settings.enable_logs and settings.log_file:
        try:
            log_path = Path(settings.log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            if clear_logs:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("=== LOG CLEARED ===\n")
                error_log_path = log_path.with_name(
                    f"{log_path.stem}_error{log_path.suffix}"
                )
                with open(error_log_path, "w", encoding="utf-8") as f:
                    f.write("=== ERROR LOG CLEARED ===\n")

            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=settings.log_max_size_mb * 1024 * 1024,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(target_level)
            file_handler.setFormatter(MaskingFormatter(use_colors=False))
            logger.addHandler(file_handler)

            error_log_path = log_path.with_name(
                f"{log_path.stem}_error{log_path.suffix}"
            )
            error_handler = RotatingFileHandler(
                error_log_path,
                maxBytes=settings.log_max_size_mb * 1024 * 1024,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
            error_handler.setLevel(logging.WARNING)
            error_handler.setFormatter(MaskingFormatter(use_colors=False))
            logger.addHandler(error_handler)

            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n\n")

        except OSError as e:
            logger.error(f"Не удалось инициализировать файлы логов: {e}")
