"""Make free-text queries safe for SQLite FTS5 MATCH.

FTS5 treats `-`, `.`, `:` etc. as syntax, so fleet names like `mellum-vii1`,
`qwen3.8-27b` or `continuity-v2` raised "no such column: v2" and the search
returned nothing. fts_safe() double-quotes any bare token that is not a plain
word, keeping FTS5 operators (AND/OR/NOT/NEAR), prefix* tokens, quoted phrases
and parentheses working.
"""
import re

_OPERATORS = {"AND", "OR", "NOT"}
_PLAIN = re.compile(r"[A-Za-z0-9_]+\*?")
_TOKEN = re.compile(r'"[^"]*"|\S+')


def _quote(tok: str) -> str:
    return '"' + tok.replace('"', "") + '"'


def fts_safe(query: str) -> str:
    out = []
    for tok in _TOKEN.findall(query or ""):
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            out.append(tok)  # already a phrase
            continue
        lead = re.match(r"^\(*", tok).group(0)
        trail = re.search(r"\)*$", tok).group(0)
        core = tok[len(lead):len(tok) - len(trail)] if trail else tok[len(lead):]
        if not core:
            out.append(tok)
        elif core in _OPERATORS or _PLAIN.fullmatch(core) or re.fullmatch(r"NEAR\(.*\)", core):
            out.append(lead + core + trail)
        else:
            out.append(lead + _quote(core) + trail)
    return " ".join(out)
