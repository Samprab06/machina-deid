"""Link preservation and identity-aware URI rewrite for the scoped PDF rewrite (attempt 2c).

MuPDF's ``apply_redactions`` deletes every link annotation whose hot area overlaps a redaction, including
links whose destination has nothing to do with the edited value. The writer therefore

1. snapshots the page's link annotations (xref + object source) before redacting,
2. re-attaches any link the redaction dropped (the *same* object, so action, border, flags and destination
   are untouched), and
3. rewrites only URI / file-spec strings that contain an already-resolved scoped value (exact, boundary
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


def rewrite_uri(uri: str, sub) -> tuple[str, int]:
    """Substitute resolved values in a URI string; percent-encoded occurrences are decoded first."""
    new, n = sub(uri)
    if n and not _contains(unquote(new), sub):
        return new, n
    dec = unquote(new)
    new2, n2 = sub(dec)
    if n2:
        return quote(new2, safe=":/?#[]@!$&'()*+,;=~%"), n + n2
    return new, n


def _contains(s: str, sub) -> bool:
    return bool(sub.rx and sub.rx.search(s))


def _action_target(doc, xref: int) -> tuple[int, str]:
    """(xref, key prefix) addressing the link's /A dictionary."""
    typ, val = doc.xref_get_key(xref, "A")
    if typ == "xref":
        return int(val.split()[0]), ""
    if typ == "dict":
        return xref, "A/"
    return 0, ""


def rewrite_link_actions(doc, sub) -> int:
    """Rewrite /URI and string /F of every link action that contains a resolved value."""
    n_total = 0
    for page in doc:
        for x, t, _ in page.annot_xrefs():
            if t != _LINK:
                continue
            ax, pre = _action_target(doc, x)
            if not ax:
                continue
            for key in ("URI", "F"):
                typ, val = doc.xref_get_key(ax, pre + key)
                if typ != "string" or not val:
                    continue
                new, n = rewrite_uri(val, sub)
                if n and new != val:
                    doc.xref_set_key(ax, pre + key, pymupdf.get_pdf_str(new))
                    n_total += n
    return n_total


# ------------------------------------------------------------------ independent verification


def _r(v) -> tuple:
    return tuple(round(float(c), 1) for c in v) if v is not None else ()


def _norm_links(page) -> list[dict]:
    out = []
    for ln in page.get_links():
        out.append({"kind": ln.get("kind"), "rect": _r(ln.get("from")), "page": ln.get("page", -1),
                    "to": _r(ln.get("to")) if ln.get("to") is not None else (),
                    "nameddest": str(ln.get("nameddest") or ""), "uri": str(ln.get("uri") or ""),
                    "file": str(ln.get("file") or "")})
    return out


class _ValueCheck:
    """Case-insensitive, boundary-free containment check of resolved values (raw and percent-decoded)."""

    def __init__(self, values):
        self.vals = [re.sub(r"\s+", "", v.casefold()) for v in values if v.strip()]

    def __call__(self, s: str) -> bool:
        flat = re.sub(r"\s+", "", (s + "\n" + unquote(s)).casefold())
        return any(v in flat for v in self.vals)


def verify_links(src_doc, out_doc, values) -> Counter:
    """Value-free failure codes (empty Counter = links preserved / identity-aware rewritten correctly)."""
    bad: Counter = Counter()
    has = _ValueCheck(values)
    for pno in range(src_doc.page_count):
        before = sorted(_norm_links(src_doc[pno]), key=lambda d: (d["kind"], d["rect"], d["uri"]))
        after = sorted(_norm_links(out_doc[pno]), key=lambda d: (d["kind"], d["rect"], d["uri"]))
        if len(before) != len(after):
            bad["link_count_changed"] += 1
            continue
        geo_a = Counter((d["kind"], d["rect"]) for d in before)
        geo_b = Counter((d["kind"], d["rect"]) for d in after)
        if geo_a != geo_b:
            bad["link_geometry_changed"] += 1
            continue
        # pair links by (kind, rect); within equal geometry compare as multisets
        groups_a, groups_b = {}, {}
        for d in before:
            groups_a.setdefault((d["kind"], d["rect"]), []).append(d)
        for d in after:
            groups_b.setdefault((d["kind"], d["rect"]), []).append(d)
        for k, la in groups_a.items():
            lb = list(groups_b[k])
            for a in la:
                match = None
                for b in lb:
                    if _same_action(a, b, has):
                        match = b
                        break
                if match is None:
                    bad["link_action_changed"] += 1
                else:
                    lb.remove(match)
    return bad


def _same_action(a: dict, b: dict, has) -> bool:
    for k in ("page", "to", "nameddest"):
        if a[k] != b[k]:
            return False
    for k in ("uri", "file"):
        if not has(a[k]):
            if a[k] != b[k]:
                return False  # untouched destination must be identical
        else:
            if has(b[k]) or not b[k]:
                return False  # identity left in the destination, or destination lost
            if urlsplit(a[k]).scheme.lower() != urlsplit(b[k]).scheme.lower():
                return False
    return True
