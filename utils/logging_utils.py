import io
import logging
import os
import sys
import time
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any


class SafeStream:
    def __init__(self, stream):
        self.stream = stream

    def write(self, s):
        try:
            self.stream.write(s)
        except UnicodeEncodeError:
            self.stream.write(s.encode(self.stream.encoding or 'utf-8', errors='replace').decode(self.stream.encoding or 'utf-8'))

    def flush(self):
        self.stream.flush()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)


class CustomFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',  # Cyan
        'INFO': '\033[32m',  # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',  # Red
        'CRITICAL': '\033[41m',  # Red background
        'RESET': '\033[0m'
    }

    def __init__(self, fmt: str, datefmt: str, use_colors: bool = True, compact: bool = False):
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors and sys.stdout.isatty()
        self.compact = compact

    def format(self, record):
        original_msg = record.msg
        original_levelname = record.levelname

        if self.use_colors:
            color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
            record.levelname = f"{color}{record.levelname}{self.COLORS['RESET']}"

        if isinstance(record.msg, str) and ('=' * 10) in record.msg:
            if record.msg.strip() == ('=' * 50) or record.msg.strip() == ('=' * 60):
                record.msg = '-' * 30

        if self.compact and record.name.endswith('.performance') and 'Slow operation' in str(record.msg):
            parts = str(record.msg).split()
            if len(parts) > 6:
                func_name = parts[2].strip(':')
                duration = parts[4].strip('s')
                args = ' '.join(parts[6:])
                if len(args) > 30:
                    args = args[:27] + "..."
                record.msg = f"Slow: {func_name} ({duration}s) {args}"

        result = super().format(record)

        record.msg = original_msg
        record.levelname = original_levelname

        return result


class ContextFilter(logging.Filter):

    def __init__(self, context: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.context = context or {}

    def filter(self, record):
        for key, value in self.context.items():
            setattr(record, key, value)
        return True


class SmartDuplicateFilter(logging.Filter):

    def __init__(self, time_window=5.0, max_count=3, summary_interval=10.0):
        super().__init__()
        self.time_window = time_window
        self.max_count = max_count
        self.summary_interval = summary_interval
        self.last_log = {}
        self.message_counts = defaultdict(int)
        self.last_summary_time = time.time()
        self.performance_stats = defaultdict(list)

    def filter(self, record):
        current_time = record.created
        msg = record.getMessage()

        if record.name.endswith('.performance') or 'Slow' in msg:
            if 'Slow operation:' in msg or 'Slow:' in msg:
                parts = msg.split()
                if len(parts) >= 5:
                    try:
                        op_name = parts[2].rstrip(':')
                        duration = float(parts[4].rstrip('s'))

                        self.performance_stats[op_name].append(duration)

                        self.message_counts[op_name] += 1
                        if self.message_counts[op_name] > self.max_count:
                            return False
                    except (ValueError, IndexError):
                        pass

            if msg in self.last_log:
                if current_time - self.last_log[msg] < self.time_window:
                    return False

            self.last_log[msg] = current_time

            if current_time - self.last_summary_time > self.summary_interval:
                self._output_summary()
                self.last_summary_time = current_time
                self.message_counts.clear()

        self.last_log = {k: v for k, v in self.last_log.items()
                         if current_time - v < 60.0}

        return True

    def _output_summary(self):
        if not self.performance_stats:
            return

        logger = logging.getLogger("performance.summary")

        stats_summary = []
        for op_name, durations in self.performance_stats.items():
            if not durations:
                continue

            count = len(durations)
            avg = sum(durations) / count
            max_val = max(durations)

            stats_summary.append(f"{op_name}: {count} calls, avg {avg:.2f}s, max {max_val:.2f}s")

        if stats_summary:
            logger.info("Performance summary:\n  " + "\n  ".join(stats_summary))

        self.performance_stats.clear()


def setup_logging(
        log_file: Optional[str] = None,
        level: int = logging.INFO,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        use_colors: bool = True,
        log_dir: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        compact_output: bool = True
) -> logging.Logger:
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    if log_file and log_dir:
        log_file = os.path.join(log_dir, log_file)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    if context:
        context_format = " | ".join(f"{k}=%(${k})s" for k in context.keys())
        log_format = f"{log_format} | {context_format}"
    date_format = "%Y-%m-%d %H:%M:%S"

    console_handler = logging.StreamHandler(SafeStream(sys.stdout))
    console_formatter = CustomFormatter(log_format, date_format, use_colors, compact_output)
    console_handler.setFormatter(console_formatter)

    duplicate_filter = SmartDuplicateFilter()
    console_handler.addFilter(duplicate_filter)

    console_handler.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    if log_file:
        try:
            file_handler = RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count
            )
            file_formatter = logging.Formatter(log_format, date_format)
            file_handler.setFormatter(file_formatter)

            file_handler.setLevel(logging.DEBUG)
            root_logger.addHandler(file_handler)
        except (IOError, PermissionError) as e:
            logging.error(f"Failed to create log file {log_file}: {e}")

    if context:
        context_filter = ContextFilter(context)
        for handler in root_logger.handlers:
            handler.addFilter(context_filter)

    summary_logger = logging.getLogger("performance.summary")
    summary_logger.setLevel(logging.INFO)
    summary_logger.propagate = False

    summary_handler = logging.StreamHandler(SafeStream(sys.stdout))
    summary_formatter = CustomFormatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        date_format, use_colors, compact_output
    )
    summary_handler.setFormatter(summary_formatter)
    summary_logger.addHandler(summary_handler)

    for logger_name in logging.root.manager.loggerDict:
        if logger_name.endswith('.performance'):
            perf_logger = logging.getLogger(logger_name)
            perf_logger.setLevel(logging.DEBUG)
            perf_logger.propagate = False

            # Add a dedicated handler
            perf_handler = logging.StreamHandler()
            perf_handler.setFormatter(console_formatter)
            perf_handler.addFilter(duplicate_filter)
            perf_handler.setLevel(logging.INFO)
            perf_logger.addHandler(perf_handler)

    for lib_logger in ["discord", "urllib3", "requests", "asyncio"]:
        logging.getLogger(lib_logger).setLevel(logging.WARNING)

    logging.info("Logging system configured successfully")
    return root_logger


def setup_performance_logger(name, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)s | %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)

    if name.endswith('.performance'):
        return setup_performance_logger(name, level or logging.INFO)

    return logger