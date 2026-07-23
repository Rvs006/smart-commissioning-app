"""Minimal deterministic PDF writer for report artifacts (stdlib only).

Hand-rolled for the same reason the DOCX report is hand-rolled OOXML (repo
convention: stdlib before deps): report artifacts must be byte-reproducible
from the stored run record so the evidence verify endpoint can re-hash a
regenerated artifact. This writer therefore emits NO /Info dictionary (so no
/CreationDate), NO /ID, and no compression — the output bytes are a pure
function of the content added to the document.

Scope, deliberately small:
  * PDF 1.4, A4 portrait or landscape, base-14 Helvetica / Helvetica-Bold with
    WinAnsiEncoding (no font embedding).
  * Bold headings, word-wrapped paragraphs, fixed-column tables whose cells
    are truncated with an ellipsis, automatic page breaks with a repeated
    table header, a "Page N of M" footer on every page, and an optional
    text-only branding band (header wordmark + rule, footer wordmark + run
    id) drawn on every page when the caller passes furniture strings.
  * Text is normalised to the measured WinAnsi subset (printable ASCII plus
    the few punctuation marks the reports use); any other character becomes
    '?' so layout — and therefore the bytes — never depends on unmeasured
    glyph widths.
"""

from __future__ import annotations

from collections.abc import Sequence

# A4 portrait in PDF points. Landscape swaps the same pair per document.
_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_MARGIN = 50
# Lowest allowed text baseline for body content; the footer sits below at y=30.
_BOTTOM_LIMIT = _MARGIN

_BODY_SIZE = 10.0
_FOOTER_SIZE = 8.0
_FOOTER_BASELINE = 30
_HEADING_SIZES = {1: 16.0, 2: 13.0}
_CELL_PADDING = 2.0

# Electracom report palette. Values are deliberately restrained so dense
# commissioning tables stay legible in print and in greyscale.
_INK = (44 / 255, 42 / 255, 40 / 255)
_MUTED_INK = (107 / 255, 101 / 255, 96 / 255)
_BRAND_DARK = (32 / 255, 55 / 255, 70 / 255)
_BRAND_ACCENT = (42 / 255, 123 / 255, 155 / 255)
_BRAND_MIST = (238 / 255, 247 / 255, 249 / 255)

# Optional branding band (text only). The header wordmark baseline sits near the
# top margin with a thin rule below it; _HEADER_RESERVE is the vertical space the
# content cursor drops by on every page so body text never collides with the
# band. Applied only when the caller supplies header furniture.
_HEADER_RESERVE = 46.0

# Helvetica / Helvetica-Bold advance widths (standard AFM metrics, units per
# 1000 at nominal size) for the printable ASCII range 32..126 — enough to
# measure every string this writer accepts after normalisation.
_HELVETICA_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_HELVETICA_BOLD_WIDTHS = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)

# The few non-ASCII characters the reports actually use, mapped to their
# WinAnsi byte and (regular, bold) advance widths. Anything else outside
# printable ASCII is rendered as '?'.
_WINANSI_EXTRAS: dict[str, tuple[int, int, int]] = {
    "–": (0x96, 556, 556),  # en dash
    "—": (0x97, 1000, 1000),  # em dash
    "…": (0x85, 1000, 1000),  # horizontal ellipsis
    "°": (0xB0, 400, 400),  # degree sign — unit strings like "21.5 °C"
    "±": (0xB1, 584, 584),  # plus-minus — tolerance values
    "µ": (0xB5, 556, 611),  # micro sign — units like "µm"
}

_ELLIPSIS = "…"


def _normalize(text: str) -> str:
    """Reduce text to the measured WinAnsi subset; unsupported chars become '?'."""
    normalized: list[str] = []
    for char in text:
        code = ord(char)
        if 32 <= code <= 126 or char in _WINANSI_EXTRAS:
            normalized.append(char)
        elif char in ("\n", "\r", "\t"):
            normalized.append(" ")
        else:
            normalized.append("?")
    return "".join(normalized)


def _char_width(char: str, bold: bool) -> int:
    code = ord(char)
    if 32 <= code <= 126:
        table = _HELVETICA_BOLD_WIDTHS if bold else _HELVETICA_WIDTHS
        return table[code - 32]
    extra = _WINANSI_EXTRAS.get(char)
    if extra is not None:
        return extra[2] if bold else extra[1]
    return _char_width("?", bold)


def _text_width(text: str, size: float, bold: bool) -> float:
    return sum(_char_width(char, bold) for char in text) * size / 1000.0


def _escape_string(text: str) -> bytes:
    """PDF literal-string bytes for normalised text: WinAnsi + escaped ( ) \\."""
    encoded = bytearray()
    for char in text:
        if char in _WINANSI_EXTRAS:
            encoded.append(_WINANSI_EXTRAS[char][0])
        elif char == "\\":
            encoded += b"\\\\"
        elif char == "(":
            encoded += b"\\("
        elif char == ")":
            encoded += b"\\)"
        else:
            encoded.append(ord(char))
    return bytes(encoded)


def _num(value: float) -> str:
    """Deterministic short number formatting for content-stream operands."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _wrap(text: str, size: float, bold: bool, max_width: float) -> list[str]:
    """Greedy word wrap; over-long tokens are hard-broken so nothing overflows.

    Widths accumulate as integer font units in a single forward pass (one
    _char_width per character). Each comparison evaluates
    ``units * size / 1000.0`` — the exact float expression _text_width uses on
    the same integer sum — so the cut points are byte-identical to the old
    shrink-and-remeasure algorithm, which was O(n^3) on long unbroken tokens
    and stalled report downloads for minutes on multi-KB issue text.
    """
    lines: list[str] = []
    current = ""
    current_units = 0
    space_units = _char_width(" ", bold)
    for word in text.split(" "):
        if not word:
            continue
        word_units = [_char_width(char, bold) for char in word]
        total_units = sum(word_units)
        candidate_units = current_units + space_units + total_units if current else total_units
        if candidate_units * size / 1000.0 <= max_width:
            current = f"{current} {word}" if current else word
            current_units = candidate_units
            continue
        if current:
            lines.append(current)
            current = ""
            current_units = 0
        start = 0
        remaining_units = total_units
        while len(word) - start > 1 and remaining_units * size / 1000.0 > max_width:
            # Largest prefix of the remaining token that fits (at least one
            # character, so progress is guaranteed even in a sliver column).
            units = 0
            cut = 0
            for index in range(start, len(word)):
                units += word_units[index]
                if units * size / 1000.0 <= max_width:
                    cut = index + 1 - start
                else:
                    break
            cut = max(cut, 1)
            lines.append(word[start : start + cut])
            remaining_units -= sum(word_units[start : start + cut])
            start += cut
        current = word[start:]
        current_units = remaining_units
    if current:
        lines.append(current)
    return lines or [""]


def _truncate(text: str, size: float, bold: bool, max_width: float) -> str:
    """Single-line cell text, truncated with an ellipsis when it cannot fit.

    Single forward prefix-scan over integer advance widths; the comparison is
    the exact float expression _text_width applies to the same integer sum, so
    the cut point is byte-identical to the old decrement-and-remeasure loop
    (which was O(n^2) and hung on multi-KB cell text).
    """
    if _text_width(text, size, bold) <= max_width:
        return text
    ellipsis_units = _char_width(_ELLIPSIS, bold)
    units = 0
    cut = 0
    for index, char in enumerate(text):
        units += _char_width(char, bold)
        if (units + ellipsis_units) * size / 1000.0 <= max_width:
            cut = index + 1
        else:
            break
    return text[:cut] + _ELLIPSIS


class _PageBuilder:
    """Accumulates content-stream operations page by page, tracking the cursor."""

    def __init__(
        self,
        *,
        page_height: float,
        margin: float,
        bottom_limit: float,
        top_reserve: float = 0.0,
    ) -> None:
        self.pages: list[list[bytes]] = []
        self.page_height = page_height
        self.margin = margin
        self.bottom_limit = bottom_limit
        self.top_reserve = top_reserve
        self.y = 0.0
        self.new_page()

    def new_page(self) -> None:
        self.pages.append([])
        self.y = float(self.page_height - self.margin) - self.top_reserve

    def fits(self, height: float) -> bool:
        return self.y - height >= self.bottom_limit

    def ensure(self, height: float) -> None:
        if not self.fits(height):
            self.new_page()

    def text(
        self,
        x: float,
        y: float,
        content: str,
        size: float,
        bold: bool,
        *,
        color: tuple[float, float, float] = _INK,
    ) -> None:
        font = b"/F2" if bold else b"/F1"
        operands = f" {_num(size)} Tf {_num(x)} {_num(y)} Td (".encode("ascii")
        color_op = f"{_num(color[0])} {_num(color[1])} {_num(color[2])} rg ".encode("ascii")
        self.pages[-1].append(
            b"q " + color_op + b"BT " + font + operands + _escape_string(content) + b") Tj ET Q\n"
        )

    def fill_rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        color: tuple[float, float, float],
    ) -> None:
        self.pages[-1].append(
            (
                f"q {_num(color[0])} {_num(color[1])} {_num(color[2])} rg "
                f"{_num(x)} {_num(y)} {_num(width)} {_num(height)} re f Q\n"
            ).encode("ascii")
        )

    def rule(
        self,
        x0: float,
        y: float,
        x1: float,
        *,
        color: tuple[float, float, float] = _BRAND_DARK,
        width: float = 0.5,
    ) -> None:
        self.pages[-1].append(
            (
                f"q {_num(color[0])} {_num(color[1])} {_num(color[2])} RG "
                f"{_num(width)} w {_num(x0)} {_num(y)} m {_num(x1)} {_num(y)} l S Q\n"
            ).encode("ascii")
        )


class PdfDocument:
    """Deterministic PDF composer: headings, paragraphs, and simple tables.

    Usage: instantiate, call add_heading / add_paragraph / add_table in reading
    order, then render() for the final bytes. render() is a pure function of
    the added content — no timestamps, ids, or environment-dependent state.
    """

    def __init__(
        self,
        *,
        header_left: str | None = None,
        header_right: str | None = None,
        footer_left: str | None = None,
        footer_right: str | None = None,
        landscape: bool = False,
    ) -> None:
        self._items: list[tuple[object, ...]] = []
        self._page_width = float(_PAGE_HEIGHT if landscape else _PAGE_WIDTH)
        self._page_height = float(_PAGE_WIDTH if landscape else _PAGE_HEIGHT)
        self._content_width = self._page_width - 2 * _MARGIN
        self._header_baseline = self._page_height - 34
        self._header_rule_y = self._page_height - 40
        # Furniture strings for the optional branding band, normalised to the
        # measured WinAnsi subset at construction. All default None -> the
        # output is byte-identical to the furniture-less writer.
        self._header_left = _normalize(header_left) if header_left is not None else None
        self._header_right = _normalize(header_right) if header_right is not None else None
        self._footer_left = _normalize(footer_left) if footer_left is not None else None
        self._footer_right = _normalize(footer_right) if footer_right is not None else None

    def add_heading(
        self,
        text: str,
        *,
        level: int = 2,
        keep_with_next: float = 36.0,
    ) -> None:
        size = _HEADING_SIZES.get(level, _HEADING_SIZES[2])
        self._items.append(
            ("text", _normalize(text), size, True, 10.0, 4.0, keep_with_next, _BRAND_DARK)
        )

    def add_paragraph(
        self,
        text: str,
        *,
        bold: bool = False,
        keep_with_next: float = 0.0,
    ) -> None:
        self._items.append(
            ("text", _normalize(text), _BODY_SIZE, bold, 0.0, 5.0, keep_with_next, _INK)
        )

    def add_page_break(self) -> None:
        """Start the next item on a fresh page without hiding any content."""
        self._items.append(("page_break",))

    def add_table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        widths: Sequence[float] | None = None,
        size: float = _BODY_SIZE,
    ) -> None:
        """Fixed-column table; ``widths`` are relative weights (default: equal).

        ``size`` is the cell font size — dense many-column tables pass a smaller
        one so key cells survive the fixed-width truncation.
        """
        header_cells = tuple(_normalize(str(header)) for header in headers)
        if not header_cells:
            return
        body_rows = tuple(
            tuple(_normalize(str(cell)) for cell in row)[: len(header_cells)] for row in rows
        )
        weights = tuple(float(weight) for weight in widths) if widths else ()
        if len(weights) != len(header_cells):
            weights = (1.0,) * len(header_cells)
        self._items.append(("table", header_cells, body_rows, weights, float(size)))

    def render(self) -> bytes:
        has_header = self._header_left is not None or self._header_right is not None
        builder = _PageBuilder(
            page_height=self._page_height,
            margin=_MARGIN,
            bottom_limit=_BOTTOM_LIMIT,
            top_reserve=_HEADER_RESERVE if has_header else 0.0,
        )
        for item in self._items:
            if item[0] == "page_break":
                # Do not create a blank trailing page if a caller accidentally
                # appends a page break after all content.
                if builder.pages[-1]:
                    builder.new_page()
            elif item[0] == "text":
                _, text, size, bold, space_before, space_after, keep_with_next, color = item
                self._layout_text(
                    builder,
                    str(text),
                    float(size),
                    bool(bold),
                    float(space_before),
                    float(space_after),
                    float(keep_with_next),
                    color,
                )
            else:
                _, headers, rows, weights, size = item
                self._layout_table(builder, headers, rows, weights, float(size))  # type: ignore[arg-type]
        if has_header:
            self._append_headers(builder.pages)
        self._append_footers(builder.pages)
        return _assemble(builder.pages, self._page_width, self._page_height)

    def _layout_text(
        self,
        builder: _PageBuilder,
        text: str,
        size: float,
        bold: bool,
        space_before: float,
        space_after: float,
        keep_with_next: float,
        color: tuple[float, float, float],
    ) -> None:
        line_height = size * 1.3
        lines = _wrap(text, size, bold, self._content_width)
        builder.ensure(
            space_before + line_height * len(lines) + space_after + keep_with_next
        )
        builder.y -= space_before
        for line in lines:
            builder.ensure(line_height)
            builder.y -= line_height
            builder.text(_MARGIN, builder.y, line, size, bold, color=color)
        builder.y -= space_after

    def _layout_table(
        self,
        builder: _PageBuilder,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
        weights: tuple[float, ...],
        size: float,
    ) -> None:
        row_height = size + 5.0
        total_weight = sum(weights) or float(len(weights))
        column_widths = [self._content_width * weight / total_weight for weight in weights]
        x_positions: list[float] = []
        x = float(_MARGIN)
        for width in column_widths:
            x_positions.append(x)
            x += width

        def emit_cells(
            cells: Sequence[str],
            bold: bool,
            *,
            fill: tuple[float, float, float] | None = None,
            text_color: tuple[float, float, float] = _INK,
        ) -> None:
            builder.y -= row_height
            if fill is not None:
                builder.fill_rect(
                    _MARGIN,
                    builder.y - 3,
                    self._content_width,
                    row_height,
                    fill,
                )
            for cell_x, width, cell in zip(x_positions, column_widths, cells, strict=False):
                usable = width - 2 * _CELL_PADDING
                builder.text(
                    cell_x + _CELL_PADDING,
                    builder.y,
                    _truncate(cell, size, bold, usable),
                    size,
                    bold,
                    color=text_color,
                )

        def emit_header() -> None:
            emit_cells(headers, True, fill=_BRAND_DARK, text_color=(1.0, 1.0, 1.0))
            builder.rule(
                _MARGIN,
                builder.y - 3,
                _MARGIN + self._content_width,
                color=_BRAND_ACCENT,
                width=1.2,
            )

        # Never orphan the header: require room for it plus one body row.
        builder.ensure(2 * row_height + 6)
        emit_header()
        for row_index, row in enumerate(rows):
            if not builder.fits(row_height + 6):
                builder.new_page()
                emit_header()
            emit_cells(row, False, fill=_BRAND_MIST if row_index % 2 else None)
        builder.rule(_MARGIN, builder.y - 3, _MARGIN + self._content_width)
        builder.y -= 10.0

    def _append_headers(self, pages: list[list[bytes]]) -> None:
        """Draw the text-only branding band on every page.

        Header-left wordmark (bold /F2) at the left margin, header-right label
        right-aligned to the right margin (/F1), and a thin rule beneath. The
        band lives in the space reserved by _HEADER_RESERVE, so appending these
        ops after the content ops is safe — the reserved region never overlaps
        body text and PDF draw order is irrelevant for non-overlapping marks.

        ponytail: text wordmark only. Embedding the logo PNG is a phase-2 item —
        the shipped asset is RGBA (color type 6), so the hand-rolled writer would
        need a raw image XObject plus a separate /SMask (or a pre-flattened
        opaque-RGB asset). Out of scope here.
        """
        left_width = (
            _text_width(self._header_left, _BODY_SIZE, True) if self._header_left else 0.0
        )
        right_edge = self._page_width - _MARGIN
        right_start_limit = _MARGIN + left_width + (18.0 if self._header_left else 0.0)
        right_max_width = max(right_edge - right_start_limit, 1.0)
        right_label = (
            _truncate(self._header_right, _BODY_SIZE, False, right_max_width)
            if self._header_right
            else None
        )
        for operations in pages:
            if self._header_left:
                operations.append(
                    b"q "
                    + f"{_num(_BRAND_DARK[0])} {_num(_BRAND_DARK[1])} {_num(_BRAND_DARK[2])} rg ".encode("ascii")
                    + b"BT /F2 "
                    + f"{_num(_BODY_SIZE)} Tf {_num(_MARGIN)} {_num(self._header_baseline)} Td (".encode("ascii")
                    + _escape_string(self._header_left)
                    + b") Tj ET Q\n"
                )
            if right_label:
                x = right_edge - _text_width(right_label, _BODY_SIZE, False)
                operations.append(
                    b"q "
                    + f"{_num(_MUTED_INK[0])} {_num(_MUTED_INK[1])} {_num(_MUTED_INK[2])} rg ".encode("ascii")
                    + b"BT /F1 "
                    + f"{_num(_BODY_SIZE)} Tf {_num(x)} {_num(self._header_baseline)} Td (".encode("ascii")
                    + _escape_string(right_label)
                    + b") Tj ET Q\n"
                )
            operations.append(
                (
                    f"q {_num(_BRAND_ACCENT[0])} {_num(_BRAND_ACCENT[1])} {_num(_BRAND_ACCENT[2])} RG "
                    f"1.2 w {_num(_MARGIN)} {_num(self._header_rule_y)} m "
                    f"{_num(_MARGIN + self._content_width)} {_num(self._header_rule_y)} l S Q\n"
                ).encode("ascii")
            )

    def _append_footers(self, pages: list[list[bytes]]) -> None:
        total = len(pages)
        for number, operations in enumerate(pages, start=1):
            label = f"Page {number} of {total}"
            x = (self._page_width - _text_width(label, _FOOTER_SIZE, False)) / 2
            operations.append(
                b"BT /F1 "
                + f"{_num(_FOOTER_SIZE)} Tf {_num(x)} {_FOOTER_BASELINE} Td (".encode("ascii")
                + _escape_string(label)
                + b") Tj ET\n"
            )
            if self._footer_left:
                operations.append(
                    b"BT /F1 "
                    + f"{_num(_FOOTER_SIZE)} Tf {_num(_MARGIN)} {_FOOTER_BASELINE} Td (".encode("ascii")
                    + _escape_string(self._footer_left)
                    + b") Tj ET\n"
                )
            if self._footer_right:
                right_x = self._page_width - _MARGIN - _text_width(self._footer_right, _FOOTER_SIZE, False)
                operations.append(
                    b"BT /F1 "
                    + f"{_num(_FOOTER_SIZE)} Tf {_num(right_x)} {_FOOTER_BASELINE} Td (".encode("ascii")
                    + _escape_string(self._footer_right)
                    + b") Tj ET\n"
                )


def _assemble(pages: list[list[bytes]], page_width: float, page_height: float) -> bytes:
    """Serialize pages into a PDF 1.4 file with a correct xref table.

    Object layout: 1 Catalog, 2 Pages, 3 /F1 Helvetica, 4 /F2 Helvetica-Bold,
    then (page, contents) object pairs. The trailer carries no /Info and no
    /ID so the bytes stay reproducible.
    """
    objects: list[bytes] = []
    kids = " ".join(f"{5 + 2 * index} 0 R" for index in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for index, operations in enumerate(pages):
        contents_ref = 6 + 2 * index
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(page_width)} {_num(page_height)}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {contents_ref} 0 R >>"
            ).encode("ascii")
        )
        stream = b"".join(operations)
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        )

    buffer = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(buffer)
    buffer += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for offset in offsets:
        buffer += f"{offset:010d} 00000 n \n".encode("ascii")
    buffer += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(buffer)
