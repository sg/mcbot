#!/usr/bin/env python3
"""setup_logging must apply [logging] log_level to BOTH the mcbot and meshcore
loggers, with --debug as a shortcut for DEBUG.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_logging.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def levels(*, log_level="INFO", debug=False):
    cfg = mcbot.Config()
    cfg.log_level = log_level
    cfg.debug = debug
    mcbot.setup_logging(cfg)
    return (
        logging.getLevelName(logging.getLogger("mcbot").level),
        logging.getLevelName(logging.getLogger("meshcore").level),
    )


def main():
    # log_level=DEBUG must raise BOTH loggers to DEBUG (the bug: meshcore
    # stayed at INFO so config DEBUG looked like a no-op).
    check(levels(log_level="DEBUG") == ("DEBUG", "DEBUG"),
          "log_level=DEBUG -> both loggers DEBUG")
    # default
    check(levels(log_level="INFO") == ("INFO", "INFO"),
          "log_level=INFO -> both loggers INFO")
    # --debug forces DEBUG regardless of log_level
    check(levels(log_level="INFO", debug=True) == ("DEBUG", "DEBUG"),
          "--debug -> both loggers DEBUG even when log_level=INFO")
    # a quieter level applies to both as well
    check(levels(log_level="WARNING") == ("WARNING", "WARNING"),
          "log_level=WARNING -> both loggers WARNING")
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
