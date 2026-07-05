"""setup_logging must apply [logging] log_level to BOTH the mcbot and meshcore
loggers, with --debug as a shortcut for DEBUG."""

import io
import logging
import sys

import mcbot


def levels(*, log_level="INFO", debug=False):
    cfg = mcbot.Config()
    cfg.log_level = log_level
    cfg.debug = debug
    mcbot.setup_logging(cfg)
    return (
        logging.getLevelName(logging.getLogger("mcbot").level),
        logging.getLevelName(logging.getLogger("meshcore").level),
    )


def test_log_level_applies_to_both_loggers():
    # log_level=DEBUG must raise BOTH loggers to DEBUG (the bug: meshcore
    # stayed at INFO so config DEBUG looked like a no-op).
    assert levels(log_level="DEBUG") == ("DEBUG", "DEBUG")
    assert levels(log_level="INFO") == ("INFO", "INFO")
    # --debug forces DEBUG regardless of log_level
    assert levels(log_level="INFO", debug=True) == ("DEBUG", "DEBUG")
    # a quieter level applies to both as well
    assert levels(log_level="WARNING") == ("WARNING", "WARNING")


def test_banner_shows_level_and_config_path(tmp_path):
    # load_config records the config file path (for the startup banner), and
    # the banner is emitted at INFO with the effective level + source.
    p = tmp_path / "test.conf"
    p.write_text("[radio]\nhost = 1.2.3.4\n\n[logging]\nlog_level = DEBUG\n")
    cfg = mcbot.load_config(mcbot.parse_args(["--config", str(p)]))
    assert cfg.log_level == "DEBUG", "load_config parses log_level=DEBUG"
    assert cfg.config_path == str(p), "load_config records the loaded config_path"

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
    assert "logging at DEBUG" in banner and str(p) in banner, \
        f"banner shows effective level + config path: {banner!r}"


def test_effective_log_level():
    # --debug forces DEBUG, else log_level
    c = mcbot.Config(); c.log_level = "DEBUG"; c.debug = False
    assert mcbot.effective_log_level(c) == "DEBUG"
    c2 = mcbot.Config(); c2.log_level = "INFO"; c2.debug = True
    assert mcbot.effective_log_level(c2) == "DEBUG"
    c3 = mcbot.Config(); c3.log_level = "WARNING"; c3.debug = False
    assert mcbot.effective_log_level(c3) == "WARNING"


def test_meshcore_override_reasserted():
    # The real bug: MeshCore.create_*() re-sets the "meshcore" logger from its
    # debug arg AFTER setup_logging (debug=False -> INFO), so log_level=DEBUG
    # was lost. run() now re-asserts effective_log_level after connecting.
    c = mcbot.Config(); c.log_level = "DEBUG"; c.debug = False
    mcbot.setup_logging(c)                                   # sets meshcore DEBUG
    logging.getLogger("meshcore").setLevel(logging.INFO)     # lib clobbers to INFO
    logging.getLogger("meshcore").setLevel(mcbot.effective_log_level(c))  # re-assert
    assert logging.getLevelName(logging.getLogger("meshcore").level) == "DEBUG", \
        "meshcore library override is re-asserted back to DEBUG"
