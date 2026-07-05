"""Web Database console: raw single-statement SQL execution + saved queries."""

import pytest

from management import MgmtError


async def audit_count(bot, action):
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM bot_audit_log WHERE action=?", (action,)
    )
    return row["n"]


async def test_select_returns_columns_and_rows(bot_factory):
    bot = bot_factory()
    r = await bot.db.run_raw("SELECT 1 AS one, 'x' AS two")
    assert r["columns"] == ["one", "two"]
    assert r["rows"] == [[1, "x"]]
    assert r["rowcount"] == 1, "rowcount = number of rows"


async def test_write_returns_rowcount(bot_factory):
    bot = bot_factory()
    ddl = await bot.db.run_raw("CREATE TABLE tmp(a INTEGER)")
    assert ddl["columns"] == [] and ddl["rowcount"] == -1, "DDL -> no columns, rowcount -1"
    ins = await bot.db.run_raw("INSERT INTO tmp(a) VALUES (1)")
    assert ins["rowcount"] == 1, "insert -> 1 affected"
    upd = await bot.db.run_raw("UPDATE tmp SET a = 2")
    assert upd["rowcount"] == 1, "update -> 1 affected"


async def test_blob_hex_encoded(bot_factory):
    bot = bot_factory()
    await bot.db.run_raw("CREATE TABLE b(x BLOB)")
    await bot.db.run_raw("INSERT INTO b(x) VALUES (x'01ff')")
    r = await bot.db.run_raw("SELECT x FROM b")
    assert r["rows"] == [["01ff"]], "BLOB hex-encoded for JSON"


async def test_run_sql_audits_and_errors(bot_factory):
    bot = bot_factory()
    await bot.mgmt.run_sql("SELECT 1")
    assert await audit_count(bot, "db.sql") == 1, "successful query is audited"
    for bad in ("", "SELECT * FROM nope", "SELECT 1; SELECT 2"):
        with pytest.raises(MgmtError):
            await bot.mgmt.run_sql(bad)


async def test_saved_query_crud(bot_factory):
    bot = bot_factory()
    await bot.mgmt.saved_query_save("recent", "SELECT * FROM contacts LIMIT 5")
    lst = await bot.mgmt.saved_query_list()
    assert len(lst) == 1 and lst[0]["name"] == "recent", "saved query listed"
    qid = lst[0]["id"]
    # overwrite by name -> still one row, updated text
    await bot.mgmt.saved_query_save("recent", "SELECT 42")
    lst = await bot.mgmt.saved_query_list()
    assert len(lst) == 1 and lst[0]["query"] == "SELECT 42", "save overwrites by name"
    assert await audit_count(bot, "db.query.save") == 2, "each save audited"
    # empty name/query rejected
    for args in (("", "SELECT 1"), ("n", "")):
        with pytest.raises(MgmtError):
            await bot.mgmt.saved_query_save(*args)
    # delete
    await bot.mgmt.saved_query_delete(qid)
    assert await bot.mgmt.saved_query_list() == [], "delete removes the row"
    assert await audit_count(bot, "db.query.delete") == 1, "delete audited"
    with pytest.raises(MgmtError):
        await bot.mgmt.saved_query_delete(qid)
