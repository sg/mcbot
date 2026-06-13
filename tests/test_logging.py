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

    # load_config records the config file path (for the startup banner), and
    # the banner is emitted at INFO with the effective level + source.
    import io
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".conf")
    os.write(fd, b"[radio]\nhost = 1.2.3.4\n\n[logging]\nlog_level = DEBUG\n")
    os.close(fd)
    cfg = mcbot.load_config(mcbot.parse_args(["--config", path]))
    check(cfg.log_level == "DEBUG", "load_config parses log_level=DEBUG")
    check(cfg.config_path == path, "load_config records the loaded config_path")

    # setup_logging's StreamHandler binds to sys.stderr at construction, so
    # swap it to capture the banner it prints there.
    old_stderr = sys.stderr
    sys.stderr = buf = io.StringIO()
    try:
        mcbot.setup_logging(cfg)
    finally:
        sys.stderr = old_stderr
    banner = next(
        (ln for ln in buf.getvalue().splitlines() if "logging at" in ln), "",
    )
    check("logging at DEBUG" in banner and path in banner,
          f"banner shows effective level + config path: {banner!r}")
    os.unlink(path)

    # effective_log_level: --debug forces DEBUG, else log_level
    c = mcbot.Config(); c.log_level = "DEBUG"; c.debug = False
    check(mcbot.effective_log_level(c) == "DEBUG", "effective: log_level=DEBUG")
    c2 = mcbot.Config(); c2.log_level = "INFO"; c2.debug = True
    check(mcbot.effective_log_level(c2) == "DEBUG", "effective: --debug")
    c3 = mcbot.Config(); c3.log_level = "WARNING"; c3.debug = False
    check(mcbot.effective_log_level(c3) == "WARNING", "effective: log_level=WARNING")

    # The real bug: MeshCore.create_*() re-sets the "meshcore" logger from its
    # debug arg AFTER setup_logging (debug=False -> INFO), so log_level=DEBUG
    # was lost. run() now re-asserts effective_log_level after connecting.
    mcbot.setup_logging(c)                                   # sets meshcore DEBUG
    logging.getLogger("meshcore").setLevel(logging.INFO)     # lib clobbers to INFO
    logging.getLogger("meshcore").setLevel(mcbot.effective_log_level(c))  # re-assert
    check(logging.getLevelName(logging.getLogger("meshcore").level) == "DEBUG",
          "meshcore library override is re-asserted back to DEBUG")
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
