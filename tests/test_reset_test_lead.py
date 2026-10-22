"""scripts/reset_test_lead.py: только тестовые номера, сухой прогон ничего не пишет,
бэкап до записи, условие ровно по одному лиду."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import reset_test_lead as r

TEST = frozenset({"wa_79635708880", "wa_5215500008289"})


def conn_mock(leads=1, msgs=5, calls=None):
    calls = calls if calls is not None else []
    c = MagicMock()

    async def fetchval(sql, *a):
        return leads if "FROM leads" in sql else msgs
    c.fetchval = AsyncMock(side_effect=fetchval)
    c.fetchrow = AsyncMock(return_value={"phone": "wa_79635708880", "name": "Luis"})
    c.fetch = AsyncMock(return_value=[])

    async def execute(sql, *a):
        calls.append(("execute", sql.split()[0]))
        return "DELETE 5" if sql.startswith("DELETE") else "UPDATE 1"
    c.execute = AsyncMock(side_effect=execute)

    class Tx:
        async def __aenter__(self):
            calls.append(("tx",))

        async def __aexit__(self, *a):
            return False
    c.transaction = lambda: Tx()
    return c, calls


@pytest.mark.parametrize("phone", ["wa_5215614513426", "wa_5215511115225", "wa_5215512345678"])
async def test_refuses_non_test_number(phone):
    c, calls = conn_mock()
    with pytest.raises(r.NotATestNumber):
        await r.reset(c, phone, TEST, commit=True)
    c.execute.assert_not_awaited()


async def test_dry_run_writes_nothing(tmp_path):
    c, calls = conn_mock()
    s = await r.reset(c, "wa_79635708880", TEST, commit=False, dirs=[tmp_path])
    assert s["messages"] == 5 and s["commit"] is False
    c.execute.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


async def test_backup_before_write(tmp_path, monkeypatch):
    calls = []
    c, calls = conn_mock(calls=calls)
    written = []
    orig = r.Path.write_text

    def spy(self, text, encoding=None):
        calls.append(("backup", self.name))
        return orig(self, text, encoding=encoding)
    monkeypatch.setattr(r.Path, "write_text", spy)
    s = await r.reset(c, "wa_79635708880", TEST, commit=True, dirs=[tmp_path / "a", tmp_path / "b"])
    kinds = [x[0] for x in calls]
    assert kinds.index("backup") < kinds.index("tx") < kinds.index("execute")
    assert ("execute", "DELETE") in calls and ("execute", "UPDATE") in calls
    assert len(s["backup"]) == 2
    data = json.loads((tmp_path / "a" / s["backup"][0].split("/")[-1]).read_text("utf-8"))
    assert data["lead"]["name"] == "Luis" and "messages" in data


async def test_exactly_one_lead():
    c, _ = conn_mock(leads=0)
    with pytest.raises(RuntimeError):
        await r.reset(c, "wa_79635708880", TEST, commit=True)
    c.execute.assert_not_awaited()


def test_reset_sql_only_this_lead_and_keeps_journals():
    assert r.RESET_SQL.rstrip().endswith("WHERE phone=$1")
    assert "mode='auto'" in r.RESET_SQL and "funnel_stage='new'" in r.RESET_SQL
    assert r.DEFAULT_PHONE == "wa_79635708880"
