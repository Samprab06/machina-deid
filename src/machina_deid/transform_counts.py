"""Redacted counts of identity-bearing fields changed in validated output."""
from dataclasses import asdict, dataclass


@dataclass
class TransformCounts:
    pdf_visible_text_replacements: int = 0
    pdf_metadata_identities_changed: int = 0
    pdf_attachment_changes: int = 0
    pdf_form_changes: int = 0
    pdf_annotation_changes: int = 0
    step_header_replacements: int = 0
    step_data_replacements: int = 0
    dxf_replacements: int = 0
    step_descriptive_replacements: int = 0  # attempt 2b: scoped PRODUCT/structure descriptive literals
    mesh_header_replacements: int = 0  # attempt 2b: STL header/solid name
    svg_metadata_replacements: int = 0  # attempt 2b: SVG RDF/Dublin Core metadata
    ole_property_replacements: int = 0  # attempt 2c: SummaryInformation saved-account property
    ole_cross_stream_replacements: int = 0  # attempt 2c: verified MFC CString / path-component records

    @property
    def total(self) -> int:
        return sum(asdict(self).values())

    def add(self, other: "TransformCounts") -> None:
        for name, count in asdict(other).items():
            setattr(self, name, getattr(self, name) + count)
