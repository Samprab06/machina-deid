"""Link preservation and identity-aware URI rewrite for the scoped PDF rewrite (attempt 2c).

MuPDF's ``apply_redactions`` deletes every link annotation whose hot area overlaps a redaction, including
links whose destination has nothing to do with the edited value. The writer therefore

1. snapshots the page's link annotations (xref + object source) before redacting,
2. re-attaches any link the redaction dropped (the *same* object, so action, border, flags and destination
   are untouched), and
3. rewrites only URI actions that contain an already-resolved scoped value (exact, boundary
   delimited, also percent-encoded); every other link action stays byte-identical.

:func:`verify_links` is the independent check: it compares the normalized link list of the source and the
output (kind, hot-area geometry, page/target/named destination, URI/file) per page and returns value-free
failure codes. It does not reuse the writer's substitution: an unchanged link must be identical, a link that
contained a resolved value must keep its scheme and no longer contain any resolved value.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import quote, unquote, urlsplit

import pymupdf

_LINK = pymupdf.PDF_ANNOT_LINK


# ------------------------------------------------------------------ writer side


def link_snapshot(doc, page) -> list[tuple[int, str]]:
    """(xref, object source) of the page's link annotations, in /Annots order."""
    return [(x, doc.xref_object(x, compressed=True)) for x, t, _ in page.annot_xrefs() if t == _LINK]


def _set_annots(doc, page, refs: list[int]) -> None:
    arr = "[" + " ".join(f"{x} 0 R" for x in refs) + "]"
    typ, val = doc.xref_get_key(page.xref, "Annots")
    if typ == "xref":
        doc.update_object(int(val.split()[0]), arr)
    else:
        doc.xref_set_key(page.xref, "Annots", arr)


def restore_links(doc, page, snap: list[tuple[int, str]], order_before: list[int]) -> int:
    """Re-attach link annotations dropped by redaction. Returns the number restored."""
    present = [x for x, _, _ in page.annot_xrefs()]
    missing = [(x, src) for x, src in snap if x not in present]
    if not missing:
        return 0
    for x, src in missing:
        if doc.xref_object(x, compressed=True) != src:
            doc.update_object(x, src)
    want = set(present) | {x for x, _ in missing}
    refs = [x for x in order_before if x in want] + [x for x in present if x not in order_before]
    _set_annots(doc, page, refs)
    return len(missing)


_SCHEME = re.compile(r"^(https?|mailto):", re.I)
_WEB = re.compile(r"^(?:https?://)?(?P<host>www\.[a-z0-9.-]+|(?<=://)[a-z0-9.-]+)(?P<tail>[/?#].*)?$", re.I)
_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I)
_URI_PART = re.compile(r"[^/?#&=;,]+")
_PERCENT_RUN = re.compile(r"(?:%[0-9a-fA-F]{2})+")
_MAILBOX = re.compile(r"[^\s@]+@[^\s@]+")
_MAIL_BOUNDARY = r"[\w.%+@-]"


def _uri_text(value: str) -> str:
    """Recognize a slash-prefixed spelling, including a PDF name's leading slash."""
    return value[1:] if value.startswith("/") and _SCHEME.match(value[1:]) else value


def _token_pattern(value: str) -> str:
    boundary = _MAIL_BOUNDARY if _MAILBOX.fullmatch(value) else r"[^\W_]"
    return rf"(?<!{boundary})" + r"\s+".join(map(re.escape, value.split())) + rf"(?!{boundary})"


def _decoded_spans(raw: str) -> tuple[str, list[int]]:
    """Map decoded character boundaries to raw offsets, leaving invalid UTF-8 escaped."""
    chars, offsets = [], [0]
    pos = 0
    for match in _PERCENT_RUN.finditer(raw):
        chars.append(raw[pos:match.start()])
        offsets.extend(range(pos + 1, match.start() + 1))
        try:
            decoded = unquote(match[0], errors="strict")
        except UnicodeDecodeError:
            decoded = match[0]
        stride = 1 if decoded == match[0] else 3
        pos = match.start()
        for char in decoded:
            pos += stride * len(char.encode("utf-8"))
            offsets.append(pos)
        chars.append(decoded)
    chars.append(raw[pos:])
    offsets.extend(range(pos + 1, len(raw) + 1))
    return "".join(chars), offsets


def _rewrite_tokens(raw: str, pattern, replacements) -> tuple[str, int]:
    decoded, offsets = _decoded_spans(raw)
    n = 0
    pieces = []
    pos = 0
    for match in pattern.finditer(decoded):
        replacement = replacements.get(" ".join(match[0].split()).casefold())
        if replacement is None:
            continue
        start, end = offsets[match.start()], offsets[match.end()]
        # Keep an encoded mailbox separator encoded. Leave all untouched raw spans intact.
        safe = "@" if "@" in raw[start:end] else ""
        pieces.extend((raw[pos:start], quote(replacement, safe=safe)))
        pos = end
        n += 1
    return "".join((*pieces, raw[pos:])), n


def _web_tail_matches(tail: str, approved: str) -> bool:
    # The visible match may end before a query/fragment or at a path prefix.
    # Compare spelling without decoding structural escapes such as %2F into /.
    escape_case = lambda s: _PERCENT_RUN.sub(lambda m: m[0].upper(), s)
    tail, approved = escape_case(tail), escape_case(approved)
    return tail == approved or (
        tail.startswith(approved) and (approved.endswith("/") or tail[len(approved)] in "/?#&")
    )


class _UriRewrite:
    def __init__(self, pairs):
        self.web = []
        self.tokens = {}
        for old, new in pairs:
            web = _WEB.fullmatch(old)
            if web and _HOST.fullmatch(new):
                self.web.append((web["host"].casefold(), web["tail"] or "", new))
            elif old.strip():
                self.tokens[" ".join(old.split()).casefold()] = new
        patterns = [_token_pattern(v) for v in sorted(self.tokens, key=len, reverse=True)]
        self.pattern = re.compile("|".join(patterns), re.I) if patterns else None

    def __call__(self, raw: str) -> tuple[str, int]:
        uri = _uri_text(raw)
        scheme = _SCHEME.match(uri)
        if not scheme:
            return raw, 0
        prefix, body = scheme[0], uri[scheme.end():]
        n = 0
        if prefix.casefold() != "mailto:":
            # Preserve authority delimiters, user info, port and the complete request target.
            authority = re.match(r"//(?:[^/?#]*@)?([^:/?#]+)(?::[0-9]+)?(?=[/?#]|$)", body)
            if not authority:
                return raw, 0
            host = authority[1].casefold()
            tail = body[authority.end():]
            for old, path, new in self.web:
                if host == old and (not path or _web_tail_matches(tail, path)):
                    start, end = authority.span(1)
                    prefix += body[:start] + new + body[end:authority.end()]
                    n += 1
                    break
            else:
                prefix += authority[0]
            body = tail

        def replace(match):
            nonlocal n
            result, count = _rewrite_tokens(match[0], self.pattern, self.tokens)
            n += count
            return result

        if self.pattern:
            body = _URI_PART.sub(replace, body)
        return (prefix + body, n) if n else (raw, 0)


def _action_target(doc, xref: int) -> tuple[int, str]:
    """(xref, key prefix) addressing the link's /A dictionary."""
    typ, val = doc.xref_get_key(xref, "A")
    if typ == "xref":
        return int(val.split()[0]), ""
    if typ == "dict":
        return xref, "A/"
    return 0, ""


def _uri_action(doc, xref):
    ax, pre = _action_target(doc, xref)
    if ax and doc.xref_get_key(ax, pre + "S") == ("name", "/URI"):
        return ax, pre, *doc.xref_get_key(ax, pre + "URI")
    return 0, "", "null", ""


def _nested_action(doc, xref):
    ax, pre = _action_target(doc, xref)
    return (doc.xref_get_key(xref, "AA")[0] != "null"
            or (ax and doc.xref_get_key(ax, pre + "Next")[0] != "null"))


def raw_link_uri(doc, xref: int) -> str:
    """Read the PDF action value even when MuPDF exposes no interpreted URI."""
    _, _, typ, val = _uri_action(doc, xref)
    return val if typ in ("string", "name") else ""


def rewrite_link_actions(doc, sub) -> int:
    """Rewrite simple URI actions on the restored annotation list."""
    rewrite = _UriRewrite(sub.pairs)
    n_total = 0
    for page in doc:
        for xref, kind, _ in page.annot_xrefs():
            if kind != _LINK or _nested_action(doc, xref):
                continue
            ax, pre, typ, val = _uri_action(doc, xref)
            if typ not in ("string", "name"):
                continue
            new, n = rewrite(val)
            if not n or new == val:
                continue
            if not pre:
                # An indirect action can also be used by another PDF object.
                clone = doc.get_new_xref()
                doc.update_object(clone, doc.xref_object(ax))
                doc.xref_set_key(xref, "A", f"{clone} 0 R")
                ax = clone
            doc.xref_set_key(ax, pre + "URI", pymupdf.get_pdf_str(new))
            n_total += n
    return n_total


# ------------------------------------------------------------------ independent verification


def _r(v) -> tuple:
    return tuple(round(float(c), 1) for c in v) if v is not None else ()


def _norm_links(page) -> list[dict]:
    interpreted = {ln["xref"]: ln for ln in page.get_links() if ln["xref"] > 0}
    out = []
    doc = page.parent
    for xref, kind, _ in page.annot_xrefs():
        if kind != _LINK:
            continue
        ln = interpreted.pop(xref, {})
        typ, rect = doc.xref_get_key(xref, "Rect")
        raw = raw_link_uri(doc, xref)
        out.append({"kind": ln.get("kind"), "rect": rect,
                    "hot_area": _r(ln.get("from")), "page": ln.get("page", -1),
                    "to": _r(ln.get("to")), "nameddest": str(ln.get("nameddest") or ""),
                    "uri": str(ln.get("uri") or ""), "file": str(ln.get("file") or ""),
                    "raw": raw, "nested": _nested_action(doc, xref)})
    return out


class _ValueCheck:
    """Check for exact approved tokens independently of the replacement routine."""

    def __init__(self, values):
        self.patterns = [re.compile(_token_pattern(v.strip()), re.I) for v in values if v.strip()]
        self.web = [m for v in values if (m := _WEB.fullmatch(v))]

    def __call__(self, value: str) -> bool:
        uri = _uri_text(value)
        for spelling in (uri, unquote(uri)):
            if any(pattern.search(spelling) for pattern in self.patterns):
                return True
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return False
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        tail = re.split(r"://[^/?#]*", uri, maxsplit=1)[-1]
        return any(parsed.hostname == m["host"].casefold()
                   and (not m["tail"] or _web_tail_matches(tail, m["tail"])) for m in self.web)


def verify_links(src_doc, out_doc, values) -> Counter:
    """Value-free failure codes (empty Counter = links preserved / identity-aware rewritten correctly)."""
    bad: Counter = Counter()
    has = _ValueCheck(values)
    for pno in range(src_doc.page_count):
        before, after = _norm_links(src_doc[pno]), _norm_links(out_doc[pno])
        if len(before) != len(after):
            bad["link_count_changed"] += 1
            continue
        if Counter(d["rect"] for d in before) != Counter(d["rect"] for d in after):
            bad["link_geometry_changed"] += 1
            continue
        if has.patterns and any(d["nested"] for d in before + after):
            bad["link_nested_action_unsupported"] += 1
        remaining = list(after)
        for a in before:
            for i, b in enumerate(remaining):
                if a["rect"] == b["rect"] and _same_action(a, b, has):
                    remaining.pop(i)
                    break
            else:
                bad["link_action_changed"] += 1
    return bad


def _same_action(a: dict, b: dict, has) -> bool:
    if has(a["raw"]):
        old_scheme = _SCHEME.match(_uri_text(a["raw"]))
        new_scheme = _SCHEME.match(b["raw"])
        if not old_scheme or not new_scheme or old_scheme[0].casefold() != new_scheme[0].casefold():
            return False
        if b["kind"] != pymupdf.LINK_URI or not b["uri"] or has(b["raw"]) or has(b["uri"]):
            return False
        try:
            parsed = urlsplit(b["raw"])
        except ValueError:
            return False
        if parsed.scheme.lower() in ("http", "https") and not parsed.hostname:
            return False
        return a["hot_area"] == b["hot_area"] or not a["hot_area"]
    return a == b and not any(has(b[k]) for k in ("uri", "file", "nameddest"))
