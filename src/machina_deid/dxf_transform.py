"""DXF de-identification with ezdxf.

Only explicitly mapped strings are changed, and only in: TEXT, MTEXT, ATTRIB,
ATTDEF text values (in modelspace, paperspace layouts and block definitions),
header string variables / custom properties, and (when listed under ``layers`` /
``blocks`` in the identity map) layer and block names. DIMENSION measurement
values, geometry and all numeric attributes are never altered. Entities of
other types are preserved untouched; ones that may carry text (DIMENSION,
MLEADER, LEADER, TOLERANCE) are reported as warnings, and a raw scan of the
output reports any remaining occurrence of a mapped string.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ezdxf
from ezdxf.math import Vec2, Vec3

from .idmap import IdentityMap

_TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
_TEXT_RISK_TYPES = {"DIMENSION", "MLEADER", "MULTILEADER", "LEADER", "TOLERANCE"}
_HEADER_STR_VARS = ("$PROJECTNAME", "$LASTSAVEDBY", "$MENU")


@dataclass
class DxfResult:
    ok: bool = False
    entity_counts_equal: bool = False
    numeric_invariants_equal: bool = False
    entities: int = 0
    text_replacements: dict[int, int] = field(default_factory=dict)
    header_replacements: int = 0
    verified_identity_fields_changed: int = 0
    layers_renamed: int = 0
    blocks_renamed: int = 0
    residual_in_entities: dict[int, int] = field(default_factory=dict)
    residual_raw: dict[int, int] = field(default_factory=dict)
    new_strings_present: bool = True
    unsupported_types: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    output_sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _spaces(doc):
    """All entity containers: layouts (model + paper) and block definitions (deduplicated)."""
    seen, out = set(), []
    for lay in list(doc.layouts) + list(doc.blocks):
        key = id(lay.block_record) if hasattr(lay, "block_record") else id(lay)
        if key in seen:
            continue
        seen.add(key)
        out.append(lay)
    return out


def _entities(doc):
    for space in _spaces(doc):
        for e in space:
            yield e
            if e.dxftype() == "INSERT":
                yield from e.attribs


def _nums(v) -> list[float] | None:
    if isinstance(v, bool):
        return [float(v)]
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, (Vec3, Vec2)):
        return [float(c) for c in v]
    if isinstance(v, (tuple, list)) and all(isinstance(c, (int, float)) for c in v):
        return [float(c) for c in v]
    return None


def invariants(doc) -> tuple[dict[str, int], dict[str, tuple]]:
    """(entity counts by type, handle -> (type, numeric attribute vector))."""
    counts: dict[str, int] = {}
    nums: dict[str, tuple] = {}
    for e in _entities(doc):
        t = e.dxftype()
        counts[t] = counts.get(t, 0) + 1
        vec: list[tuple[str, tuple]] = []
        for name in sorted(e.dxf.all_existing_dxf_attribs()):
            n = _nums(e.dxf.get(name))
            if n is not None:
                vec.append((name, tuple(n)))
        if t == "LWPOLYLINE":
            vec.append(("_pts", tuple(tuple(p) for p in e.get_points("xyseb"))))
        elif t == "POLYLINE":
            vec.append(("_pts", tuple(tuple(v.dxf.location) for v in e.vertices)))
        elif t == "SPLINE":
            vec.append(("_cp", tuple(tuple(p) for p in e.control_points)))
        elif t == "DIMENSION":  # measured values + override text must stay as-is
            vec.append(("_text", (e.dxf.get("text", ""),)))
        nums[f"{e.dxf.handle}"] = (t, tuple(vec))
    return counts, nums


def _close(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    return a == b


_OPTIONAL_SERIALIZATION_ATTRS = {
    "angle", "const_width", "extrusion", "flow_direction",
    "line_spacing_factor", "line_spacing_style"
}


def _compare(before: dict, after: dict) -> bool:
    """Compare persisted geometry while allowing ezdxf default attributes.

    ezdxf may materialize a default value on save (for example MTEXT angle) or
    omit an optional default (for example line-spacing fields). Common values,
    including dimension measurements, insertion points, vertices, and explicit
    dimension text, must still match exactly within tolerance.
    """
    if before.keys() != after.keys():
        return False
    for handle, (before_type, before_vec) in before.items():
        after_type, after_vec = after[handle]
        if before_type != after_type:
            return False
        bm, am = dict(before_vec), dict(after_vec)
        if set(bm) - set(am) - _OPTIONAL_SERIALIZATION_ATTRS:
            return False
        for name in set(bm) & set(am):
            if not _close(bm[name], am[name]):
                return False
    return True


def _replace(text: str, pairs: list[tuple[str, str]], index: dict[str, int], tally: dict[int, int]) -> str:
    for old, new in pairs:
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            tally[index[old]] = tally.get(index[old], 0) + n
    return text


def _entity_texts(doc):
    for e in _entities(doc):
        t = e.dxftype()
        if t in _TEXT_TYPES:
            yield e, (e.text if t == "MTEXT" else e.dxf.get("text", ""))


def transform_dxf(src: str | Path, dst: str | Path, idmap: IdentityMap, *, overwrite: bool = False) -> DxfResult:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("output path must differ from source path")
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    res = DxfResult()
    pairs = idmap.ordered_strings()
    index = {old: i for i, old in enumerate(idmap.strings)}

    doc = ezdxf.readfile(src)
    counts_before, nums_before = invariants(doc)
    res.entities = sum(counts_before.values())
    res.unsupported_types = {t: n for t, n in sorted(counts_before.items()) if t in _TEXT_RISK_TYPES}
    for t, n in res.unsupported_types.items():
        res.warnings.append(f"{n} {t} entities not text-transformed (may carry identity text)")

    edited_texts = {}
    for e, text in list(_entity_texts(doc)):
        new = _replace(text, pairs, index, res.text_replacements)
        if new != text:
            edited_texts[e.dxf.handle] = new
            if e.dxftype() == "MTEXT":
                e.text = new
            else:
                e.dxf.text = new

    # header string variables and custom properties
    edited_headers = {}
    for var in _HEADER_STR_VARS:
        if var in doc.header:
            old = doc.header[var]
            new = _replace(old, pairs, index, {}) if isinstance(old, str) else old
            if new != old:
                doc.header[var] = new
                edited_headers[var] = new
                res.header_replacements += 1
    cv = doc.header.custom_vars
    original_custom = list(cv)
    for tag, value in original_custom:
        new = _replace(value, pairs, index, {})
        if new != value:
            cv.replace(tag, new)
    custom_changed = sum(old != new for old, new in zip(original_custom, cv))
    res.header_replacements += custom_changed

    # explicitly mapped layer / block names
    if idmap.layers:
        for e in _entities(doc):
            lay = e.dxf.get("layer")
            if lay in idmap.layers:
                e.dxf.layer = idmap.layers[lay]
        for old, new in idmap.layers.items():
            if old in doc.layers:
                if new in doc.layers:
                    raise ValueError("target layer name already exists")
                doc.layers.get(old).dxf.name = new
                res.layers_renamed += 1
    for old, new in idmap.blocks.items():
        if old in doc.blocks:
            doc.blocks.rename_block(old, new)
            for e in _entities(doc):
                if e.dxftype() == "INSERT" and e.dxf.name == old:
                    e.dxf.name = new
            res.blocks_renamed += 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".partial")
    doc.saveas(tmp)

    out = ezdxf.readfile(tmp)
    counts_after, nums_after = invariants(out)
    res.entity_counts_equal = counts_before == counts_after
    res.numeric_invariants_equal = _compare(nums_before, nums_after)

    after_texts_by_handle = {e.dxf.handle: text for e, text in _entity_texts(out)}
    readback_ok = (all(after_texts_by_handle.get(h) == text for h, text in edited_texts.items())
                   and all(out.header.get(var) == value for var, value in edited_headers.items())
                   and (not custom_changed or list(out.header.custom_vars) == list(cv)))
    after_texts = list(after_texts_by_handle.values())
    for old, _ in pairs:
        n = sum(t.count(old) for t in after_texts)
        if n:
            res.residual_in_entities[index[old]] = n
    for old, new in pairs:
        if res.text_replacements.get(index[old]) and not any(new in t for t in after_texts):
            res.new_strings_present = False
    for old, new in idmap.layers.items():
        if old in out.layers or (res.layers_renamed and new not in out.layers):
            res.warnings.append("layer rename not reflected in output")
    for old in idmap.blocks:
        if old in out.blocks:
            res.warnings.append("block rename not reflected in output")

    raw = tmp.read_bytes()
    for old, _ in pairs:
        n = max(raw.count(old.encode(enc, "ignore")) for enc in ("utf-8", "cp1252"))
        if n:
            res.residual_raw[index[old]] = n
    if res.residual_raw:
        res.warnings.append("mapped strings still present in untransformed locations (raw scan)")

    if not readback_ok:
        res.warnings.append("edited identity fields differ on readback")
    res.ok = (readback_ok and res.entity_counts_equal and res.numeric_invariants_equal and res.new_strings_present
              and not res.residual_in_entities and not any("rename not reflected" in w for w in res.warnings))
    if res.ok:
        tmp.replace(dst)
        res.output_sha256 = _sha256(dst)
        res.verified_identity_fields_changed = len(edited_texts) + res.header_replacements
    else:
        tmp.unlink()
    return res
