"""Tests for spreadsheet_drawings.py (pictures and charts of a workbook) and the
Markdown side of it in spreadsheet.py.

Everything here runs without LibreOffice: .xlsx edge cases are built in-test as
minimal packages, and the .ods / .xls fixtures were saved by LibreOffice once, by
generate_test_files.py. Renderings are faked with real PNG files.
"""

import io
import re
import zipfile
from pathlib import Path

import pytest
from PIL import Image

import spreadsheet_drawings
from libreoffice import RenderedShape
from schemas import SpreadsheetMode
from spreadsheet import (
    assemble_sections,
    build_spreadsheet_document,
    is_sparse,
    merge_sheet_bodies,
    render_figures,
)
from spreadsheet_drawings import (
    DrawingSurvey,
    Figure,
    SheetObject,
    WorkbookDrawings,
    decode_objects,
    discover_ods,
    discover_xlsx,
    format_chart_summary,
    needs_libreoffice,
    summarize_xlsx_chart,
    survey_drawings,
    to_rgb,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A cell range as Excel or ODF writes it — must never reach the Markdown.
_CELL_RANGE_RE = re.compile(
    r"\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+|!\$?[A-Z]{1,3}\$?\d+"
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

NS = (
    'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _png(size=(120, 90), mode="RGB", color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _rels(*entries: tuple[str, str, str, str | None]) -> str:
    rows = "".join(
        f'<Relationship Id="{rid}" Type="{REL}/{rtype}" Target="{target}"'
        + (f' TargetMode="{mode}"' if mode else "")
        + "/>"
        for rid, rtype, target, mode in entries
    )
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rows}</Relationships>"
    )


def _pic(
    name: str, rel: str, descr: str = "", extra: str = "", link: bool = False
) -> str:
    attr = "r:link" if link else "r:embed"
    return (
        f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="{name}" descr="{descr}" {extra}/>'
        f'<xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip {attr}="{rel}"/></xdr:blipFill>'
        "</xdr:pic>"
    )


def _chart_frame(name: str, rel: str, chartex: bool = False) -> str:
    prefix = "cx" if chartex else "c"
    return (
        f'<xdr:graphicFrame><xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="{name}"/>'
        "</xdr:nvGraphicFramePr><a:graphic><a:graphicData>"
        f'<{prefix}:chart r:id="{rel}"/></a:graphicData></a:graphic></xdr:graphicFrame>'
    )


def _anchor(kind: str, row: int, col: int, body: str) -> str:
    start = f"<xdr:from><xdr:col>{col}</xdr:col><xdr:row>{row}</xdr:row></xdr:from>"
    if kind == "absoluteAnchor":
        start = '<xdr:pos x="0" y="0"/>'
    return f"<xdr:{kind}>{start}{body}<xdr:clientData/></xdr:{kind}>"


CHART_MIXED = f"""<c:chartSpace {NS}><c:chart>
<c:title><c:tx><c:rich><a:p><a:r><a:t>Mixed</a:t></a:r></a:p></c:rich></c:tx></c:title>
<c:plotArea><c:barChart><c:barDir val="bar"/>
 <c:ser><c:tx><c:strRef><c:f>'My ''Data'''!$B$1</c:f><c:strCache><c:pt idx="0"><c:v>A</c:v></c:pt></c:strCache></c:strRef></c:tx>
  <c:cat><c:strRef><c:f>'My ''Data'''!$A$2:$A$4</c:f><c:strCache>
   <c:pt idx="1"><c:v>y</c:v></c:pt><c:pt idx="0"><c:v>x</c:v></c:pt><c:pt idx="2"><c:v>z</c:v></c:pt>
  </c:strCache></c:strRef></c:cat>
  <c:val><c:numRef><c:f>'My ''Data'''!$B$2:$B$4</c:f><c:numCache>
   <c:pt idx="0"><c:v>101</c:v></c:pt><c:pt idx="1"><c:v>102</c:v></c:pt><c:pt idx="2"><c:v>103</c:v></c:pt>
  </c:numCache></c:numRef></c:val></c:ser>
 <c:ser><c:tx><c:v>Literal</c:v></c:tx>
  <c:val><c:numLit><c:pt idx="0"><c:v>7</c:v></c:pt><c:pt idx="1"><c:v>8</c:v></c:pt><c:pt idx="2"><c:v>9</c:v></c:pt></c:numLit></c:val></c:ser>
 <c:ser><c:tx><c:strRef><c:f>[1]Other!$C$1</c:f><c:strCache><c:pt idx="0"><c:v>Ext</c:v></c:pt></c:strCache></c:strRef></c:tx>
  <c:val><c:numRef><c:f>[1]Other!$C$2:$C$4</c:f><c:numCache>
   <c:pt idx="0"><c:v>4</c:v></c:pt><c:pt idx="1"><c:v>5</c:v></c:pt><c:pt idx="2"><c:v>6</c:v></c:pt>
  </c:numCache></c:numRef></c:val></c:ser>
</c:barChart>
<c:valAx><c:title><c:tx><c:rich><a:p><a:r><a:t>Units</a:t></a:r></a:p></c:rich></c:tx></c:title></c:valAx>
</c:plotArea></c:chart></c:chartSpace>"""

CHARTEX = f"""<cx:chartSpace {NS}><cx:chartData><cx:data id="0"/></cx:chartData>
<cx:chart><cx:title><cx:tx><cx:txData><cx:f>Sheet1!$A$1</cx:f><cx:v>Flow</cx:v></cx:txData></cx:tx></cx:title>
<cx:plotArea><cx:plotAreaRegion><cx:series layoutId="waterfall">
<cx:tx><cx:txData><cx:f>Sheet1!$B$1</cx:f><cx:v>Steps</cx:v></cx:txData></cx:tx>
</cx:series></cx:plotAreaRegion></cx:plotArea></cx:chart></cx:chartSpace>"""


def _edge_case_xlsx(path: Path) -> Path:
    """A package exercising every anchor / nesting rule of the drawing parser.

    Document order, with the on-sheet position (row, col) of each anchor:
      1. twoCellAnchor (10, 1)  picture "Photo lower", alt text
      2. oneCellAnchor (2, 5)   group: picture (generic alt) + chart "Mixed"
      3. absoluteAnchor         hidden picture — skipped
      4. twoCellAnchor (0, 0)   mc:AlternateContent: chartex (Choice) / shape (Fallback)
      5. oneCellAnchor (1, 0)   linked picture — skipped
      6. oneCellAnchor (3, 0)   SmartArt graphic frame — skipped
    """
    drawing = (
        f"<xdr:wsDr {NS}>"
        + _anchor(
            "twoCellAnchor",
            10,
            1,
            _pic("Photo lower", "rIdImg", "Warehouse &amp; dock"),
        )
        + _anchor(
            "oneCellAnchor",
            2,
            5,
            "<xdr:grpSp>"
            + _pic("Grouped pic", "rIdImg2", "Picture 7")
            + _chart_frame("Chart 1", "rIdChart")
            + "</xdr:grpSp>",
        )
        + _anchor("absoluteAnchor", 0, 0, _pic("Hidden", "rIdImg", extra='hidden="1"'))
        + _anchor(
            "twoCellAnchor",
            0,
            0,
            '<mc:AlternateContent><mc:Choice Requires="cx1">'
            + _chart_frame("Waterfall 1", "rIdChartex", chartex=True)
            + '</mc:Choice><mc:Fallback><xdr:sp><xdr:nvSpPr><xdr:cNvPr id="9" name="Placeholder"/>'
            "</xdr:nvSpPr></xdr:sp></mc:Fallback></mc:AlternateContent>",
        )
        + _anchor("oneCellAnchor", 1, 0, _pic("Linked", "rIdLink", link=True))
        + _anchor(
            "oneCellAnchor",
            3,
            0,
            '<xdr:graphicFrame><xdr:nvGraphicFramePr><xdr:cNvPr id="5" name="Diagram 1"/>'
            "</xdr:nvGraphicFramePr><a:graphic><a:graphicData>"
            '<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" r:dm="rIdDgm"/>'
            "</a:graphicData></a:graphic></xdr:graphicFrame>",
        )
        + "</xdr:wsDr>"
    )
    parts = {
        "_rels/.rels": _rels(("rId1", "officeDocument", "xl/workbook.xml", None)),
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'xmlns:r="{REL}"><sheets>'
            '<sheet name="My \'Data\'" sheetId="1" r:id="rId1"/>'
            '<sheet name="Plain" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": _rels(
            ("rId1", "worksheet", "worksheets/sheet1.xml", None),
            ("rId2", "worksheet", "/xl/worksheets/sheet2.xml", None),
        ),
        "xl/worksheets/sheet1.xml": "<worksheet/>",
        "xl/worksheets/sheet2.xml": "<worksheet/>",
        "xl/worksheets/_rels/sheet1.xml.rels": _rels(
            ("rId1", "drawing", "../drawings/drawing1.xml", None)
        ),
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": _rels(
            ("rIdImg", "image", "../media/image1.png", None),
            ("rIdImg2", "image", "../media/image2.png", None),
            ("rIdChart", "chart", "../charts/chart1.xml", None),
            ("rIdChartex", "chartEx", "../charts/chartEx1.xml", None),
            ("rIdLink", "image", "https://example.com/x.png", "External"),
        ),
        "xl/media/image1.png": _png(),
        "xl/media/image2.png": _png(color=(0, 0, 200)),
        "xl/charts/chart1.xml": CHART_MIXED,
        "xl/charts/chartEx1.xml": CHARTEX,
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


_NAME_RE = re.compile(r"^imgs/[0-9a-f]{16}\.jpg$")


def _pixel(image, xy: tuple[int, int]) -> tuple[int, ...]:
    """A pixel of an EncodedImage, decoded back from its JPEG bytes."""
    with Image.open(io.BytesIO(image.jpeg)) as im:
        return im.convert("RGB").getpixel(xy)


def _near(a: tuple[int, ...], b: tuple[int, ...], tolerance: int = 8) -> bool:
    """JPEG is lossy: compare colors with a tolerance."""
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def _zip_with(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _obj(kind, sheet, index, media=None, name="", summary="", alt="", key=None):
    return SheetObject(
        kind=kind,
        sheet=sheet,
        shape_name=name,
        kind_index=index,
        sort_key=key or (0, index, 0, index),
        media=media,
        alt_text=alt,
        summary=summary,
    )


# ---------------------------------------------------------------------------
#  .xlsx discovery
# ---------------------------------------------------------------------------


class TestDiscoverXlsx:
    def test_objects_follow_on_sheet_order_and_skip_what_is_out_of_scope(
        self, tmp_path
    ):
        drawings = discover_xlsx(_edge_case_xlsx(tmp_path / "edge.xlsx"))
        assert drawings.sheet_order == ("My 'Data'", "Plain")
        got = [(o.kind, o.shape_name, o.kind_index) for o in drawings.objects]
        assert got == [
            ("chart", "Waterfall 1", 1),  # row 0
            ("picture", "Grouped pic", 1),  # row 2, group children keep their order
            ("chart", "Chart 1", 0),
            ("picture", "Photo lower", 0),  # row 10
        ], "hidden, linked and SmartArt objects must be skipped"

    def test_media_alt_text_and_summaries(self, tmp_path):
        objects = {
            o.shape_name: o
            for o in discover_xlsx(_edge_case_xlsx(tmp_path / "e.xlsx")).objects
        }
        assert objects["Photo lower"].media == "xl/media/image1.png"
        assert objects["Photo lower"].alt_text == "Warehouse & dock"
        assert objects["Grouped pic"].alt_text == "", (
            "generic 'Picture 7' carries nothing"
        )
        assert objects["Waterfall 1"].summary.startswith('Waterfall chart "Flow"')
        assert "Series: Steps" in objects["Waterfall 1"].summary
        assert objects["Chart 1"].summary.startswith('Bar chart "Mixed"')

    def test_fixture_written_by_openpyxl(self):
        drawings = discover_xlsx(FIXTURES / "drawings.xlsx")
        assert drawings.sheet_order == ("Data", "Pics")
        assert [(o.sheet, o.kind, o.kind_index) for o in drawings.objects] == [
            ("Data", "chart", 0),
            ("Data", "chart", 1),
            ("Pics", "picture", 0),
            ("Pics", "picture", 1),
        ]
        # openpyxl writes no cached values: series names degrade gracefully.
        assert drawings.objects[0].summary.splitlines() == [
            'Column chart "Revenue 2025" — data from sheet Data',
            "Axes: Month / EUR",
            "Series: series 1, series 2",
        ]


class TestChartSummaries:
    def test_cell_backed_values_are_not_repeated(self):
        summary = summarize_xlsx_chart(CHART_MIXED.encode(), ["My 'Data'"])
        assert summary.splitlines() == [
            "Bar chart \"Mixed\" — data from sheet My 'Data'",
            "Axes: Units",
            "Series: A, Literal, Ext",
            "Categories: x, y, z",
            "Values of Literal: 7, 8, 9",
            "Values of Ext: 4, 5, 6",
        ]
        assert "101" not in summary, "cell-backed values are already in the table"
        assert not _CELL_RANGE_RE.search(summary)

    def test_unknown_sheets_are_not_reported(self):
        summary = summarize_xlsx_chart(CHART_MIXED.encode(), ["Somewhere else"])
        assert summary.splitlines()[0] == 'Bar chart "Mixed"'

    def test_long_series_are_capped(self):
        info = spreadsheet_drawings._ChartInfo(
            types=["line"],
            series=[
                spreadsheet_drawings._Series(
                    name="s",
                    values=[str(i) for i in range(120)],
                    categories=[f"c{i}" for i in range(120)],
                    cell_backed=False,
                )
            ],
        )
        lines = format_chart_summary(info).splitlines()
        assert lines[-1].endswith(", 49, … (120 points)")

    def test_categories_span_when_nothing_is_listed(self):
        info = spreadsheet_drawings._ChartInfo(
            types=["pie"],
            series=[
                spreadsheet_drawings._Series(categories=["Jan", "Feb", "Mar", "Apr"])
            ],
        )
        assert "Categories: Jan … Apr (4 points)" in format_chart_summary(info)

    def test_ods_fixture_saved_by_libreoffice(self):
        charts = [
            o
            for o in discover_ods(FIXTURES / "drawings.ods").objects
            if o.kind == "chart"
        ]
        assert [c.summary.splitlines() for c in charts] == [
            [
                'Column chart "Revenue 2025" — data from sheet Data',
                "Axes: Month / EUR",
                "Series: Revenue, Cost",
                "Categories: Jan … Jun (6 points)",
            ],
            [
                'Line chart "Cost trend" — data from sheet Data',
                "Series: Cost",
                "Categories: Jan … Jun (6 points)",
            ],
        ]


# ---------------------------------------------------------------------------
#  .ods discovery
# ---------------------------------------------------------------------------


class TestDiscoverOds:
    def test_fixture(self):
        drawings = discover_ods(FIXTURES / "drawings.ods")
        assert drawings.sheet_order == ("Data", "Pics")
        assert [
            (o.sheet, o.kind, o.shape_name, o.kind_index) for o in drawings.objects
        ] == [
            ("Data", "chart", "Chart 1", 0),
            ("Data", "chart", "Chart 2", 1),
            ("Pics", "picture", "Image 1", 0),
            ("Pics", "picture", "Image 2", 1),
        ]
        pictures = [o for o in drawings.objects if o.kind == "picture"]
        assert all(o.media and o.media.startswith("Pictures/") for o in pictures)
        assert pictures[0].sort_key[1] < pictures[1].sort_key[1], "cell rows order them"


# ---------------------------------------------------------------------------
#  Survey (before the fallback decision)
# ---------------------------------------------------------------------------


class TestSurvey:
    def test_xlsx_counts_media_and_chart_bytes(self):
        path = FIXTURES / "drawings.xlsx"
        with zipfile.ZipFile(path) as zf:
            expected = sum(
                i.compress_size
                for i in zf.infolist()
                if i.filename.startswith(("xl/media/", "xl/charts/"))
            )
        survey = survey_drawings(path, ".xlsx")
        assert survey.object_bytes == expected > 0
        assert survey.has_objects and survey.has_charts
        assert survey.drawings is not None and len(survey.drawings.objects) == 4

    def test_big_picture_bytes_cover_most_of_the_file(self):
        path = FIXTURES / "sparse_image_only.xlsx"
        survey = survey_drawings(path, ".xlsx")
        assert survey.object_bytes > 0.9 * path.stat().st_size

    def test_embedded_ole_payload_is_not_an_object(self):
        survey = survey_drawings(FIXTURES / "sparse_ole_object.xlsx", ".xlsx")
        assert survey == DrawingSurvey(drawings=survey.drawings)
        assert not survey.has_objects

    def test_ods(self):
        survey = survey_drawings(FIXTURES / "drawings.ods", ".ods")
        assert survey.has_objects and survey.has_charts and survey.object_bytes > 0

    def test_xls_with_drawings(self):
        survey = survey_drawings(FIXTURES / "drawings.xls", ".xls")
        assert survey.has_objects and survey.has_charts and survey.object_bytes > 0
        assert survey.drawings is None, ".xls objects are only known after conversion"

    def test_xls_without_drawings_needs_no_libreoffice(self):
        """LibreOffice writes empty drawing containers even without any object."""
        survey = survey_drawings(FIXTURES / "plain.xls", ".xls")
        assert survey == DrawingSurvey()
        assert not needs_libreoffice(".xls", survey, True, True)

    def test_never_raises(self, tmp_path):
        garbage = tmp_path / "garbage.xlsx"
        garbage.write_bytes(b"not a zip at all")
        assert survey_drawings(garbage, ".xlsx") == DrawingSurvey()
        assert survey_drawings(garbage, ".xls") == DrawingSurvey()

    def test_needs_libreoffice(self):
        survey = survey_drawings(FIXTURES / "drawings.xlsx", ".xlsx")
        assert needs_libreoffice(
            ".xlsx", survey, render_charts=True, rasterize_metafiles=False
        )
        assert not needs_libreoffice(
            ".xlsx", survey, render_charts=False, rasterize_metafiles=True
        )
        emf = survey_drawings(FIXTURES / "drawings_emf.xlsx", ".xlsx")
        assert needs_libreoffice(
            ".xlsx", emf, render_charts=False, rasterize_metafiles=True
        )
        pictures = survey_drawings(FIXTURES / "pictures_only.xlsx", ".xlsx")
        assert not needs_libreoffice(".xlsx", pictures, True, True)


# ---------------------------------------------------------------------------
#  Decoding
# ---------------------------------------------------------------------------


class TestDecode:
    @pytest.fixture
    def archive(self, tmp_path) -> Path:
        buf = io.BytesIO()
        Image.new("RGBA", (100, 80), (0, 0, 0, 0)).save(buf, "PNG")
        return _zip_with(
            tmp_path / "a.zip",
            {
                "m/logo.png": buf.getvalue(),
                # Most producers store a picture once per use, not once per content.
                "m/logo_copy.png": buf.getvalue(),
                "m/other.png": _png(),
                "m/broken.png": b"not an image",
                "m/vector.emf": b"\x01\x00\x00\x00",
            },
        )

    @pytest.fixture
    def rendered_png(self, tmp_path) -> Path:
        path = tmp_path / "render.png"
        path.write_bytes(_png(size=(300, 200), color=(0, 128, 0)))
        return path

    def test_dedupe_skip_and_render(self, archive, rendered_png):
        drawings = WorkbookDrawings(
            archive,
            ("S1", "S2"),
            (
                _obj("picture", "S1", 0, media="m/logo.png", alt="Logo"),
                _obj("picture", "S1", 1, media="m/broken.png"),
                _obj("picture", "S1", 2, media="m/vector.emf"),  # not rendered
                _obj("chart", "S1", 0, name="Chart 1", summary="Pie chart"),
                _obj(
                    "chart", "S1", 1, name="Chart 2", summary="Line chart"
                ),  # not rendered
                _obj("picture", "S2", 0, media="m/logo.png"),  # same media as S1
                _obj("picture", "S2", 1, media="m/logo_copy.png"),  # same bytes
            ),
        )
        rendered = [RenderedShape("S1", "chart", 0, "Chart 1", rendered_png)]
        result = decode_objects(drawings, rendered, max_images=10)

        (logo, _), (chart, _), (unrendered, _) = [
            (f.image, f.kind) for f in result.figures["S1"]
        ]
        assert unrendered is None, "an unrendered chart keeps its summary only"
        assert logo != chart and set(result.images) == {logo, chart}
        assert all(_NAME_RE.match(name) for name in result.images)
        assert result.contexts == {logo: "Logo", chart: "Pie chart"}
        assert [f.image for f in result.figures["S2"]] == [logo, logo]
        assert result.omitted == {}

    def test_identical_pixels_share_one_image(self, archive, tmp_path):
        """Two charts rendering to the same pixels: one image, not counted twice."""
        first, second = tmp_path / "first.png", tmp_path / "second.png"
        first.write_bytes(_png(color=(0, 128, 0)))
        second.write_bytes(first.read_bytes())
        drawings = WorkbookDrawings(
            archive,
            ("S1",),
            (
                _obj("chart", "S1", 0, name="Chart 1", summary="a"),
                _obj("chart", "S1", 1, name="Chart 2", summary="b"),
                _obj("picture", "S1", 0, media="m/other.png"),
            ),
        )
        rendered = [
            RenderedShape("S1", "chart", 0, "Chart 1", first),
            RenderedShape("S1", "chart", 1, "Chart 2", second),
        ]
        result = decode_objects(drawings, rendered, max_images=2)
        names = [f.image for f in result.figures["S1"]]
        assert names[0] == names[1] != names[2]
        assert set(result.images) == set(names) and result.omitted == {}

    def test_transparency_is_flattened_on_white(self, archive):
        drawings = WorkbookDrawings(
            archive, ("S1",), (_obj("picture", "S1", 0, media="m/logo.png"),)
        )
        (image,) = decode_objects(drawings, None, 10).images.values()
        assert _near(_pixel(image, (0, 0)), (255, 255, 255))

    def test_cap_drops_pictures_and_keeps_chart_summaries(self, archive, rendered_png):
        drawings = WorkbookDrawings(
            archive,
            ("S1",),
            (
                _obj("picture", "S1", 0, media="m/logo.png"),
                _obj("picture", "S1", 1, media="m/other.png"),
                _obj("chart", "S1", 0, name="Chart 1", summary="Pie chart"),
            ),
        )
        rendered = [RenderedShape("S1", "chart", 0, "Chart 1", rendered_png)]
        result = decode_objects(drawings, rendered, max_images=1)
        (picture, chart) = result.figures["S1"]
        assert list(result.images) == [picture.image]
        assert (chart.kind, chart.image) == ("chart", None)
        assert result.omitted == {"S1": 2}

    def test_renderings_match_by_index_when_names_are_ambiguous(
        self, archive, tmp_path
    ):
        first, second = tmp_path / "first.png", tmp_path / "second.png"
        first.write_bytes(_png(color=(255, 0, 0)))
        second.write_bytes(_png(color=(0, 0, 255)))
        drawings = WorkbookDrawings(
            archive,
            ("S1",),
            (
                _obj("chart", "S1", 0, name="Chart 1", summary="a"),
                _obj("chart", "S1", 1, name="Chart 1", summary="b"),
            ),
        )
        rendered = [
            RenderedShape("S1", "chart", 1, "Chart 1", second),
            RenderedShape("S1", "chart", 0, "Chart 1", first),
        ]
        result = decode_objects(drawings, rendered, 10)
        chart_a, chart_b = (result.images[f.image] for f in result.figures["S1"])
        assert _near(_pixel(chart_a, (5, 5)), (255, 0, 0))
        assert _near(_pixel(chart_b, (5, 5)), (0, 0, 255))

    def test_pixel_limit(self, monkeypatch):
        monkeypatch.setattr(spreadsheet_drawings, "_MAX_PIXELS", 1000)
        assert to_rgb(_png(size=(100, 100))) is None
        assert to_rgb(_png(size=(10, 10))) is not None


# ---------------------------------------------------------------------------
#  Markdown: figures, sheet bodies, table of contents, sparse rule
# ---------------------------------------------------------------------------


class TestMarkdown:
    def test_render_figures(self):
        figures = [
            Figure("picture", "imgs/p.jpg", 'Say "hi" <b>', ""),
            Figure("picture", "imgs/too_small.jpg", "", ""),
            Figure("chart", "imgs/c.jpg", "", "Pie chart\nSeries: A"),
            Figure("chart", None, "", "Line chart"),
        ]
        md = render_figures(
            figures,
            kept_images={"imgs/p.jpg", "imgs/c.jpg"},
            descriptions={"imgs/p.jpg": "A greeting.\r\nTwo lines."},
            omitted=3,
        )
        assert md == (
            "<figure>\n"
            '<img src="imgs/p.jpg" alt="Say &quot;hi&quot; &lt;b&gt;">\n'
            "<figcaption>  \nA greeting.\nTwo lines.  \n</figcaption>\n"
            "</figure>\n\n"
            "<figure>\n"
            '<img src="imgs/c.jpg">\n'
            "<chart>  \nPie chart  \nSeries: A  \n</chart>\n"
            "</figure>\n\n"
            "<figure>\n"
            "<chart>  \nLine chart  \n</chart>\n"
            "</figure>\n\n"
            "> 3 more image(s) on this sheet were not extracted: "
            "the per-document image limit was reached."
        )

    def test_merge_follows_the_workbook_order(self):
        bodies = merge_sheet_bodies(
            tables=[("Data", "|a|\n|-|\n|1|"), ("Notes", "")],
            figures={"Charts": "<figure>c</figure>", "Data": "<figure>d</figure>"},
            sheet_order=("Data", "Charts", "Notes"),
        )
        assert bodies == [
            ("Data", "|a|\n|-|\n|1|\n\n<figure>d</figure>"),
            ("Charts", "<figure>c</figure>"),
            ("Notes", ""),
        ]

    def test_toc_stays_exact_with_multi_line_figures(self):
        """Descriptions may contain any line separator; ToC numbers count only "\\n"."""
        weird = "one two\rthree\x0bfour"
        figures = render_figures(
            [Figure("picture", "imgs/p.jpg", "", "")],
            {"imgs/p.jpg"},
            {"imgs/p.jpg": weird},
        )
        md, anchors = assemble_sections(
            merge_sheet_bodies(
                [("First", "|a|\n|-|\n|1|"), ("Second", "|b|\n|-|\n|2|")],
                {"First": figures},
            )
        )
        doc = build_spreadsheet_document(
            ".xlsx", SpreadsheetMode.PANDAS, "llm", pandas_md=md, pandas_anchors=anchors
        ).markdown
        lines = doc.splitlines()
        toc = [line for line in lines if re.match(r"^\|(First|Second)\|\d+\|$", line)]
        for row in toc:
            name, number = row.strip("|").split("|")
            assert lines[int(number) - 1] == f"## {name}"

    def test_is_sparse_ignores_natively_extracted_objects(self):
        mb = 1024 * 1024
        # 20 kB of Markdown from a 5.02 MB file whose 5 MB are a picture.
        kwargs = dict(min_input_mb=0.25, min_output_ratio=0.01)
        assert is_sparse(20_000, 5 * mb + 20_000, **kwargs)
        assert not is_sparse(20_000, 5 * mb + 20_000, object_bytes=5 * mb, **kwargs)
        # A text box weighs on the file but is not extracted: still sparse.
        assert is_sparse(200, mb, object_bytes=10_000, **kwargs)
