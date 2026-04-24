import sys
from datetime import datetime

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"

_log_file = None


def set_log_file(path):
    global _log_file
    _log_file = open(path, "a")
    _log_file.write(f"\n{'='*60}\n")
    _log_file.write(f"Session started: {datetime.now().isoformat()}\n")
    _log_file.write(f"{'='*60}\n")
    _log_file.flush()


def close_log_file():
    global _log_file
    if _log_file:
        _log_file.write(f"Session ended: {datetime.now().isoformat()}\n\n")
        _log_file.close()
        _log_file = None


def _write_log(level: str, msg: str):
    if _log_file:
        ts = datetime.now().strftime("%H:%M:%S")
        _log_file.write(f"[{ts}] {level}: {msg}\n")
        _log_file.flush()


def log_info(msg: str):
    print(f"{BLUE}[*]{RESET} {msg}", flush=True)
    _write_log("INFO", msg)


def log_ok(msg: str):
    print(f"{GREEN}[+]{RESET} {msg}", flush=True)
    _write_log("OK", msg)


def log_warn(msg: str):
    print(f"{YELLOW}[!]{RESET} {msg}", flush=True)
    _write_log("WARN", msg)


def log_err(msg: str):
    print(f"{RED}[-]{RESET} {msg}", file=sys.stderr, flush=True)
    _write_log("ERR", msg)


def log_debug(msg: str):
    """Only written to log file, never to console."""
    _write_log("DEBUG", msg)
