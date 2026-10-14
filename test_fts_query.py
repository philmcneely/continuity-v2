import sqlite3

import pytest

from fts_query import fts_fallback, fts_safe


@pytest.mark.parametrize("raw,expected", [
    ("mellum-vii1", '"mellum-vii1"'),
    ("continuity-v2 MCP", '"continuity-v2" MCP'),
    ("qwen3.8-27b on apol", '"qwen3.8-27b" on apol'),
    ('"gold-402" distribution', '"gold-402" distribution'),
    ("wopr AND card2", "wopr AND card2"),
    ("moe35* OR swift", "moe35* OR swift"),
    ("(mellum-vii1 OR maya) NOT strata", '("mellum-vii1" OR maya) NOT strata'),
    ("host:wopr", '"host:wopr"'),
    ("", ""),
])
def test_fts_safe(raw, expected):
    assert fts_safe(raw) == expected


@pytest.mark.parametrize("raw", ["mellum-vii1", "continuity-v2 MCP", "qwen3.8-27b", "host:wopr",
                                 "(a-b OR c) NOT d.e", 'say "hi', "x' OR 1=1 --"])
def test_fts_safe_never_raises_in_real_fts5(raw):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(text)")
    con.execute("INSERT INTO t VALUES ('mellum-vii1 runs on apol card1 qwen3.8-27b host:wopr a-b d.e')")
    con.execute("SELECT * FROM t WHERE t MATCH ?", [fts_safe(raw)]).fetchall()


def test_no_quadratic_blowup_on_adversarial_tokens():
    import time
    for tok in (")" * 50_000 + "x", "(" * 50_000 + "x", '"' * 50_000, "a-" * 50_000):
        t0 = time.perf_counter()
        fts_safe(tok)
        fts_fallback(tok)
        assert time.perf_counter() - t0 < 0.5, tok[:10]


def test_query_is_capped_and_nul_stripped():
    assert len(fts_safe("a" * 100_000)) <= 2000
    assert "\x00" not in fts_safe("mellum\x00-vii1")


@pytest.mark.parametrize("raw", ["", "   ", "AND", "a OR", "a AND NOT b", "(a", "a)", '"a b")',
                                 "(text) : foo", "x\x00y", "(" * 1000 + "a", "NEAR(a b)"])
def test_fallback_is_always_valid_fts5(raw):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(text)")
    q = fts_fallback(raw)
    if q:
        con.execute("SELECT * FROM t WHERE t MATCH ?", [q]).fetchall()


def test_hyphenated_name_is_found():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(text)")
    con.execute("INSERT INTO t VALUES ('started mellum-vii1 on card1')")
    assert con.execute("SELECT count(*) FROM t WHERE t MATCH ?", [fts_safe("mellum-vii1")]).fetchone()[0] == 1
