"""load_config flags unrecognized config sections/keys (and deprecated ones),
while accepting dynamic [env]/[channels] keys and valid options silently."""

import mcbot


def warnings_for(tmp_path, text):
    p = tmp_path / "test.conf"
    p.write_text(text)
    cfg = mcbot.load_config(mcbot.parse_args(["--config", str(p)]))
    return cfg.config_warnings


def test_valid_config_no_warnings(tmp_path):
    # a fully valid config -> no warnings (no false positives)
    valid = (
        "[radio]\nhost = 1.2.3.4\nport = 4000\n\n"
        "[bot]\nenabled = true\nrepeat_tracking = true\nadvert_interval_hours = 3\n\n"
        "[env]\nPWS_API_KEY = abc\n\n"
        "[channels]\n0 = #bot\n"
    )
    w = warnings_for(tmp_path, valid)
    assert w == [], f"valid config -> no warnings (got {w})"


def test_flags_unknown_and_deprecated(tmp_path):
    # bad config: unknown key, unknown section, deprecated key; dynamic ok
    bad = (
        "[radio]\nhost = 1.2.3.4\n\n"
        "[bot]\nenabled = true\nbogus_key = 1\nrx_log_decrypt = false\n\n"
        "[bogus]\nfoo = bar\n\n"
        "[env]\nANY_NAME = x\n\n"
        "[channels]\n1 = #bot-cmd-test\n"
    )
    w = warnings_for(tmp_path, bad)
    assert any("unrecognized key 'bogus_key'" in x for x in w), \
        "warns on unrecognized [bot] key"
    assert any("unrecognized section [bogus]" in x for x in w), \
        "warns on unrecognized section"
    assert any("rx_log_decrypt" in x and "deprecated" in x for x in w), \
        "flags rx_log_decrypt as deprecated (not unrecognized)"
    assert not any("ANY_NAME" in x or "any_name" in x for x in w), \
        "does NOT warn on [env] keys"
    assert not any("bot-cmd-test" in x or "'1'" in x for x in w), \
        "does NOT warn on [channels] keys"
