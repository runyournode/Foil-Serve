"""Pictures and charts embedded in spreadsheets (.xlsx / .ods / .xls).

pandas reads cell values only: everything drawn on top of the grid is lost. This
module recovers pictures and charts, natively whenever the container allows it:

- ``.xlsx`` and ``.ods`` are ZIP archives of XML. Pictures are stored as-is, and
  a chart's definition (type, title, series, cached values) is plain XML.
- ``.xls`` keeps its drawings in binary OfficeArt records that nothing in our
  dependencies decodes. It goes through LibreOffice (→ .xlsx) — but only when a
  scan of its BIFF record headers shows that it has drawings at all.

The work is split by cost, so the caller can settle the OCR fallback before paying
for anything heavy:

1. ``survey_drawings()`` — byte counts from the ZIP directory / BIFF headers, and
   for ZIP formats the objects themselves, read from the (small) drawing XML.
2. ``render_with_libreoffice()`` — only for what has no bitmap in the file (charts,
   EMF/WMF pictures) and for .xls.
3. ``decode_objects()`` — picture bytes and rendered PNGs → RGB PIL images.

Cell coordinates are read only to order objects within their sheet; they are never
written to the Markdown, where row and column numbers do not survive.
"""

import io
import logging
import posixpath
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from PIL import Image as PILImage
from PIL.Image import Image
from xlrd.compdoc import CompDoc

from libreoffice import LibreOfficeServer, RenderedShape

logger = logging.getLogger(__name__)

ObjectKind = Literal["picture", "chart"]

# Formats Pillow decodes; anything else (EMF, WMF, SVM, PICT…) needs LibreOffice.
RASTER_EXTS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".jpe",
        ".jfif",
        ".gif",
        ".bmp",
        ".dib",
        ".tif",
        ".tiff",
        ".webp",
    }
)

# Pictures larger than this are not decoded at all (decompression-bomb guard, well
# below Pillow's own limit), and decoded ones are shrunk to this longest side.
_MAX_PIXELS = 50_000_000
_MAX_SIDE_PX = 4096

# Chart summaries: values listed per series, and characters kept per label.
_MAX_LISTED_POINTS = 50
_MAX_LABEL_CHARS = 40

# Alt texts that producers write by default and that carry no information.
_GENERIC_ALT_RE = re.compile(
    r"^\s*(picture|image|img|chart|graphic|graphique|imagen|bild|immagine|grafico)"
    r"\s*\d*\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
#  Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SheetObject:
    """A picture or chart found in the file.

    ``kind_index`` is the position among objects of the same kind on the sheet, in
    drawing order — it names the image and backs up the shape name when matching
    LibreOffice renderings. ``sort_key`` orders objects top-to-bottom, left-to-right.
    """

    kind: ObjectKind
    sheet: str
    shape_name: str
    kind_index: int
    sort_key: tuple[int, int, int, int]
    media: str | None = None  # archive path of a picture's bytes
    alt_text: str = ""
    summary: str = ""  # charts: type, title, series…


@dataclass(frozen=True, slots=True)
class WorkbookDrawings:
    """Every object of a workbook, grouped by sheet in workbook order."""

    source: Path  # archive the `media` paths refer to
    sheet_order: tuple[str, ...]
    objects: tuple[SheetObject, ...]

    def needs_rendering(self) -> tuple[bool, bool]:
        """(has charts, has pictures Pillow cannot decode)."""
        charts = any(o.kind == "chart" for o in self.objects)
        metafiles = any(
            o.kind == "picture"
            and o.media is not None
            and PurePosixPath(o.media).suffix.lower() not in RASTER_EXTS
            for o in self.objects
        )
        return charts, metafiles


@dataclass(frozen=True, slots=True)
class DrawingSurvey:
    """What the cheap pass learned — enough to decide on the OCR fallback.

    ``object_bytes`` is the on-disk size of the objects handled natively: it is
    excluded from the file size when judging whether pandas missed content.
    ``drawings`` is None for .xls, whose objects are only known after conversion.
    """

    object_bytes: int = 0
    has_objects: bool = False
    has_charts: bool = False
    drawings: WorkbookDrawings | None = None


@dataclass(frozen=True, slots=True)
class Figure:
    """One object as it will appear under its sheet."""

    kind: ObjectKind
    image: str | None  # key in the response images; None when there is none to show
    alt_text: str
    summary: str


@dataclass(frozen=True, slots=True)
class ExtractedObjects:
    """Decoded objects, ready for the VLM and the Markdown."""

    sheet_order: tuple[str, ...]
    figures: dict[str, list[Figure]]
    images: dict[str, Image]
    # image name → VLM prompt context (chart summary / alt text)
    contexts: dict[str, str]
    # sheet → images dropped over the cap
    omitted: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
#  XML helpers (namespace-agnostic: transitional and strict OOXML both parse)
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _children(el: ET.Element, name: str) -> Iterator[ET.Element]:
    return (child for child in el if _local(child.tag) == name)


def _child(el: ET.Element | None, *path: str) -> ET.Element | None:
    for name in path:
        if el is None:
            return None
        el = next(_children(el, name), None)
    return el


def _descendants(el: ET.Element, name: str) -> Iterator[ET.Element]:
    return (d for d in el.iter() if _local(d.tag) == name)


def _ns_attr(el: ET.Element, name: str) -> str | None:
    """Value of a namespaced attribute by local name (e.g. r:id, r:embed)."""
    for key, value in el.attrib.items():
        if key.startswith("{") and _local(key) == name:
            return value
    return None


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _clean_alt(*candidates: str | None) -> str:
    for candidate in candidates:
        text = " ".join((candidate or "").split())
        if text and not _GENERIC_ALT_RE.match(text):
            return text
    return ""


# ---------------------------------------------------------------------------
#  Chart summaries (shared by .xlsx and .ods)
# ---------------------------------------------------------------------------


@dataclass
class _Series:
    name: str = ""
    categories: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    cell_backed: bool = True


@dataclass
class _ChartInfo:
    types: list[str] = field(default_factory=list)
    title: str = ""
    axis_titles: list[str] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    series: list[_Series] = field(default_factory=list)


def _label(value: str) -> str:
    value = " ".join(value.split())
    if len(value) > _MAX_LABEL_CHARS:
        value = value[: _MAX_LABEL_CHARS - 1] + "…"
    return value


def _listing(items: Sequence[str]) -> str:
    shown = ", ".join(_label(v) for v in items[:_MAX_LISTED_POINTS])
    if len(items) > _MAX_LISTED_POINTS:
        shown += f", … ({len(items)} points)"
    return shown


def _span(items: Sequence[str]) -> str:
    if len(items) <= 3:
        return ", ".join(_label(v) for v in items)
    return f"{_label(items[0])} … {_label(items[-1])} ({len(items)} points)"


def format_chart_summary(info: _ChartInfo) -> str:
    """Plain-text description of a chart, without any cell reference.

    Values are listed only for series that are not backed by the workbook's cells
    (literals, links to other workbooks): cell-backed values are already in the
    sheet's table, and repeating them would only duplicate data.
    """
    kinds = " + ".join(dict.fromkeys(info.types)) or "unknown"
    head = f"{kinds[0].upper()}{kinds[1:]} chart"
    if info.title:
        head += f' "{_label(info.title)}"'
    if info.sheets:
        plural = "s" if len(info.sheets) > 1 else ""
        head += f" — data from sheet{plural} {', '.join(info.sheets)}"
    lines = [head]
    if info.axis_titles:
        lines.append(f"Axes: {' / '.join(_label(t) for t in info.axis_titles)}")
    names = [s.name or f"series {i}" for i, s in enumerate(info.series, start=1)]
    if names:
        lines.append(f"Series: {', '.join(_label(n) for n in names)}")
    literal = [s for s in info.series if not s.cell_backed and s.values]
    categories = next((s.categories for s in info.series if s.categories), [])
    if categories:
        shown = _listing(categories) if literal else _span(categories)
        lines.append(f"Categories: {shown}")
    for s, name in zip(info.series, names):
        if not s.cell_backed and s.values:
            lines.append(f"Values of {_label(name)}: {_listing(s.values)}")
    return "\n".join(lines)


# Excel range references: 'My ''Sheet'''!$A$1, Sheet1!A1:B2, [1]Sheet1!A1
_XLSX_REF_RE = re.compile(r"(?:'((?:[^']|'')+)'|([^'!(),\s]+))!")
# ODF range addresses: Sheet1.A1:Sheet1.B2, 'My Sheet'.A1, $Sheet1.$A$1
_ODS_REF_RE = re.compile(r"(?:'((?:[^']|'')+)'|\$?([^.\s':]+))\.\$?[A-Za-z]+\$?\d+")


def _ref_sheets(formula: str, pattern: re.Pattern[str]) -> tuple[list[str], bool]:
    """(sheets referenced, whether any reference points to another workbook)."""
    sheets: list[str] = []
    external = False
    for match in pattern.finditer(formula):
        name = (
            match.group(1).replace("''", "'")
            if match.group(1) is not None
            else match.group(2)
        )
        if name.startswith("[") or "#" in name:
            external = True
            continue
        sheets.append(name)
    if "#" in formula and "file:" in formula:  # ODF link to another document
        external = True
    return sheets, external


_XLSX_CHART_TYPES = {
    "barChart": "bar",
    "bar3DChart": "3-D bar",
    "lineChart": "line",
    "line3DChart": "3-D line",
    "pieChart": "pie",
    "pie3DChart": "3-D pie",
    "ofPieChart": "pie-of-pie",
    "doughnutChart": "doughnut",
    "areaChart": "area",
    "area3DChart": "3-D area",
    "scatterChart": "scatter",
    "radarChart": "radar",
    "bubbleChart": "bubble",
    "stockChart": "stock",
    "surfaceChart": "surface",
    "surface3DChart": "3-D surface",
}


def _xlsx_points(data: ET.Element | None) -> list[str]:
    """Cached or literal points of a c:cat / c:val / c:tx data source."""
    if data is None:
        return []
    for container in ("strRef", "numRef", "multiLvlStrRef"):
        ref = _child(data, container)
        if ref is not None:
            cache = next(
                (
                    c
                    for c in ref
                    if _local(c.tag) in ("strCache", "numCache", "multiLvlStrCache")
                ),
                None,
            )
            if cache is None:
                return []
            level = _child(cache, "lvl")  # multi-level categories: innermost level
            return _pts(level if level is not None else cache)
    for literal in ("strLit", "numLit"):
        lit = _child(data, literal)
        if lit is not None:
            return _pts(lit)
    v = _child(data, "v")
    return [v.text or ""] if v is not None else []


def _pts(container: ET.Element) -> list[str]:
    points = []
    for pt in _children(container, "pt"):
        try:
            idx = int(pt.get("idx", len(points)))
        except ValueError:
            idx = len(points)
        points.append((idx, _text(_child(pt, "v"))))
    return [value for _, value in sorted(points)]


def _xlsx_formula(data: ET.Element | None) -> str | None:
    if data is None:
        return None
    for container in ("strRef", "numRef", "multiLvlStrRef"):
        f = _child(data, container, "f")
        if f is not None:
            return f.text or ""
    return None


def _xlsx_rich_title(title: ET.Element | None) -> str:
    if title is None:
        return ""
    rich = _child(title, "tx", "rich")
    if rich is not None:
        return _text(rich)
    return next(iter(_xlsx_points(_child(title, "tx"))), "")


def summarize_xlsx_chart(xml: bytes, known_sheets: Sequence[str]) -> str:
    """Summary of a DrawingML chart part (xl/charts/chartN.xml), or a chartex part."""
    root = ET.fromstring(xml)
    info = _ChartInfo()
    if _local(root.tag) == "chartSpace" and _child(root, "chartData") is not None:
        return _summarize_chartex(root)

    chart = _child(root, "chart")
    if chart is None:
        return format_chart_summary(info)
    info.title = _xlsx_rich_title(_child(chart, "title"))
    plot_area = _child(chart, "plotArea")
    sheets: list[str] = []
    if plot_area is not None:
        for plot in plot_area:
            name = _local(plot.tag)
            if name in _XLSX_CHART_TYPES:
                kind = _XLSX_CHART_TYPES[name]
                bar_dir = _child(plot, "barDir")
                if bar_dir is not None and bar_dir.get("val") == "col":
                    kind = kind.replace("bar", "column")
                info.types.append(kind)
                for ser in _children(plot, "ser"):
                    info.series.append(_xlsx_series(ser, sheets))
            elif name in ("catAx", "valAx", "dateAx", "serAx"):
                axis_title = _xlsx_rich_title(_child(plot, "title"))
                if axis_title:
                    info.axis_titles.append(axis_title)
    info.sheets = [s for s in dict.fromkeys(sheets) if s in known_sheets]
    return format_chart_summary(info)


def _xlsx_series(ser: ET.Element, sheets: list[str]) -> _Series:
    series = _Series()
    series.name = next(iter(_xlsx_points(_child(ser, "tx"))), "")
    categories = _child(ser, "cat")
    if categories is None:
        categories = _child(ser, "xVal")
    values = _child(ser, "val")
    if values is None:
        values = _child(ser, "yVal")
    series.categories = _xlsx_points(categories)
    series.values = _xlsx_points(values)
    formula = _xlsx_formula(values)
    if formula:
        refs, external = _ref_sheets(formula, _XLSX_REF_RE)
        sheets.extend(refs)
        series.cell_backed = not external
    else:
        series.cell_backed = False  # literal values, or no source at all
    return series


_CHARTEX_TYPES = {
    "waterfall": "waterfall",
    "treemap": "treemap",
    "sunburst": "sunburst",
    "boxWhisker": "box & whisker",
    "clusteredColumn": "histogram",
    "paretoLine": "pareto",
    "funnel": "funnel",
    "regionMap": "map",
}


def _chartex_text(el: ET.Element | None) -> str:
    """Displayed text of a cx:tx — its cx:v / a:t values, never its cx:f formula."""
    if el is None:
        return ""
    return " ".join(
        " ".join((d.text or "").split())
        for d in el.iter()
        if _local(d.tag) in ("v", "t") and d.text
    )


def _summarize_chartex(root: ET.Element) -> str:
    """Best-effort summary of an Excel 2016+ chart (cx: namespace)."""
    info = _ChartInfo()
    chart = _child(root, "chart")
    if chart is not None:
        info.title = _chartex_text(_child(chart, "title", "tx"))
        for series in _descendants(chart, "series"):
            kind = _CHARTEX_TYPES.get(series.get("layoutId", ""), "")
            if kind:
                info.types.append(kind)
            info.series.append(_Series(name=_chartex_text(_child(series, "tx"))))
    return format_chart_summary(info)


_ODS_CHART_TYPES = {
    "bar": "bar",
    "line": "line",
    "circle": "pie",
    "ring": "doughnut",
    "area": "area",
    "scatter": "scatter",
    "radar": "radar",
    "filled-radar": "filled radar",
    "bubble": "bubble",
    "stock": "stock",
    "surface": "surface",
    "gantt": "gantt",
}

_NS_CHART = "urn:oasis:names:tc:opendocument:xmlns:chart:1.0"
_NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_NS_STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
_NS_XLINK = "http://www.w3.org/1999/xlink"


def _ods_cell_text(cell: ET.Element) -> str:
    """Paragraph text of a chart-table cell — its draw:g/svg:desc holds the source
    cell range, which must not leak into the summary."""
    return " ".join(_text(p) for p in _children(cell, "p")).strip()


def _ods_chart_type(value: str | None) -> str:
    return _ODS_CHART_TYPES.get((value or "").partition(":")[2], "")


def summarize_ods_chart(xml: bytes, known_sheets: Sequence[str]) -> str:
    """Summary of an ODF chart object (Object N/content.xml)."""
    root = ET.fromstring(xml)
    info = _ChartInfo()
    # Exact tag: office:chart (the body element) shares the local name.
    chart = next(root.iter(f"{{{_NS_CHART}}}chart"), None)
    if chart is None:
        return format_chart_summary(info)

    # Horizontal bars are a style property of the plot area.
    horizontal_styles = {
        style.get(f"{{{_NS_STYLE}}}name")
        for style in _descendants(root, "style")
        if (props := _child(style, "chart-properties")) is not None
        and props.get(f"{{{_NS_CHART}}}vertical") == "true"
    }
    plot_area = _child(chart, "plot-area")
    horizontal = (
        plot_area is not None
        and plot_area.get(f"{{{_NS_CHART}}}style-name") in horizontal_styles
    )

    def kind_name(value: str | None) -> str:
        kind = _ods_chart_type(value)
        return "column" if kind == "bar" and not horizontal else kind

    info.title = _text(_child(chart, "title"))
    main_kind = kind_name(chart.get(f"{{{_NS_CHART}}}class"))

    # Local table: header row = series names, first column = categories.
    header: list[str] = []
    rows: list[list[str]] = []
    local_table = next(_descendants(chart, "table"), None)
    if local_table is not None:
        header_rows = _child(local_table, "table-header-rows")
        if header_rows is not None:
            first = _child(header_rows, "table-row")
            if first is not None:
                header = [_ods_cell_text(c) for c in _children(first, "table-cell")]
        body = _child(local_table, "table-rows")
        if body is not None:
            rows = [
                [_ods_cell_text(c) for c in _children(row, "table-cell")]
                for row in _children(body, "table-row")
            ]
    categories = [row[0] for row in rows if row]

    sheets: list[str] = []
    if plot_area is not None:
        for axis in _children(plot_area, "axis"):
            axis_title = _text(_child(axis, "title"))
            if axis_title:
                info.axis_titles.append(axis_title)
        for column, ser in enumerate(_children(plot_area, "series"), start=1):
            info.types.append(kind_name(ser.get(f"{{{_NS_CHART}}}class")) or main_kind)
            address = ser.get(f"{{{_NS_CHART}}}values-cell-range-address") or ""
            series = _Series(
                name=header[column] if column < len(header) else "",
                categories=categories,
                values=[row[column] for row in rows if column < len(row)],
            )
            if address:
                refs, external = _ref_sheets(address, _ODS_REF_RE)
                sheets.extend(refs)
                series.cell_backed = not external
            else:
                series.cell_backed = False
            info.series.append(series)
    if not info.types and main_kind:
        info.types.append(main_kind)
    info.sheets = [s for s in dict.fromkeys(sheets) if s in known_sheets]
    return format_chart_summary(info)


# ---------------------------------------------------------------------------
#  .xlsx discovery
# ---------------------------------------------------------------------------


class _Archive:
    """Case-insensitive access to OPC parts (part names are case-insensitive)."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self._names = {info.filename.lower(): info.filename for info in zf.infolist()}

    def name(self, part: str) -> str | None:
        return self._names.get(part.lower())

    def read(self, part: str) -> bytes | None:
        name = self.name(part)
        return self.zf.read(name) if name is not None else None

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """Relationships of a part: rId → (type, resolved target part)."""
        folder, _, filename = part.rpartition("/")
        data = self.read(
            f"{folder}/_rels/{filename}.rels" if folder else f"_rels/{filename}.rels"
        )
        if data is None:
            return {}
        result: dict[str, tuple[str, str]] = {}
        for rel in ET.fromstring(data):
            if _local(rel.tag) != "Relationship" or rel.get("TargetMode") == "External":
                continue
            rel_id, target = rel.get("Id"), rel.get("Target")
            if not rel_id or not target:
                continue
            if target.startswith("/"):
                resolved = target.lstrip("/")
            else:
                resolved = posixpath.normpath(posixpath.join(folder, target))
            if resolved.startswith("../") or resolved == "..":
                continue  # would escape the package
            result[rel_id] = (rel.get("Type", ""), resolved)
        return result


def _anchor_key(anchor: ET.Element, seq: int) -> tuple[int, int, int, int]:
    start = _child(anchor, "from")
    if start is not None:
        try:
            row = int(_text(_child(start, "row")) or 0)
            col = int(_text(_child(start, "col")) or 0)
            return (0, row, col, seq)
        except ValueError:
            pass
    return (1, 0, 0, seq)  # absolute anchor: after the cell-anchored objects


def _drawing_objects(
    anchor_content: ET.Element,
) -> Iterator[tuple[str, ET.Element]]:
    """(kind, element) for pictures and charts inside an anchor, groups flattened.

    mc:AlternateContent: the first branch holding an object wins (Choice before
    Fallback), which picks a chartex chart over its "unsupported" placeholder shape.
    """
    for el in anchor_content:
        name = _local(el.tag)
        if name == "pic":
            yield "picture", el
        elif name == "graphicFrame":
            yield "chart", el
        elif name == "grpSp":
            yield from _drawing_objects(el)
        elif name == "AlternateContent":
            for branch in el:
                found = list(_drawing_objects(branch))
                if found:
                    yield from found
                    break


def _xlsx_sheet_objects(
    archive: _Archive,
    sheet: str,
    drawing_part: str,
    known_sheets: Sequence[str],
) -> list[SheetObject]:
    data = archive.read(drawing_part)
    if data is None:
        return []
    rels = archive.rels(drawing_part)
    objects: list[SheetObject] = []
    counters: Counter[str] = Counter()
    seq = 0

    for anchor in ET.fromstring(data):
        anchors = [anchor]
        if _local(anchor.tag) == "AlternateContent":
            branch = next(iter(anchor), None)
            anchors = list(branch) if branch is not None else []
        for real_anchor in anchors:
            if _local(real_anchor.tag) not in (
                "twoCellAnchor",
                "oneCellAnchor",
                "absoluteAnchor",
            ):
                continue
            for kind, el in _drawing_objects(real_anchor):
                props = next(_descendants(el, "cNvPr"), None)
                if props is not None and props.get("hidden") in ("1", "true"):
                    continue
                shape_name = props.get("name", "") if props is not None else ""
                alt = _clean_alt(
                    props.get("descr") if props is not None else None,
                    props.get("title") if props is not None else None,
                )
                seq += 1
                key = _anchor_key(real_anchor, seq)

                if kind == "picture":
                    blip = next(_descendants(el, "blip"), None)
                    rel_id = _ns_attr(blip, "embed") if blip is not None else None
                    target = rels.get(rel_id or "")
                    media = archive.name(target[1]) if target else None
                    if media is None:
                        continue  # linked (external) or broken picture
                    objects.append(
                        SheetObject(
                            kind="picture",
                            sheet=sheet,
                            shape_name=shape_name,
                            kind_index=counters["picture"],
                            sort_key=key,
                            media=media,
                            alt_text=alt,
                        )
                    )
                    counters["picture"] += 1
                else:
                    ref = next(
                        (d for d in _descendants(el, "chart") if _ns_attr(d, "id")),
                        None,
                    )
                    if ref is None:
                        continue  # SmartArt, OLE object… out of scope
                    target = rels.get(_ns_attr(ref, "id") or "")
                    chart_xml = archive.read(target[1]) if target else None
                    if chart_xml is None:
                        continue
                    try:
                        summary = summarize_xlsx_chart(chart_xml, known_sheets)
                    except ET.ParseError as e:
                        logger.warning(
                            f"Unreadable chart part '{target[1] if target else ''}': {e!r}"
                        )
                        summary = format_chart_summary(_ChartInfo())
                    objects.append(
                        SheetObject(
                            kind="chart",
                            sheet=sheet,
                            shape_name=shape_name,
                            kind_index=counters["chart"],
                            sort_key=key,
                            alt_text=alt,
                            summary=summary,
                        )
                    )
                    counters["chart"] += 1
    return objects


def discover_xlsx(path: Path) -> WorkbookDrawings:
    """Pictures and charts of an .xlsx file, from its drawing parts."""
    with zipfile.ZipFile(path) as zf:
        archive = _Archive(zf)
        workbook_part = next(
            (
                target
                for rel_type, target in archive.rels("").values()
                if rel_type.endswith("/officeDocument")
            ),
            "xl/workbook.xml",
        )
        workbook_xml = archive.read(workbook_part)
        if workbook_xml is None:
            return WorkbookDrawings(path, (), ())
        workbook_rels = archive.rels(workbook_part)
        sheets = [
            (el.get("name", ""), workbook_rels.get(_ns_attr(el, "id") or ""))
            for el in _descendants(ET.fromstring(workbook_xml), "sheet")
        ]
        sheet_order = tuple(name for name, _ in sheets)

        objects: list[SheetObject] = []
        for name, rel in sheets:
            if rel is None:
                continue
            drawing = next(
                (
                    target
                    for rel_type, target in archive.rels(rel[1]).values()
                    if rel_type.endswith("/drawing")
                ),
                None,
            )
            if drawing is None:
                continue
            sheet_objects = _xlsx_sheet_objects(archive, name, drawing, sheet_order)
            objects.extend(sorted(sheet_objects, key=lambda o: o.sort_key))
    return WorkbookDrawings(path, sheet_order, tuple(objects))


# ---------------------------------------------------------------------------
#  .ods discovery
# ---------------------------------------------------------------------------


def _repeat(el: ET.Element, attr: str) -> int:
    try:
        return max(1, int(el.get(f"{{{_NS_TABLE}}}{attr}", "1")))
    except ValueError:
        return 1


def _href(el: ET.Element) -> str:
    return (el.get(f"{{{_NS_XLINK}}}href") or "").removeprefix("./")


def discover_ods(path: Path) -> WorkbookDrawings:
    """Pictures and charts of an .ods file, streaming its content.xml."""
    with zipfile.ZipFile(path) as zf:
        archive = _Archive(zf)
        content = archive.name("content.xml")
        if content is None:
            return WorkbookDrawings(path, (), ())

        sheet_order: list[str] = []
        found: list[tuple[str, int, tuple[int, int, int, int], ET.Element]] = []
        sheet = ""
        row = col = 0
        in_cell = 0
        frame_depth = 0
        seq = 0

        with zf.open(content) as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                name = _local(el.tag)
                if event == "start":
                    if name == "table" and el.tag == f"{{{_NS_TABLE}}}table":
                        sheet = el.get(f"{{{_NS_TABLE}}}name", "")
                        sheet_order.append(sheet)
                        row = 0
                    elif name == "table-row":
                        col = 0
                    elif name in ("table-cell", "covered-table-cell"):
                        in_cell += 1
                    elif name == "frame":
                        frame_depth += 1
                    continue

                if name == "frame":
                    frame_depth -= 1
                    if frame_depth == 0 and sheet_order:
                        seq += 1
                        key = (0, row, col, seq) if in_cell else (1, 0, 0, seq)
                        try:
                            z_index = int(el.get(f"{{{_NS_DRAW}}}z-index", seq))
                        except ValueError:
                            z_index = seq
                        found.append((sheet, z_index, key, el))
                elif name in ("table-cell", "covered-table-cell"):
                    in_cell -= 1
                    col += _repeat(el, "number-columns-repeated")
                    if not frame_depth:
                        el.clear()
                elif name == "table-row":
                    row += _repeat(el, "number-rows-repeated")
                    el.clear()

        objects = _ods_objects(archive, found, tuple(sheet_order))
    return WorkbookDrawings(path, tuple(sheet_order), tuple(objects))


def _ods_objects(
    archive: _Archive,
    found: list[tuple[str, int, tuple[int, int, int, int], ET.Element]],
    sheet_order: tuple[str, ...],
) -> list[SheetObject]:
    candidates: list[tuple[str, int, tuple[int, int, int, int], SheetObject]] = []
    for sheet, z_index, key, frame in found:
        shape_name = frame.get(f"{{{_NS_DRAW}}}name", "")
        alt = _clean_alt(_text(_child(frame, "desc")), _text(_child(frame, "title")))
        obj = _child(frame, "object")
        image = _child(frame, "image")
        if _child(frame, "object-ole") is not None:
            continue  # OLE object: its draw:image is only a preview, out of scope
        if obj is not None:
            chart_xml = archive.read(f"{_href(obj).rstrip('/')}/content.xml")
            if chart_xml is None or b"chart:chart" not in chart_xml:
                continue  # embedded OLE document, out of scope
            try:
                summary = summarize_ods_chart(chart_xml, sheet_order)
            except ET.ParseError as e:
                logger.warning(f"Unreadable ODF chart object '{_href(obj)}': {e!r}")
                summary = format_chart_summary(_ChartInfo())
            kind: ObjectKind = "chart"
            media = None
        elif image is not None:
            media = archive.name(_href(image))
            if media is None:
                continue  # linked (external) or broken picture
            kind, summary = "picture", ""
        else:
            continue
        candidates.append(
            (
                sheet,
                z_index,
                key,
                SheetObject(kind, sheet, shape_name, 0, key, media, alt, summary),
            )
        )

    # kind_index follows LibreOffice's drawing order, which is the z-index order.
    objects: list[SheetObject] = []
    for sheet in sheet_order:
        on_sheet = [c for c in candidates if c[0] == sheet]
        counters: Counter[str] = Counter()
        indexed = []
        for _, _, key, o in sorted(on_sheet, key=lambda c: (c[1], c[2][3])):
            indexed.append(
                SheetObject(
                    o.kind,
                    o.sheet,
                    o.shape_name,
                    counters[o.kind],
                    key,
                    o.media,
                    o.alt_text,
                    o.summary,
                )
            )
            counters[o.kind] += 1
        objects.extend(sorted(indexed, key=lambda o: o.sort_key))
    return objects


# ---------------------------------------------------------------------------
#  Cheap survey (before the OCR fallback decision)
# ---------------------------------------------------------------------------

_BIFF_BOF = 0x0809
_BIFF_CONTINUE = 0x003C
_BIFF_FILEPASS = 0x002F
_BIFF_OBJ = 0x005D
_BIFF_MSODRAWINGGROUP = 0x00EB
_BIFF_CHART_SUBSTREAM = 0x0020
_BIFF_FT_CMO = 0x0015  # first sub-record of an OBJ record: its object type
_BIFF_OT_CHART = 0x0005
_BIFF_OT_PICTURE = 0x0008


def _xls_survey(path: Path) -> DrawingSurvey:
    """Scan the BIFF records of an .xls workbook stream.

    Pictures and charts are OBJ records typed chart or picture. Producers also
    write empty drawing containers, and comments are OBJ records too — neither
    counts. In an RC4-encrypted workbook the OBJ payload is unreadable, but record
    headers are not encrypted: there, any OBJ record counts.
    """
    compdoc = CompDoc(path.read_bytes(), logfile=io.StringIO())
    stream = compdoc.get_named_stream("Workbook") or compdoc.get_named_stream("Book")
    if not stream:
        return DrawingSurvey()
    object_bytes = 0
    encrypted = has_objects = has_charts = False
    previous = 0
    pos = 0
    while pos + 4 <= len(stream):
        record, length = struct.unpack_from("<HH", stream, pos)
        if record == _BIFF_MSODRAWINGGROUP or (
            record == _BIFF_CONTINUE and previous == _BIFF_MSODRAWINGGROUP
        ):
            object_bytes += length
            record = _BIFF_MSODRAWINGGROUP  # a Continue extends the group record
        elif record == _BIFF_FILEPASS:
            encrypted = True
        elif record == _BIFF_OBJ and length >= 6:
            if encrypted:
                has_objects = True
            else:
                sub_record, _, object_type = struct.unpack_from("<HHH", stream, pos + 4)
                if sub_record == _BIFF_FT_CMO and object_type in (
                    _BIFF_OT_CHART,
                    _BIFF_OT_PICTURE,
                ):
                    has_objects = True
                    has_charts = has_charts or object_type == _BIFF_OT_CHART
        elif record == _BIFF_BOF and length >= 4:
            (substream,) = struct.unpack_from("<H", stream, pos + 6)
            if substream == _BIFF_CHART_SUBSTREAM:
                has_charts = True
        previous = record
        pos += 4 + length
    return DrawingSurvey(
        object_bytes=object_bytes if has_objects else 0,
        has_objects=has_objects or has_charts,
        has_charts=has_charts,
    )


def _zip_object_bytes(path: Path, prefixes: tuple[str, ...]) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(
            info.compress_size
            for info in zf.infolist()
            if info.filename.lower().startswith(prefixes)
        )


def survey_drawings(path: Path, mime: str) -> DrawingSurvey:
    """Cheap pass over a spreadsheet's pictures and charts. Never raises.

    Nothing is decompressed but the drawing / chart XML: byte counts come from the
    ZIP directory or the BIFF record headers.
    """
    try:
        if mime == ".xls":
            return _xls_survey(path)
        if mime == ".xlsx":
            drawings = discover_xlsx(path)
            object_bytes = _zip_object_bytes(path, ("xl/media/", "xl/charts/"))
        elif mime == ".ods":
            drawings = discover_ods(path)
            object_bytes = _zip_object_bytes(
                path, ("pictures/", "objectreplacements/", "object ")
            )
        else:
            return DrawingSurvey()
    except Exception as e:
        logger.warning(
            f"Could not read the drawings of the spreadsheet ({mime}): {e!r} — "
            f"pictures and charts are ignored"
        )
        return DrawingSurvey()
    return DrawingSurvey(
        object_bytes=object_bytes,
        has_objects=bool(drawings.objects),
        has_charts=any(o.kind == "chart" for o in drawings.objects),
        drawings=drawings,
    )


# ---------------------------------------------------------------------------
#  LibreOffice: .xls conversion, chart and metafile rendering
# ---------------------------------------------------------------------------


def needs_libreoffice(
    mime: str, survey: DrawingSurvey, render_charts: bool, rasterize_metafiles: bool
) -> bool:
    """Whether `render_with_libreoffice` has anything to do for this file."""
    if not survey.has_objects:
        return False
    if mime == ".xls":
        return True  # its drawings are only readable once converted
    if survey.drawings is None:
        return False
    has_charts, has_metafiles = survey.drawings.needs_rendering()
    return (render_charts and has_charts) or (rasterize_metafiles and has_metafiles)


def render_with_libreoffice(
    path: Path,
    mime: str,
    survey: DrawingSurvey,
    lo_server: LibreOfficeServer,
    work_dir: Path,
    render_charts: bool,
    rasterize_metafiles: bool,
) -> tuple[WorkbookDrawings | None, list[RenderedShape] | None]:
    """Run LibreOffice only where the file itself cannot provide an image.

    Returns the (possibly re-discovered) drawings and the renderings. For .xls the
    drawings come from the .xlsx LibreOffice writes — or from the one the
    legacy-encryption path already produced. Raises on LibreOffice failures.
    """
    out_dir = work_dir / "rendered_drawings"
    if mime == ".xls":
        converted = path.with_suffix(".xlsx")  # left by the encrypted-XLS path, if any
        charts = render_charts and survey.has_charts
        pictures = rasterize_metafiles
        if converted.exists():
            rendered = (
                lo_server.render_drawings(converted, out_dir, charts, pictures)
                if charts or pictures
                else None
            )
        else:
            converted = work_dir / "drawings_source.xlsx"
            rendered = lo_server.render_drawings(
                path, out_dir, charts, pictures, output_xlsx=converted
            )
        return discover_xlsx(converted), rendered

    drawings = survey.drawings
    if drawings is None:
        return None, None
    has_charts, has_metafiles = drawings.needs_rendering()
    charts = render_charts and has_charts
    pictures = rasterize_metafiles and has_metafiles
    if not (charts or pictures):
        return drawings, None
    return drawings, lo_server.render_drawings(path, out_dir, charts, pictures)


# ---------------------------------------------------------------------------
#  Decoding
# ---------------------------------------------------------------------------


def _match_renderings(
    objects: Sequence[SheetObject], rendered: Sequence[RenderedShape]
) -> dict[tuple[str, str, int], Path]:
    """(sheet, kind, kind_index) → PNG. By shape name when it is unambiguous on both
    sides, by drawing order otherwise."""
    native_names = Counter((o.sheet, o.kind, o.shape_name) for o in objects)
    rendered_names = Counter((r.sheet, r.kind, r.name) for r in rendered)
    by_name = {
        (r.sheet, r.kind, r.name): r.png
        for r in rendered
        if r.name and rendered_names[(r.sheet, r.kind, r.name)] == 1
    }
    by_index = {(r.sheet, r.kind, r.index): r.png for r in rendered}

    matches: dict[tuple[str, str, int], Path] = {}
    for o in objects:
        key = (o.sheet, o.kind, o.shape_name)
        png = by_name.get(key) if o.shape_name and native_names[key] == 1 else None
        if png is None:
            png = by_index.get((o.sheet, o.kind, o.kind_index))
        if png is not None:
            matches[(o.sheet, o.kind, o.kind_index)] = png
    return matches


def to_rgb(data: bytes) -> Image | None:
    """Decode an image to RGB, flattening transparency on white. None if unusable."""
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            width, height = im.size
            if width * height > _MAX_PIXELS:
                logger.warning(
                    f"Picture skipped: {width}x{height} exceeds the pixel limit"
                )
                return None
            im.load()
            if im.mode in ("RGBA", "LA", "PA") or (
                im.mode == "P" and "transparency" in im.info
            ):
                rgba = im.convert("RGBA")
                rgb = PILImage.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = im.convert("RGB")
    except (
        PILImage.DecompressionBombError,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
    ) as e:
        logger.warning(f"Picture skipped: cannot decode it ({e!r})")
        return None
    if max(rgb.size) > _MAX_SIDE_PX:
        rgb.thumbnail((_MAX_SIDE_PX, _MAX_SIDE_PX))
    return rgb


def decode_objects(
    drawings: WorkbookDrawings,
    rendered: Sequence[RenderedShape] | None,
    max_images: int,
) -> ExtractedObjects:
    """Turn discovered objects into images and figures.

    - A picture used several times (a logo on every sheet) is decoded once and
      shares one image name.
    - Past ``max_images`` distinct images, pictures are dropped and counted in
      ``omitted``; charts keep their text summary without an image.
    - A picture that cannot be decoded (or rendered) is dropped; a chart that was
      not rendered keeps its summary.
    """
    renders = _match_renderings(drawings.objects, rendered or ())
    sheet_numbers = {name: i for i, name in enumerate(drawings.sheet_order, start=1)}
    figures: dict[str, list[Figure]] = {}
    images: dict[str, Image] = {}
    contexts: dict[str, str] = {}
    omitted: Counter[str] = Counter()
    by_media: dict[str, str | None] = {}

    with zipfile.ZipFile(drawings.source) as zf:
        for obj in drawings.objects:
            png = renders.get((obj.sheet, obj.kind, obj.kind_index))
            image_name: str | None = None

            if obj.media is not None and obj.media in by_media:
                image_name = by_media[obj.media]
            elif obj.kind == "chart" and png is None:
                pass  # not rendered: the chart keeps its text summary only
            elif len(images) >= max_images:
                omitted[obj.sheet] += 1
            else:
                img = _load(obj, zf, png)
                if img is not None:
                    sheet_no = sheet_numbers.get(obj.sheet, 0)
                    image_name = (
                        f"imgs/sheet{sheet_no}_{obj.kind}{obj.kind_index + 1}.jpg"
                    )
                    images[image_name] = img
                    contexts[image_name] = obj.summary or obj.alt_text
                if obj.media is not None:
                    by_media[obj.media] = image_name

            if image_name is None and obj.kind == "picture":
                continue
            figures.setdefault(obj.sheet, []).append(
                Figure(obj.kind, image_name, obj.alt_text, obj.summary)
            )

    return ExtractedObjects(
        sheet_order=drawings.sheet_order,
        figures=figures,
        images=images,
        contexts=contexts,
        omitted=dict(omitted),
    )


def _load(obj: SheetObject, zf: zipfile.ZipFile, png: Path | None) -> Image | None:
    if obj.kind == "chart" or obj.media is None:
        return to_rgb(png.read_bytes()) if png is not None else None
    if PurePosixPath(obj.media).suffix.lower() in RASTER_EXTS:
        try:
            return to_rgb(zf.read(obj.media))
        except (KeyError, zipfile.BadZipFile, OSError) as e:
            logger.warning(f"Picture skipped: cannot read '{obj.media}' ({e!r})")
            return None
    if png is not None:
        return to_rgb(png.read_bytes())
    logger.info(f"Picture skipped: '{obj.media}' needs LibreOffice rendering")
    return None
