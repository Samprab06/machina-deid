import ezdxf
import pytest

from machina_deid.dxf_transform import invariants, transform_dxf
from machina_deid.idmap import IdentityMap

NAME = "Jane Quincy Doe"
CO = "Acme Widgets"


def make_dxf(path):
    doc = ezdxf.new("R2010")
    doc.layers.add(f"{CO}-WALLS")
    doc.header["$PROJECTNAME"] = f"{CO} project"
    doc.header.custom_vars.append("Owner", NAME)
    blk = doc.blocks.new("TITLE_" + CO[:4])
    blk.add_attdef("OWNER", (0, 0), text=NAME, height=2.5)
    blk.add_text(f"Drawn by {NAME}", height=2.5, dxfattribs={"insert": (1, 1)})
    msp = doc.modelspace()
    msp.add_line((0, 0), (100.25, 50.5), dxfattribs={"layer": f"{CO}-WALLS"})
    msp.add_circle((10, 10), 12.5)
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10.125)])
    msp.add_text(f"Owner: {NAME} / 12.500", height=3, dxfattribs={"insert": (5, 5)})
    msp.add_mtext(f"{NAME}\\PSecond line {CO}", dxfattribs={"insert": (0, 20)})
    ins = msp.add_blockref("TITLE_" + CO[:4], (0, 0))
    ins.add_auto_attribs({"OWNER": NAME})
    msp.add_linear_dim(base=(0, -5), p1=(0, 0), p2=(100, 0), override={"dimtxt": 2.5}).render()
    doc.layout("Layout1").add_text(f"Sheet for {NAME}", height=2, dxfattribs={"insert": (1, 1)})
    doc.saveas(path)


def all_text(doc):
    out = []
    for lay in list(doc.layouts) + list(doc.blocks):
        for e in lay:
            t = e.dxftype()
            if t == "TEXT":
                out.append(e.dxf.text)
            elif t == "MTEXT":
                out.append(e.text)
            elif t == "INSERT":
                out += [a.dxf.text for a in e.attribs]
            elif t == "ATTDEF":
                out.append(e.dxf.text)
    return "\n".join(out)


def test_text_attribs_header_transformed_geometry_intact(tmp_path):
    src, dst = tmp_path / "in.dxf", tmp_path / "out" / "in.dxf"
    make_dxf(src)
    m = IdentityMap(strings={NAME: "Person-ABCDE", CO: "Organization-FGHIJ"})
    res = transform_dxf(src, dst, m)
    assert res.ok, res.to_dict()
    assert res.entity_counts_equal and res.numeric_invariants_equal
    assert res.text_replacements[0] >= 5 and res.text_replacements[1] >= 1
    assert res.header_replacements == 2
    out = ezdxf.readfile(dst)
    text = all_text(out)
    assert NAME not in text and CO not in text and "Person-ABCDE" in text
    assert "12.500" in text  # numeric text untouched
    assert NAME not in out.header["$PROJECTNAME"] and "Organization-FGHIJ" in out.header["$PROJECTNAME"]
    assert "Person-ABCDE" in dict(out.header.custom_vars)["Owner"]
    before = invariants(ezdxf.readfile(src))
    assert before[0] == invariants(out)[0]
    assert any("DIMENSION" in w for w in res.warnings)
    assert res.unsupported_types == {"DIMENSION": 1}
    assert NAME.encode() not in dst.read_bytes()


def test_layer_and_block_names_only_when_mapped(tmp_path):
    src = tmp_path / "in.dxf"
    make_dxf(src)
    res = transform_dxf(src, tmp_path / "a.dxf", IdentityMap(strings={NAME: "P-1"}))
    assert res.ok and res.layers_renamed == 0 and res.blocks_renamed == 0
    assert f"{CO}-WALLS" in ezdxf.readfile(tmp_path / "a.dxf").layers  # unmapped => untouched
    assert res.residual_raw == {} and f"{CO}" not in res.to_dict().__str__()

    m = IdentityMap(strings={CO: "Org-1"}, layers={f"{CO}-WALLS": "Org-1-WALLS"}, blocks={"TITLE_" + CO[:4]: "TITLE_X"})
    res = transform_dxf(src, tmp_path / "b.dxf", m)
    assert res.ok, res.to_dict()
    out = ezdxf.readfile(tmp_path / "b.dxf")
    assert "Org-1-WALLS" in out.layers and f"{CO}-WALLS" not in out.layers
    assert "TITLE_X" in out.blocks and ("TITLE_" + CO[:4]) not in out.blocks
    line = next(e for e in out.modelspace() if e.dxftype() == "LINE")
    assert line.dxf.layer == "Org-1-WALLS" and tuple(line.dxf.end)[:2] == (100.25, 50.5)
    assert res.layers_renamed == 1 and res.blocks_renamed == 1


def test_unmapped_string_in_dimension_override_reported_not_changed(tmp_path):
    src = tmp_path / "in.dxf"
    doc = ezdxf.new("R2010")
    doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0), text=f"{NAME} 10").render()
    doc.saveas(src)
    res = transform_dxf(src, tmp_path / "o.dxf", IdentityMap(strings={NAME: "P"}))
    assert res.residual_raw  # raw scan sees it in an untransformed location
    assert any("untransformed locations" in w for w in res.warnings)
    assert res.numeric_invariants_equal


def test_refuses_same_path(tmp_path):
    src = tmp_path / "in.dxf"
    make_dxf(src)
    with pytest.raises(ValueError):
        transform_dxf(src, src, IdentityMap(strings={NAME: "P"}), overwrite=True)
