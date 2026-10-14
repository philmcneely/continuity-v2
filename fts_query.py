"""Make free-text queries safe for SQLite FTS5 MATCH.

FTS5 treats `-`, `.`, `:` etc. as syntax, so fleet names like `mellum-vii1`,
`qwen3.8-27b` or `continuity-v2` raised "no such column: v2" and the search
returned nothing. fts_safe() double-quotes any bare token that is not a plain
word, keeping FTS5 operators (AND/OR/NOT), prefix* tokens, quoted phrases and
parentheses working. fts_fallback() is the always-valid form (every token quoted)
for when the operator syntax the user typed is itself malformed.

Linear-time only: no regex with nested/adjacent unbounded quantifiers on
attacker-controlled tokens (an earlier `\\)*$` search was O(n^2) - security review).
"""
import re

MAX_QUERY_CHARS = 2000
_OPERATORS = {"AND", "OR", "NOT"}
_PLAIN = re.compile(r"[A-Za-z0-9_]+\*?")
_TOKEN = re.compile(r'"[^"]*"|\S+')


def _clean(query: str) -> str:
    return (query or "").replace("\x00", " ")[:MAX_QUERY_CHARS]


def _quote(tok: str) -> str:
    return '"' + tok.replace('"', "") + '"'


def fts_safe(query: str) -> str:
    out = []
    for tok in _TOKEN.findall(_clean(query)):
        if len(tok) >= 2 and tok.startswith('"') and tok.endswith('"'):
            out.append(tok)  # already a phrase
            continue
        no_lead = tok.lstrip("(")
        core = no_lead.rstrip(")")
        lead = "(" * (len(tok) - len(no_lead))
        trail = ")" * (len(no_lead) - len(core))
        if not core:
            out.append(tok)
        elif core in _OPERATORS or _PLAIN.fullmatch(core):
            out.append(lead + core + trail)
        else:
            out.append(lead + _quote(core) + trail)
    return " ".join(out)


def fts_fallback(query: str) -> str:
    """Every token as a quoted literal: always valid FTS5 (or '' if nothing searchable)."""
    toks = [t.replace('"', "").strip("()") for t in _TOKEN.findall(_clean(query))]
    return " ".join('"' + t + '"' for t in toks if t)
