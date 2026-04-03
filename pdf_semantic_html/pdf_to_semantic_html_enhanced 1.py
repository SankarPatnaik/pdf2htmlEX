#!/usr/bin/env python3
"""
Production-ready PDF -> Semantic HTML converter.

Goals:
- Reconstruct paragraphs instead of line-per-<p>
- Preserve tables as real HTML tables
- Extract images and place them in reading order
- Remove repeating headers / footers
- Emit clean semantic HTML + CSS for web rendering

Primary dependency: PyMuPDF (fitz / pymupdf)
Optional dependency: beautifulsoup4 (not required)

Usage:
    python pdf_to_semantic_html.py input.pdf -o out_dir

Outputs:
    out_dir/
      document.html
      assets/
         page_0001_img_001.png
         ...
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import fitz  # PyMuPDF


LOGGER = logging.getLogger("pdf_to_semantic_html")


DEFAULT_CSS = r"""
:root {
  --page-max-width: 920px;
  --text-color: #1f2937;
  --muted: #6b7280;
  --border: #d1d5db;
  --bg: #f3f4f6;
  --surface: #ffffff;
  --table-stripe: #f9fafb;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text-color);
  font-family: Georgia, "Times New Roman", serif;
}
body {
  line-height: 1.65;
}
main.pdf-document {
  max-width: var(--page-max-width);
  margin: 32px auto;
  background: var(--surface);
  padding: 40px 56px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.08);
  border-radius: 12px;
}
header.document-meta {
  border-bottom: 1px solid var(--border);
  padding-bottom: 16px;
  margin-bottom: 24px;
}
header.document-meta h1 {
  margin: 0 0 8px;
  font-size: 2rem;
  line-height: 1.2;
}
header.document-meta .subtitle {
  color: var(--muted);
  font-size: 0.95rem;
}
section.page {
  margin: 0 0 28px 0;
}
h1, h2, h3, h4, h5, h6 {
  font-family: Inter, Arial, sans-serif;
  color: #111827;
  line-height: 1.25;
  margin-top: 1.3em;
  margin-bottom: 0.55em;
  text-align: center;
}
h1 { font-size: 1.8rem; }
h2 { font-size: 1.45rem; }
h3 { font-size: 1.2rem; }
p {
  margin: 0 0 0.9em;
  text-align: justify;
  text-justify: inter-word;
  orphans: 3;
  widows: 3;
}
blockquote {
  margin: 1rem 0;
  padding: 0.6rem 1rem;
  border-left: 4px solid #9ca3af;
  background: #f9fafb;
}
figure {
  margin: 1.25rem 0;
}
figure img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 0 auto;
}
figure figcaption {
  text-align: center;
  color: var(--muted);
  font-size: 0.9rem;
  margin-top: 0.4rem;
}
table {
  width: 100%;
  border-collapse: collapse;
  table-layout: auto;
  margin: 1rem 0 1.25rem;
  font-size: 0.96rem;
}
thead th {
  background: #f3f4f6;
}
th, td {
  border: 1px solid var(--border);
  padding: 8px 10px;
  vertical-align: top;
  text-align: left;
}
tbody tr:nth-child(even) {
  background: var(--table-stripe);
}
hr.page-break {
  margin: 2rem 0;
  border: none;
  border-top: 1px dashed var(--border);
}
.smallcaps {
  font-variant: small-caps;
}
.meta-block {
  color: var(--muted);
  font-size: 0.95rem;
}
@media (max-width: 960px) {
  main.pdf-document {
    margin: 0;
    border-radius: 0;
    padding: 20px 18px;
  }
}
"""


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    font_size: float
    font_name: str
    flags: int
    page_number: int
    block_no: int = -1

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Element:
    kind: str
    y0: float
    payload: Any
    bbox: Optional[tuple[float, float, float, float]] = None


@dataclass
class DocumentStats:
    body_font_size: float = 11.0
    typical_line_gap: float = 3.0
    median_left_indent: float = 72.0
    median_right_edge: float = 520.0


@dataclass
class Config:
    header_band_ratio: float = 0.12
    footer_band_ratio: float = 0.10
    repeat_threshold_ratio: float = 0.6
    min_repeat_pages: int = 2
    body_font_tolerance: float = 1.5
    same_indent_tolerance: float = 10.0
    same_right_edge_tolerance: float = 24.0
    paragraph_gap_multiplier: float = 2.4
    heading_gap_multiplier: float = 2.5
    image_min_size: float = 28.0
    max_heading_words: int = 18
    drop_page_numbers: bool = True
    embed_css: bool = True
    center_headings: bool = True
    post_merge_paragraphs: bool = True
    continuation_words: tuple[str, ...] = (
        "of", "and", "or", "to", "from", "in", "on", "at", "for", "by", "with",
        "under", "into", "upon", "vs.", "v.", "no.", "nos.", "section", "sections"
    )
    enable_ocr: bool = True
    force_ocr: bool = False
    ocr_lang: str = "eng"
    ocr_dpi: int = 300


class PDFSemanticHTMLConverter:
    def __init__(self, pdf_path: Path, output_dir: Path, config: Optional[Config] = None) -> None:
        self.pdf_path = Path(pdf_path)
        self.output_dir = Path(output_dir)
        self.assets_dir = self.output_dir / "assets"
        self.config = config or Config()
        self.doc = fitz.open(self.pdf_path)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def convert(self) -> dict[str, Any]:
        pages_raw = [self._extract_page_data(page, i) for i, page in enumerate(self.doc)]
        header_footer_patterns = self._detect_repeating_margin_text(pages_raw)
        stats = self._compute_document_stats(pages_raw)
        rendered_pages: list[str] = []

        for page_index, page_raw in enumerate(pages_raw):
            html_fragment = self._render_page(
                page_index=page_index,
                page_raw=page_raw,
                stats=stats,
                header_footer_patterns=header_footer_patterns,
            )
            rendered_pages.append(html_fragment)

        title = self._guess_title(pages_raw)
        final_html = self._build_document_html(title=title, pages_html=rendered_pages)
        html_path = self.output_dir / "document.html"
        html_path.write_text(final_html, encoding="utf-8")

        manifest = {
            "input_pdf": str(self.pdf_path),
            "output_html": str(html_path),
            "assets_dir": str(self.assets_dir),
            "page_count": len(self.doc),
            "title": title,
            "stats": stats.__dict__,
            "header_footer_patterns": sorted(header_footer_patterns),
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest

    def _extract_page_data(self, page: fitz.Page, page_number: int) -> dict[str, Any]:
        text_dict = page.get_text("dict", sort=True)
        lines: list[Line] = []

        for block_no, block in enumerate(text_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(span.get("text", "") for span in spans)
                text = self._normalize_text(text)
                if not text:
                    continue
                bbox = self._merge_bboxes([tuple(span["bbox"]) for span in spans])
                primary = max(spans, key=lambda s: len(s.get("text", "")))
                font_size = float(primary.get("size", 11.0))
                font_name = str(primary.get("font", ""))
                flags = int(primary.get("flags", 0))
                lines.append(
                    Line(
                        text=text,
                        bbox=bbox,
                        font_size=font_size,
                        font_name=font_name,
                        flags=flags,
                        page_number=page_number,
                        block_no=block_no,
                    )
                )

        images = self._extract_images(page, page_number)
        tables = self._extract_tables(page)
        return {
            "page_number": page_number,
            "width": float(page.rect.width),
            "height": float(page.rect.height),
            "lines": sorted(lines, key=lambda l: (round(l.y0, 1), round(l.x0, 1))),
            "images": images,
            "tables": tables,
        }

    def _extract_tables(self, page: fitz.Page) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        try:
            found = page.find_tables()
        except Exception as exc:
            LOGGER.warning("Table detection failed on page %s: %s", page.number + 1, exc)
            return tables

        for table in getattr(found, "tables", []):
            try:
                data = table.extract()
            except Exception as exc:
                LOGGER.warning("Table extraction failed on page %s: %s", page.number + 1, exc)
                continue
            bbox = tuple(table.bbox)
            tables.append({"bbox": bbox, "data": data})
        return tables

    def _extract_images(self, page: fitz.Page, page_number: int) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        image_infos = page.get_image_info(xrefs=True)
        seen: set[tuple[int, int, int, int]] = set()

        for idx, info in enumerate(image_infos, start=1):
            bbox = tuple(info.get("bbox", (0, 0, 0, 0)))
            x0, y0, x1, y1 = bbox
            if (x1 - x0) < self.config.image_min_size or (y1 - y0) < self.config.image_min_size:
                continue
            key = tuple(int(v) for v in bbox)
            if key in seen:
                continue
            seen.add(key)

            xref = info.get("xref", 0)
            if not xref:
                continue
            try:
                image_dict = self.doc.extract_image(xref)
            except Exception as exc:
                LOGGER.warning("Image extraction failed on page %s: %s", page_number + 1, exc)
                continue

            ext = image_dict.get("ext", "png")
            out_name = f"page_{page_number + 1:04d}_img_{idx:03d}.{ext}"
            out_path = self.assets_dir / out_name
            out_path.write_bytes(image_dict["image"])
            images.append({"bbox": bbox, "src": f"assets/{out_name}", "alt": out_name})
        return images

    def _detect_repeating_margin_text(self, pages_raw: list[dict[str, Any]]) -> set[str]:
        top_counts: dict[str, int] = {}
        bottom_counts: dict[str, int] = {}
        total_pages = len(pages_raw)

        for page in pages_raw:
            top_limit = page["height"] * self.config.header_band_ratio
            bottom_limit = page["height"] * (1 - self.config.footer_band_ratio)
            top_lines = [self._normalize_repetition_key(l.text) for l in page["lines"] if l.y0 <= top_limit]
            bottom_lines = [self._normalize_repetition_key(l.text) for l in page["lines"] if l.y1 >= bottom_limit]

            for txt in set(filter(None, top_lines)):
                top_counts[txt] = top_counts.get(txt, 0) + 1
            for txt in set(filter(None, bottom_lines)):
                bottom_counts[txt] = bottom_counts.get(txt, 0) + 1

        out: set[str] = set()
        needed = max(self.config.min_repeat_pages, math.ceil(total_pages * self.config.repeat_threshold_ratio))
        for txt, count in {**top_counts, **bottom_counts}.items():
            if count >= needed:
                out.add(txt)
        return out

    def _compute_document_stats(self, pages_raw: list[dict[str, Any]]) -> DocumentStats:
        font_sizes: list[float] = []
        line_gaps: list[float] = []
        left_indents: list[float] = []
        right_edges: list[float] = []

        for page in pages_raw:
            lines = page["lines"]
            for i, line in enumerate(lines):
                if self._looks_like_margin_noise(line.text):
                    continue
                font_sizes.append(line.font_size)
                left_indents.append(line.x0)
                right_edges.append(line.x1)
                if i > 0:
                    prev = lines[i - 1]
                    gap = line.y0 - prev.y1
                    if 0 <= gap <= 40:
                        line_gaps.append(gap)

        body_font_size = self._robust_median(font_sizes, default=11.0)
        typical_line_gap = self._robust_median(line_gaps, default=3.0)
        median_left_indent = self._robust_median(left_indents, default=72.0)
        median_right_edge = self._robust_median(right_edges, default=520.0)
        return DocumentStats(
            body_font_size=body_font_size,
            typical_line_gap=typical_line_gap,
            median_left_indent=median_left_indent,
            median_right_edge=median_right_edge,
        )

    def _render_page(
        self,
        page_index: int,
        page_raw: dict[str, Any],
        stats: DocumentStats,
        header_footer_patterns: set[str],
    ) -> str:
        page_no = page_index + 1
        lines = [
            l for l in page_raw["lines"]
            if not self._should_drop_line(l, page_raw, header_footer_patterns)
            and not self._looks_like_side_margin_noise(l, page_raw["width"])
        ]

        reserved_regions = [fitz.Rect(t["bbox"]) for t in page_raw["tables"]]
        reserved_regions.extend(fitz.Rect(i["bbox"]) for i in page_raw["images"])

        flow_elements: list[Element] = []

        body_lines = [l for l in lines if not self._line_overlaps_reserved(l, reserved_regions)]
        body_chunks = self._group_lines_into_paragraphs(body_lines, stats)
        for chunk in body_chunks:
            kind, text, bbox = self._classify_chunk(chunk, stats)
            flow_elements.append(Element(kind=kind, y0=bbox[1], payload=text, bbox=bbox))

        for table in page_raw["tables"]:
            table_html = self._table_to_html(table["data"])
            flow_elements.append(Element(kind="table", y0=table["bbox"][1], payload=table_html, bbox=table["bbox"]))

        for image in page_raw["images"]:
            figure_html = (
                f'<figure><img src="{html.escape(image["src"])}" '
                f'alt="{html.escape(image["alt"])}" loading="lazy"></figure>'
            )
            flow_elements.append(Element(kind="image", y0=image["bbox"][1], payload=figure_html, bbox=image["bbox"]))

        flow_elements.sort(key=lambda e: (round(e.y0, 1), 0 if e.kind in {"h1", "h2", "h3"} else 1))
        if self.config.post_merge_paragraphs:
            flow_elements = self._coalesce_flow_elements(flow_elements, stats)

        parts = [f'<section class="page" data-page="{page_no}">']
        for elem in flow_elements:
            if elem.kind in {"p", "blockquote", "h1", "h2", "h3"}:
                tag = elem.kind
                parts.append(f"<{tag}>{self._preserve_inline_breaks(elem.payload)}</{tag}>")
            elif elem.kind == "table":
                parts.append(elem.payload)
            elif elem.kind == "image":
                parts.append(elem.payload)
        if page_no != len(self.doc):
            parts.append('<hr class="page-break">')
        parts.append("</section>")
        return "\n".join(parts)

    def _group_lines_into_paragraphs(self, lines: list[Line], stats: DocumentStats) -> list[list[Line]]:
        if not lines:
            return []
        lines = sorted(lines, key=lambda l: (round(l.y0, 1), round(l.x0, 1)))
        paragraphs: list[list[Line]] = [[lines[0]]]

        for current in lines[1:]:
            paragraph = paragraphs[-1]
            prev = paragraph[-1]
            gap = max(0.0, current.y0 - prev.y1)
            same_block = current.block_no == prev.block_no and current.block_no != -1
            same_font_band = abs(current.font_size - prev.font_size) <= max(1.0, self.config.body_font_tolerance)
            similar_indent = abs(current.x0 - prev.x0) <= max(12.0, self.config.same_indent_tolerance)
            similar_right_edge = abs(current.x1 - prev.x1) <= max(28.0, self.config.same_right_edge_tolerance)
            likely_continuation = self._is_likely_continuation(prev.text, current.text, prev, current, stats)
            bullet_break = self._starts_list_item(current.text)
            style_break = self._is_heading_like_text(current.text, current.font_size, stats)
            large_gap = gap > max(10.0, stats.typical_line_gap * self.config.paragraph_gap_multiplier)
            major_indent_change = abs(current.x0 - prev.x0) > 24.0
            paragraph_start_like = self._looks_like_paragraph_start(current.text) and not self._looks_like_short_continuation(current.text)
            prev_looks_wrapped = self._looks_like_visual_line_wrap(prev, stats)

            should_merge = False
            if not bullet_break and not style_break and not large_gap:
                if same_block and same_font_band:
                    should_merge = True
                elif same_font_band and (similar_indent or similar_right_edge) and not major_indent_change and likely_continuation:
                    should_merge = True
                elif same_font_band and prev_looks_wrapped and not paragraph_start_like:
                    should_merge = True

            if should_merge:
                paragraphs[-1].append(current)
            else:
                paragraphs.append([current])
        return paragraphs

    def _classify_chunk(self, lines: list[Line], stats: DocumentStats) -> tuple[str, str, tuple[float, float, float, float]]:
        bbox = self._merge_bboxes([line.bbox for line in lines])
        text = self._join_paragraph_lines([line.text for line in lines])
        first = lines[0]
        avg_font = statistics.mean([line.font_size for line in lines])

        if self._is_heading_like_text(text, avg_font, stats):
            level = "h1" if avg_font >= stats.body_font_size + 3 else "h2" if avg_font >= stats.body_font_size + 1.5 else "h3"
            return level, text, bbox
        if self._is_blockquote(lines, stats):
            return "blockquote", text, bbox
        return "p", text, bbox

    def _is_heading_like_text(self, text: str, font_size: float, stats: DocumentStats) -> bool:
        words = text.split()
        if not words or len(words) > self.config.max_heading_words:
            return False
        if font_size >= stats.body_font_size + 2.5:
            return True
        clean = text.strip()
        if len(clean) <= 120 and clean.isupper() and len(words) <= 12:
            return True
        if re.fullmatch(r"(?:\d+(?:\.\d+)*)\s+[A-Z][^.]{0,80}", clean):
            return True
        if clean.endswith(":") and len(words) <= 10:
            return True
        return False

    def _is_blockquote(self, lines: list[Line], stats: DocumentStats) -> bool:
        if len(lines) < 2:
            return False
        mean_indent = statistics.mean([line.x0 for line in lines])
        return mean_indent > stats.median_left_indent + 20

    def _should_drop_line(self, line: Line, page_raw: dict[str, Any], patterns: set[str]) -> bool:
        norm = self._normalize_repetition_key(line.text)
        if norm in patterns:
            return True

        top_limit = page_raw["height"] * self.config.header_band_ratio
        bottom_limit = page_raw["height"] * (1 - self.config.footer_band_ratio)

        if self.config.drop_page_numbers:
            if line.y0 <= top_limit or line.y1 >= bottom_limit:
                if re.fullmatch(r"(?:page\s+)?\d+(?:\s+of\s+\d+)?", norm, flags=re.I):
                    return True
                if re.fullmatch(r"\d+", norm):
                    return True
        return False

    def _line_overlaps_reserved(self, line: Line, reserved_regions: list[fitz.Rect]) -> bool:
        rect = fitz.Rect(line.bbox)
        for region in reserved_regions:
            if rect.intersects(region):
                return True
        return False

    def _table_to_html(self, table_data: list[list[Any]]) -> str:
        rows = [row for row in table_data if row and any((cell or "").strip() for cell in row)]
        if not rows:
            return ""

        header = rows[0]
        body = rows[1:] if len(rows) > 1 else []

        def cell_text(value: Any) -> str:
            txt = self._normalize_text(str(value or ""))
            return html.escape(txt)

        out = ["<table>"]
        if header:
            out.append("<thead><tr>")
            out.extend(f"<th>{cell_text(cell)}</th>" for cell in header)
            out.append("</tr></thead>")
        if body:
            out.append("<tbody>")
            for row in body:
                out.append("<tr>")
                out.extend(f"<td>{cell_text(cell)}</td>" for cell in row)
                out.append("</tr>")
            out.append("</tbody>")
        out.append("</table>")
        return "".join(out)

    def _join_paragraph_lines(self, lines: list[str]) -> str:
        out: list[str] = []
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            if idx == 0:
                out.append(line)
                continue
            prev = out[-1]
            if prev.endswith("-"):
                if self._looks_like_numeric_continuation(line):
                    out[-1] = prev + line.lstrip()
                elif line and line[0].islower():
                    out[-1] = prev[:-1] + line.lstrip()
                else:
                    out[-1] = prev + line.lstrip()
            elif self._looks_like_short_continuation(line):
                out[-1] = prev + ("" if not self._should_insert_space(prev, line) else " ") + line.lstrip()
            elif self._should_insert_space(prev, line):
                out[-1] = prev + " " + line.lstrip()
            else:
                out[-1] = prev + line.lstrip()
        return html.escape(out[-1] if out else "") if len(out) == 1 else html.escape(" ".join(out))

    def _should_insert_space(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left.endswith(("(", "/", "₹", "$", "§")):
            return False
        if right.startswith((")", ",", ".", ";", ":", "%", "?", "!")):
            return False
        return True

    def _is_likely_continuation(
        self,
        prev_text: str,
        current_text: str,
        prev_line: Optional[Line] = None,
        current_line: Optional[Line] = None,
        stats: Optional[DocumentStats] = None,
    ) -> bool:
        prev = prev_text.strip()
        curr = current_text.strip()
        if not prev or not curr:
            return False
        if self._starts_list_item(curr):
            return False
        if self._looks_like_short_continuation(curr):
            return True
        if prev.endswith((':', ';')) and curr[:1].isupper():
            return False
        if prev.endswith('-'):
            return True
        if curr[:1].islower():
            return True
        if prev[-1] not in '.?!:':
            return True
        if prev_line is not None and current_line is not None and stats is not None:
            if self._looks_like_visual_line_wrap(prev_line, stats) and not self._looks_like_paragraph_start(curr):
                return True
        return False

    def _starts_list_item(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        if re.match(r"^\d{4}\.\s+[A-Z]", t):
            return False
        return bool(re.match(r"^\s*(?:[•\-*]|\(?\d{1,3}[.)]|\(?[ivxIVX]{1,8}[.)]|[A-Za-z][.)])\s+", t))


    def _looks_like_short_continuation(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        if len(t) <= 8 and re.fullmatch(r"[\d./-]+[.)]?$", t):
            return True
        if len(t.split()) <= 3 and re.fullmatch(r"(?:\(?[ivxIVX]+\)?|[a-zA-Z])(?:[.)])?", t):
            return True
        return False

    def _looks_like_numeric_continuation(self, text: str) -> bool:
        t = text.strip()
        return bool(re.fullmatch(r"[\d./-]+[A-Za-z]?\.?", t))

    def _looks_like_paragraph_start(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        if self._starts_list_item(t):
            return True
        if re.match(r'^\(?\d+(?:\.\d+)*[.)]\s+', t):
            return True
        if re.match(r'^[A-Z][A-Z\s,&/-]{3,}$', t):
            return True
        return False

    def _looks_like_visual_line_wrap(self, line: Line, stats: DocumentStats) -> bool:
        return abs(line.x1 - stats.median_right_edge) <= max(20.0, self.config.same_right_edge_tolerance)

    def _looks_like_side_margin_noise(self, line: Line, page_width: float) -> bool:
        text = line.text.strip()
        if not text:
            return True
        if len(text) <= 2 and re.fullmatch(r"[A-H]\.?", text) and (line.x0 < page_width * 0.08 or line.x1 > page_width * 0.92):
            return True
        return False


    def _coalesce_flow_elements(self, elements: list[Element], stats: DocumentStats) -> list[Element]:
        if not elements:
            return elements
        merged: list[Element] = []
        for elem in elements:
            if merged and self._should_merge_elements(merged[-1], elem, stats):
                prev = merged[-1]
                merged_payload = self._merge_element_payload(prev.payload, elem.payload)
                merged_bbox = prev.bbox
                if prev.bbox and elem.bbox:
                    merged_bbox = self._merge_bboxes([prev.bbox, elem.bbox])
                elif elem.bbox:
                    merged_bbox = elem.bbox
                merged[-1] = Element(kind=prev.kind, y0=prev.y0, payload=merged_payload, bbox=merged_bbox)
            else:
                merged.append(elem)
        return merged

    def _should_merge_elements(self, prev: Element, current: Element, stats: DocumentStats) -> bool:
        if prev.kind != "p" or current.kind != "p":
            return False
        if not prev.bbox or not current.bbox:
            return False

        prev_text = html.unescape(str(prev.payload)).strip()
        curr_text = html.unescape(str(current.payload)).strip()
        if not prev_text or not curr_text:
            return False

        vertical_gap = max(0.0, current.bbox[1] - prev.bbox[3])
        if vertical_gap > max(10.0, stats.typical_line_gap * 2.2):
            return False

        indent_delta = abs(current.bbox[0] - prev.bbox[0])
        right_delta = abs(current.bbox[2] - prev.bbox[2])
        same_alignment = (
            indent_delta <= max(18.0, self.config.same_indent_tolerance * 1.5)
            or right_delta <= max(26.0, self.config.same_right_edge_tolerance * 1.5)
        )
        if not same_alignment:
            return False

        if self._starts_list_item(curr_text):
            return False
        if self._looks_like_heading_text(prev_text) or self._looks_like_heading_text(curr_text):
            return False

        if prev_text.endswith("-"):
            return True

        if prev_text.split():
            last_word = re.sub(r"[^A-Za-z.]+$", "", prev_text.split()[-1].lower())
            if last_word in self.config.continuation_words:
                return True

        if prev_text[-1] not in ".?!:":
            return True

        if re.match(r"^(?:\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", curr_text):
            return True

        return False

    def _merge_element_payload(self, left: str, right: str) -> str:
        left_text = html.unescape(str(left)).strip()
        right_text = html.unescape(str(right)).strip()
        merged_text = html.unescape(self._join_paragraph_lines([left_text, right_text]))
        return html.escape(merged_text)

    def _looks_like_heading_text(self, text: str) -> bool:
        clean = text.strip()
        if not clean:
            return False
        words = clean.split()
        if len(words) <= 10 and clean.isupper():
            return True
        if clean.endswith(":") and len(words) <= 12:
            return True
        if re.fullmatch(
            r"(?:PETITIONER|RESPONDENT|BENCH|ACT|HEADNOTE|JUDGMENT|CITATION|DATE OF JUDGMENT)[:\-]?",
            clean,
            flags=re.I,
        ):
            return True
        return False

    def _build_document_html(self, title: str, pages_html: list[str]) -> str:
        css = DEFAULT_CSS
        subtitle = f"Converted from PDF • {html.escape(self.pdf_path.name)}"
        title_safe = html.escape(title)
        parts = [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{title_safe}</title>",
        ]
        if self.config.embed_css:
            parts.append("<style>")
            parts.append(css)
            parts.append("</style>")
        parts.extend([
            "</head>",
            "<body>",
            '<main class="pdf-document">',
            '<header class="document-meta">',
            f"<h1>{title_safe}</h1>",
            f'<div class="subtitle">{subtitle}</div>',
            "</header>",
            *pages_html,
            "</main>",
            "</body>",
            "</html>",
        ])
        return "\n".join(parts)

    def _guess_title(self, pages_raw: list[dict[str, Any]]) -> str:
        if not pages_raw:
            return self.pdf_path.stem
        first_page = pages_raw[0]
        candidates = sorted(first_page["lines"], key=lambda l: (-l.font_size, l.y0))[:8]
        for candidate in candidates:
            text = candidate.text.strip()
            if len(text.split()) >= 2 and len(text) <= 200 and not self._looks_like_margin_noise(text):
                return text
        return self.pdf_path.stem

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\u00a0", " ")
        text = text.replace("\xad", "")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\s*\n\s*", " ", text)
        return text.strip()

    @staticmethod
    def _normalize_repetition_key(text: str) -> str:
        text = PDFSemanticHTMLConverter._normalize_text(text).lower()
        text = re.sub(r"\b\d+\b", "#", text)
        return text

    @staticmethod
    def _merge_bboxes(bboxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
        xs0, ys0, xs1, ys1 = zip(*bboxes)
        return (min(xs0), min(ys0), max(xs1), max(ys1))

    @staticmethod
    def _robust_median(values: list[float], default: float) -> float:
        if not values:
            return default
        try:
            return float(statistics.median(values))
        except statistics.StatisticsError:
            return default

    @staticmethod
    def _preserve_inline_breaks(text: str) -> str:
        return text.replace("\n", "<br>")

    @staticmethod
    def _looks_like_margin_noise(text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return True
        if re.fullmatch(r"(?:page\s+)?\d+(?:\s+of\s+\d+)?", t):
            return True
        if re.search(r"https?://", t):
            return True
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert PDF to semantic HTML.")
    parser.add_argument("input_pdf", type=Path, help="Path to input PDF")
    parser.add_argument("-o", "--output-dir", type=Path, required=True, help="Output directory")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--ocr-lang", default="eng", help="Tesseract OCR language, default: eng")
    parser.add_argument("--ocr-dpi", type=int, default=300, help="Rasterization DPI for OCR pages")
    parser.add_argument("--force-ocr", action="store_true", help="Force OCR on every page")
    parser.add_argument("--disable-ocr", action="store_true", help="Disable OCR fallback")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not args.input_pdf.exists():
        parser.error(f"Input PDF not found: {args.input_pdf}")

    config = Config(
        enable_ocr=not args.disable_ocr,
        force_ocr=args.force_ocr,
        ocr_lang=args.ocr_lang,
        ocr_dpi=args.ocr_dpi,
    )
    converter = PDFSemanticHTMLConverter(args.input_pdf, args.output_dir, config=config)
    manifest = converter.convert()
    LOGGER.info("HTML written to %s", manifest["output_html"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
