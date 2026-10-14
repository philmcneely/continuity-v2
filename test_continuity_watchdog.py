"""Watchdog must catch each 2026-10-14 failure mode: broken MCP interpreter, FTS5 hyphen errors,
missing edges table. Uses a temp HOME/DB so nothing live is touched."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import continuity_watchdog as wd


@pytest.fixture
def tmp_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude.json"
    monkeypatch.setattr(wd, "CLAUDE_JSON", cfg)
    return cfg


def write_cfg(cfg, command):
    cfg.write_text(json.dumps({"mcpServers": {"continuity-v2": {
        "command": command, "args": [str(wd.REPO / "mcp_server.py")], "env": {}}}}))


def test_mcp_config_flags_interpreter_without_deps(tmp_cfg):
    write_cfg(tmp_cfg, "/usr/bin/python3")  # system 3.9: no sqlite_vec
    ok, detail = wd.check_mcp_config()
    assert not ok and "cannot import" in detail


def test_mcp_config_heal_repoints_to_venv_and_backs_up(tmp_cfg):
    write_cfg(tmp_cfg, "/usr/bin/python3")
    wd.heal_mcp_config()
    assert json.loads(tmp_cfg.read_text())["mcpServers"]["continuity-v2"]["command"] == str(wd.VENV_PY)
    assert list(tmp_cfg.parent.glob(".claude.json.bak-watchdog-*"))
    assert wd.check_mcp_config()[0]


def test_mcp_search_passes_with_venv_and_hyphen_query(tmp_cfg):
    write_cfg(tmp_cfg, str(wd.VENV_PY))
    ok, detail = wd.check_mcp_search()
    assert ok, detail


def test_mcp_search_fails_with_broken_interpreter(tmp_cfg):
    write_cfg(tmp_cfg, "/usr/bin/python3")
    ok, _ = wd.check_mcp_search()
    assert not ok


def test_edges_check_detects_missing_table(tmp_path, monkeypatch):
    db = tmp_path / "c.db"
    sqlite3.connect(db).execute("CREATE TABLE turns (ts TEXT)")
    monkeypatch.setattr(wd, "DB", db)
    ok, detail = wd.check_edges()
    assert not ok and "missing" in detail


def test_dedup_alerts_once_then_recovers(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(wd, "send", sent.append)
    monkeypatch.setattr(wd, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(wd, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(wd, "LOG", tmp_path / "wd.log")
    flag = {"ok": False}
    monkeypatch.setattr(wd, "CHECKS", [("fake", lambda: (flag["ok"], "x"), None)])
    wd.main(); wd.main()
    assert len(sent) == 1 and "FAILED" in sent[0]
    flag["ok"] = True
    wd.main(); wd.main()
    assert len(sent) == 2 and "recovered" in sent[1]
