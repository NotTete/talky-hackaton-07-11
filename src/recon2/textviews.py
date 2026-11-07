"""Per-line text views: folded join (with N43 wrap glue) and ref-like tokens."""

from __future__ import annotations

import re
import unicodedata

from recon2.model import Line

_DROP = re.compile(r"[^A-Z0-9 ./+\-]")
_WS = re.compile(r"\s+")
_SPLIT = re.compile(r"[\s/+:;,()\[\]|]+")
_NOSLASH_SPLIT = re.compile(r"[\s+:;,()\[\]|]+")
_GLUED = re.compile(r"[A-Z]{0,6}\d{4,}")
_PREFIXED = re.compile(r"^([A-Z]+)(\d+)$")
_LONGPRE = re.compile(r"^([A-Z]{5,})(\d{4,}[A-Z0-9]*)$")
WRAP = 38


def fold(s: str) -> str:
    """NFKD -> ASCII upper, keep [A-Z0-9 ./-+], collapse spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").upper()
    return _WS.sub(" ", _DROP.sub(" ", s)).strip()


def unwrap(text: str) -> str:
    """N43 joins its 38-char fields with ' | '; a full 38-char fragment is a wrap, glue it."""
    if " | " not in text:
        return text
    parts = text.split(" | ")
    out = parts[0]
    prev = parts[0]
    for p in parts[1:]:
        out += (p if len(prev) == WRAP else " " + p)
        prev = p
    return out


def _alnum(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum())


def _reflike(t: str) -> bool:
    return len(t) >= 5 and any(ch.isdigit() for ch in t)


def tokens_of(*fields: str) -> tuple[str, ...]:
    out: set[str] = set()
    for field in fields:
        if not field:
            continue
        words = [w for w in (_alnum(x) for x in _SPLIT.split(fold(field))) if w]
        # also hyphen/dot pieces ('RE-250110905' -> '250110905')
        pieces: list[str] = []
        for x in _SPLIT.split(fold(field)):
            if "-" in x or "." in x:
                pieces.extend(p for p in re.split(r"[-.]", x) if p)
        # slash document numbers ('F25/04150', 'AB-F25/03319', '2025/00036') must also exist
        # joined: '/' is a separator above (MT940 /ORDP/ fields), which cut 749/772 visible refs.
        for x in _NOSLASH_SPLIT.split(fold(field)):
            if "/" in x.strip("/"):
                for seg in (x.strip("/"),) + tuple(x.strip("/").split("/")[i] + x.strip("/").split("/")[i + 1]
                                                   for i in range(x.strip("/").count("/"))):
                    pieces.append(_alnum(seg))
        for w in words + pieces:
            if _reflike(w):
                out.add(w)
                for g in _GLUED.findall(w):
                    if _reflike(g):
                        out.add(g)
                m = _LONGPRE.match(w)
                if m:  # glued word + ref: 'RECHNUNGRE250110905' -> RE250110905 (prefix length unknown)
                    letters, rest = m.group(1), m.group(2)
                    for k in range(0, 5):
                        t = letters[len(letters) - k:] + rest if k else rest
                        if _reflike(t):
                            out.add(t)
        # continuation lists: 'RE250106232 106234 106402' -> RE250106234, RE250106402
        base = None
        for w in words:
            m = _PREFIXED.match(w)
            if m and len(m.group(2)) >= 4:
                base = m
                continue
            if base is not None and w.isdigit():
                pre, dig = base.group(1), base.group(2)
                if 3 <= len(w) <= len(dig):
                    t = pre + dig[: len(dig) - len(w)] + w
                    if _reflike(t):
                        out.add(t)
                continue
            base = None
    return tuple(sorted(out))


def build(line: Line) -> None:
    """Fill line.t_join and line.tokens."""
    text = unwrap(line.text or "")
    line.t_join = fold(" ".join(f for f in (text, line.ref1, line.ref2, line.rinfo, line.cpty_name, line.e2e) if f))
    line.tokens = tokens_of(text, line.ref1, line.ref2, line.rinfo, line.e2e)
