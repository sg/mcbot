#!/usr/bin/env python3
"""Web Database console: raw single-statement SQL execution + saved queries.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_db_console.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402
from management import MgmtError  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def make_bot():
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    log = mcbot.logging.getLogger("test-dbconsole")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


async def audit_count(bot, action):
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM bot_audit_log WHERE action=?", (action,)
    )
    return row["n"]


async def test_select_returns_columns_and_rows():
    print("test_select_returns_columns_and_rows")
    bot = make_bot()
    r = await bot.db.run_raw("SELECT 1 AS one, 'x' AS two")
    check(r["columns"] == ["one", "two"], f"column names (got {r['columns']})")
    check(r["rows"] == [[1, "x"]], f"row values (got {r['rows']})")
    check(r["rowcount"] == 1, "rowcount = number of rows")
    bot.db.close()


async def test_write_returns_rowcount():
    print("test_write_returns_rowcount")
    bot = make_bot()
    ddl = await bot.db.run_raw("CREATE TABLE tmp(a INTEGER)")
    check(ddl["columns"] == [] and ddl["rowcount"] == -1, "DDL -> no columns, rowcount -1")
    ins = await bot.db.run_raw("INSERT INTO tmp(a) VALUES (1)")
    check(ins["rowcount"] == 1, "insert -> 1 affected")
    upd = await bot.db.run_raw("UPDATE tmp SET a = 2")
    check(upd["rowcount"] == 1, "update -> 1 affected")
    bot.db.close()


async def test_blob_hex_encoded():
    print("test_blob_hex_encoded")
    bot = make_bot()
    await bot.db.run_raw("CREATE TABLE b(x BLOB)")
    await bot.db.run_raw("INSERT INTO b(x) VALUES (x'01ff')")
    r = await bot.db.run_raw("SELECT x FROM b")
    check(r["rows"] == [["01ff"]], f"BLOB hex-encoded for JSON (got {r['rows']})")
    bot.db.close()


async def test_run_sql_audits_and_errors():
    print("test_run_sql_audits_and_errors")
    bot = make_bot()
    await bot.mgmt.run_sql("SELECT 1")
    check(await audit_count(bot, "db.sql") == 1, "successful query is audited")
    for bad, label in (("", "empty"), ("SELECT * FROM nope", "bad table"),
                       ("SELECT 1; SELECT 2", "two statements")):
        try:
            await bot.mgmt.run_sql(bad)
            check(False, f"{label} should raise")
        except MgmtError:
            check(True, f"{label} rejected")
    bot.db.close()


async def test_saved_query_crud():
    print("test_saved_query_crud")
    bot = make_bot()
    await bot.mgmt.saved_query_save("recent", "SELECT * FROM contacts LIMIT 5")
    lst = await bot.mgmt.saved_query_list()
    check(len(lst) == 1 and lst[0]["name"] == "recent", "saved query listed")
    qid = lst[0]["id"]
    # overwrite by name -> still one row, updated text
    await bot.mgmt.saved_query_save("recent", "SELECT 42")
    lst = await bot.mgmt.saved_query_list()
    check(len(lst) == 1 and lst[0]["query"] == "SELECT 42", "save overwrites by name")
    check(await audit_count(bot, "db.query.save") == 2, "each save audited")
    # empty name/query rejected
    for args in (("", "SELECT 1"), ("n", "")):
        try:
            await bot.mgmt.saved_query_save(*args)
            check(False, "empty field should raise")
        except MgmtError:
            check(True, "empty field rejected")
    # delete
    await bot.mgmt.saved_query_delete(qid)
    check(await bot.mgmt.saved_query_list() == [], "delete removes the row")
    check(await audit_count(bot, "db.query.delete") == 1, "delete audited")
    try:
        await bot.mgmt.saved_query_delete(qid)
        check(False, "deleting missing should raise")
    except MgmtError:
        check(True, "deleting missing rejected")
    bot.db.close()


async def main():
    for t in (
        test_select_returns_columns_and_rows,
        test_write_returns_rowcount,
        test_blob_hex_encoded,
        test_run_sql_audits_and_errors,
        test_saved_query_crud,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
