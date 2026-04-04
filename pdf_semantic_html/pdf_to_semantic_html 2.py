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
div.para-block {
  margin: 0 0 0.95em;
}
ol, ul {
  margin: 0.15em 0 1em 1.5em;
  padding-left: 1.2em;
}
li {
  margin-bottom: 0.4em;
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
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentStats:
    body_font_size: float = 11.0
    typical_line_gap: float = 3.0
    median_left_indent: float = 72.0
    median_right_edge: float = 520.0
    median_line_height: float = 12.0
    is_judis_like: bool = False


@dataclass
class Config:
    header_band_ratio: float = 0.12
    footer_band_ratio: float = 0.10
    repeat_threshold_ratio: float = 0.6
    min_repeat_pages: int = 2
    body_font_tolerance: float = 1.5
    same_indent_tolerance: float = 10.0
    same_right_edge_tolerance: float = 18.0
    paragraph_gap_multiplier: float = 1.9
    heading_gap_multiplier: float = 2.5
    image_min_size: float = 28.0
    max_heading_words: int = 18
    drop_page_numbers: bool = True
    embed_css: bool = True
    paragraph_hanging_indent: float = 12.0
    list_indent_delta: float = 14.0
    watermark_image_area_ratio: float = 0.22


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

        for block in text_dict.get("blocks", []):
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
                    )
                )

        lines = self._merge_same_baseline_fragments(lines)
        images = self._extract_images(page, page_number)
        filtered_images = self._filter_non_watermark_images(
            images=images,
            lines=lines,
            page_width=float(page.rect.width),
            page_height=float(page.rect.height),
        )
        self._cleanup_filtered_image_assets(images, filtered_images)
        tables = self._extract_tables(page)
        return {
            "page_number": page_number,
            "width": float(page.rect.width),
            "height": float(page.rect.height),
            "lines": sorted(lines, key=lambda l: (round(l.y0, 1), round(l.x0, 1))),
            "images": filtered_images,
            "tables": tables,
        }


    def _merge_same_baseline_fragments(self, lines: list[Line]) -> list[Line]:
        if not lines:
            return []

        lines = sorted(lines, key=lambda l: (round(l.y0, 1), round(l.x0, 1)))
        merged: list[Line] = []
        current = lines[0]

        for nxt in lines[1:]:
            same_baseline = abs(current.y0 - nxt.y0) <= 2.5 and abs(current.y1 - nxt.y1) <= 2.5
            compatible_style = abs(current.font_size - nxt.font_size) <= 0.75
            horizontally_ordered = nxt.x0 >= current.x0
            small_horizontal_gap = (nxt.x0 - current.x1) <= 32.0

            if same_baseline and compatible_style and horizontally_ordered and small_horizontal_gap:
                joiner = "" if not self._should_insert_space(current.text, nxt.text) else " "
                current = Line(
                    text=(current.text.rstrip() + joiner + nxt.text.lstrip()).strip(),
                    bbox=self._merge_bboxes([current.bbox, nxt.bbox]),
                    font_size=max(current.font_size, nxt.font_size),
                    font_name=current.font_name or nxt.font_name,
                    flags=current.flags,
                    page_number=current.page_number,
                )
            else:
                merged.append(current)
                current = nxt

        merged.append(current)
        return merged

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
            if not self._is_probable_real_table(data):
                continue
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
            images.append(
                {
                    "bbox": bbox,
                    "src": f"assets/{out_name}",
                    "alt": out_name,
                    "meta": {
                        "width": int(image_dict.get("width", 0) or 0),
                        "height": int(image_dict.get("height", 0) or 0),
                        "colorspace": str(image_dict.get("cs-name", "") or ""),
                    },
                }
            )
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
        line_heights: list[float] = []
        line_widths: list[float] = []
        numbered_line_count = 0
        all_caps_short_line_count = 0
        total_lines = 0

        for page in pages_raw:
            lines = page["lines"]
            for i, line in enumerate(lines):
                if self._looks_like_margin_noise(line.text):
                    continue
                total_lines += 1
                font_sizes.append(line.font_size)
                left_indents.append(line.x0)
                right_edges.append(line.x1)
                line_heights.append(line.height)
                line_widths.append(line.width)
                if self._starts_list_item(line.text):
                    numbered_line_count += 1
                if self._is_all_caps_heading_candidate(line.text):
                    all_caps_short_line_count += 1
                if i > 0:
                    prev = lines[i - 1]
                    gap = line.y0 - prev.y1
                    if 0 <= gap <= 40:
                        line_gaps.append(gap)

        body_font_size = self._robust_median(font_sizes, default=11.0)
        typical_line_gap = self._robust_median(line_gaps, default=3.0)
        median_left_indent = self._robust_median(left_indents, default=72.0)
        median_right_edge = self._robust_median(right_edges, default=520.0)
        median_line_height = self._robust_median(line_heights, default=max(10.0, body_font_size + 1.0))
        median_line_width = self._robust_median(line_widths, default=420.0)
        wide_line_ratio = (
            len([w for w in line_widths if w >= median_line_width * 0.90]) / len(line_widths)
            if line_widths else 0.0
        )
        judis_signals = 0
        if total_lines and (numbered_line_count / total_lines) >= 0.08:
            judis_signals += 1
        if total_lines and (all_caps_short_line_count / total_lines) >= 0.04:
            judis_signals += 1
        if wide_line_ratio >= 0.55:
            judis_signals += 1
        is_judis_like = judis_signals >= 2
        return DocumentStats(
            body_font_size=body_font_size,
            typical_line_gap=typical_line_gap,
            median_left_indent=median_left_indent,
            median_right_edge=median_right_edge,
            median_line_height=median_line_height,
            is_judis_like=is_judis_like,
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
        ]

        reserved_regions = [fitz.Rect(t["bbox"]) for t in page_raw["tables"]]
        reserved_regions.extend(fitz.Rect(i["bbox"]) for i in page_raw["images"])

        flow_elements: list[Element] = []

        body_lines = [l for l in lines if not self._line_overlaps_reserved(l, reserved_regions)]
        body_chunks = self._group_lines_into_paragraphs(body_lines, stats)
        for chunk in body_chunks:
            kind, payload, bbox = self._classify_chunk(chunk, stats)
            flow_elements.append(Element(kind=kind, y0=bbox[1], payload=payload, bbox=bbox))

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

        parts = [f'<section class="page" data-page="{page_no}">']
        for elem in flow_elements:
            if elem.kind in {"p", "blockquote", "h1", "h2", "h3"}:
                tag = elem.kind
                parts.append(f"<{tag}>{self._preserve_inline_breaks(elem.payload)}</{tag}>")
            elif elem.kind in {"ol", "ul"}:
                list_items = "".join(f"<li>{self._preserve_inline_breaks(item)}</li>" for item in elem.payload)
                parts.append(f"<{elem.kind}>{list_items}</{elem.kind}>")
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
            prev = paragraphs[-1][-1]
            if self._is_paragraph_boundary(prev, current, stats):
                paragraphs.append([current])
            else:
                paragraphs[-1].append(current)
        return paragraphs

    def _classify_chunk(self, lines: list[Line], stats: DocumentStats) -> tuple[str, Any, tuple[float, float, float, float]]:
        bbox = self._merge_bboxes([line.bbox for line in lines])
        text = self._join_paragraph_lines([line.text for line in lines])
        avg_font = statistics.mean([line.font_size for line in lines])

        if self._is_heading_like_text(text, avg_font, stats):
            level = "h1" if avg_font >= stats.body_font_size + 3 else "h2" if avg_font >= stats.body_font_size + 1.5 else "h3"
            return level, text, bbox
        if self._all_lines_are_list_items(lines):
            list_kind = "ol" if self._is_ordered_list_item(lines[0].text) else "ul"
            items = [self._strip_list_marker(line.text) for line in lines]
            return list_kind, [html.escape(item) for item in items], bbox
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
        if self._is_all_caps_heading_candidate(clean):
            return True
        if re.fullmatch(r"(?:\d+(?:\.\d+)*)\s+[A-Z][^.]{0,80}", clean):
            return True
        if re.fullmatch(r"\s*(?:JUDGMENT|ORDER|CORAM|PRAYER|APPEARANCE)\s*", clean, flags=re.I):
            return True
        if clean.endswith(":") and len(words) <= 10:
            return True
        return False

    @staticmethod
    def _is_all_caps_heading_candidate(text: str) -> bool:
        clean = text.strip()
        words = clean.split()
        return bool(clean) and len(clean) <= 120 and clean.isupper() and len(words) <= 12

    def _is_blockquote(self, lines: list[Line], stats: DocumentStats) -> bool:
        if len(lines) < 2:
            return False
        mean_indent = statistics.mean([line.x0 for line in lines])
        return mean_indent > stats.median_left_indent + 20

    def _should_drop_line(self, line: Line, page_raw: dict[str, Any], patterns: set[str]) -> bool:
        norm = self._normalize_repetition_key(line.text)
        if norm in patterns:
            return True
        if self._looks_like_signature_artifact(line.text):
            return True
        if self._looks_like_watermark_text(line, page_raw):
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

    @staticmethod
    def _looks_like_watermark_text(line: Line, page_raw: dict[str, Any]) -> bool:
        text = PDFSemanticHTMLConverter._normalize_text(line.text)
        if not text:
            return False
        if len(text) > 90:
            return False

        upper = text.upper()
        alpha_chars = [ch for ch in text if ch.isalpha()]
        if not alpha_chars:
            return False
        uppercase_ratio = sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
        words = text.split()
        text_is_label_like = (
            uppercase_ratio >= 0.82
            and 1 <= len(words) <= 8
            and not text.endswith((".", "?", "!", ":"))
        )
        known_watermark = bool(
            re.search(r"\b(draft|confidential|sample|copy|duplicate|demo|preview|watermark)\b", upper)
        )
        if not text_is_label_like and not known_watermark:
            return False

        page_width = max(1.0, float(page_raw["width"]))
        page_height = max(1.0, float(page_raw["height"]))
        center_y = page_height * 0.5
        in_mid_band = (page_height * 0.18) <= line.y0 <= (page_height * 0.82)
        near_center_y = abs(((line.y0 + line.y1) * 0.5) - center_y) <= page_height * 0.32
        wide_enough = (line.width / page_width) >= 0.34
        very_large_font = line.font_size >= 20

        return in_mid_band and near_center_y and wide_enough and very_large_font

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
            if prev.endswith("-") and line and line[0].islower():
                out[-1] = prev[:-1] + line
            elif self._should_insert_space(prev, line):
                out[-1] = prev + " " + line.lstrip()
            else:
                out[-1] = prev + line.lstrip()
        return html.escape(out[-1] if out else "") if len(out) == 1 else html.escape(" ".join(out))

    def _is_paragraph_boundary(self, prev: Line, current: Line, stats: DocumentStats) -> bool:
        gap = max(0.0, current.y0 - prev.y1)
        same_font_band = abs(current.font_size - prev.font_size) <= self.config.body_font_tolerance
        indent_shift = current.x0 - prev.x0
        right_edge_delta = abs(current.x1 - prev.x1)
        similar_indent = abs(indent_shift) <= self.config.same_indent_tolerance
        similar_right_edge = right_edge_delta <= self.config.same_right_edge_tolerance
        major_indent_change = abs(indent_shift) > max(14.0, self.config.same_indent_tolerance)
        large_gap = gap > max(
            stats.median_line_height * 0.85,
            stats.typical_line_gap * self.config.paragraph_gap_multiplier
        )
        heading_break = self._is_heading_like_text(current.text, current.font_size, stats)
        current_is_list = self._starts_list_item(current.text)
        prev_is_list = self._starts_list_item(prev.text)
        continuation = self._is_likely_continuation(prev.text, current.text)
        prev_sentence_end = prev.text.strip().endswith((".", "?", "!", ":"))
        hanging_indent_continuation = (
            indent_shift >= self.config.paragraph_hanging_indent
            and gap <= max(stats.typical_line_gap * 1.25, stats.median_line_height * 0.35)
        )
        ragged_paragraph_end = (
            prev.width < (stats.median_right_edge - stats.median_left_indent) * 0.62
            and prev_sentence_end
        )
        numbered_paragraph_break = bool(re.match(r"^\s*\d{1,3}[.)]\s+[A-Za-z]", current.text))

        if heading_break:
            return True
        if numbered_paragraph_break and prev_sentence_end:
            return True
        if current_is_list and not prev_is_list:
            return True
        if current_is_list and prev_is_list:
            list_gap_limit = max(stats.typical_line_gap * 1.8, stats.median_line_height * 0.55)
            return gap > list_gap_limit or abs(indent_shift) > self.config.list_indent_delta
        if not same_font_band and abs(current.font_size - prev.font_size) > 1.2:
            return True
        if large_gap:
            return True
        if major_indent_change and not hanging_indent_continuation:
            return True
        if stats.is_judis_like:
            if prev_sentence_end and indent_shift >= self.config.paragraph_hanging_indent:
                return True
            if prev_sentence_end and not similar_right_edge and right_edge_delta > 28:
                return True
            if ragged_paragraph_end and not continuation:
                return True
        if prev_is_list and not current_is_list and current.x0 <= prev.x0 - self.config.list_indent_delta:
            return True

        return not ((similar_indent or similar_right_edge or hanging_indent_continuation) and continuation)

    def _should_insert_space(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left.endswith(("(", "/", "₹", "$", "§")):
            return False
        if right.startswith((")", ",", ".", ";", ":", "%", "?", "!")):
            return False
        return True

    def _is_likely_continuation(self, prev_text: str, current_text: str) -> bool:
        prev = prev_text.strip()
        curr = current_text.strip()
        if not prev or not curr:
            return False
        if self._starts_list_item(curr):
            return False
        if prev.endswith((':', ';')) and curr[:1].isupper():
            return False
        if prev.endswith('-'):
            return True
        if curr[:1].islower():
            return True
        if prev[-1] not in '.?!:' and len(curr.split()) > 0:
            return True
        return False

    def _is_probable_real_table(self, table_data: list[list[Any]]) -> bool:
        rows = [row for row in table_data if row]
        if len(rows) < 2:
            return False
        col_count = max((len(r) for r in rows), default=0)
        if col_count < 2:
            return False

        normalized_rows: list[list[str]] = []
        for row in rows:
            norm = [self._normalize_text(str(cell or "")) for cell in row]
            normalized_rows.append(norm)

        non_empty_cells = sum(1 for row in normalized_rows for c in row if c)
        total_cells = sum(len(row) for row in normalized_rows)
        if total_cells == 0:
            return False
        density = non_empty_cells / total_cells
        non_empty_per_row = [sum(1 for c in row if c) for row in normalized_rows]
        avg_non_empty_per_row = statistics.mean(non_empty_per_row) if non_empty_per_row else 0.0

        if col_count >= 6 and density < 0.45:
            return False
        if avg_non_empty_per_row < 1.8:
            return False
        return True

    def _filter_non_watermark_images(
        self,
        images: list[dict[str, Any]],
        lines: list[Line],
        page_width: float,
        page_height: float,
    ) -> list[dict[str, Any]]:
        if not images:
            return images
        out: list[dict[str, Any]] = []
        page_area = max(1.0, page_width * page_height)
        text_rects = [fitz.Rect(line.bbox) for line in lines]

        for image in images:
            rect = fitz.Rect(image["bbox"])
            area_ratio = (rect.width * rect.height) / page_area
            if area_ratio < self.config.watermark_image_area_ratio:
                out.append(image)
                continue
            overlap_count = sum(1 for tr in text_rects if tr.intersects(rect))
            if not self._is_probable_watermark_image(
                image=image,
                rect=rect,
                area_ratio=area_ratio,
                overlap_count=overlap_count,
                text_rect_count=len(text_rects),
                page_width=page_width,
                page_height=page_height,
            ):
                out.append(image)
        return out

    def _is_probable_watermark_image(
        self,
        image: dict[str, Any],
        rect: fitz.Rect,
        area_ratio: float,
        overlap_count: int,
        text_rect_count: int,
        page_width: float,
        page_height: float,
    ) -> bool:
        meta = image.get("meta", {})
        colorspace = str(meta.get("colorspace", "")).lower()
        is_grayscale = "gray" in colorspace
        text_overlap_ratio = (overlap_count / text_rect_count) if text_rect_count else 0.0
        center_x = page_width * 0.5
        center_y = page_height * 0.5
        image_center_x = (rect.x0 + rect.x1) * 0.5
        image_center_y = (rect.y0 + rect.y1) * 0.5
        near_center = (
            abs(image_center_x - center_x) <= page_width * 0.22
            and abs(image_center_y - center_y) <= page_height * 0.22
        )

        decorative_large_center = near_center and area_ratio >= self.config.watermark_image_area_ratio
        likely_background_mark = text_overlap_ratio >= 0.35 and area_ratio >= 0.16
        low_overlap_stamp = text_overlap_ratio <= 0.12 and area_ratio >= 0.30

        if decorative_large_center and is_grayscale:
            return True
        if decorative_large_center and (likely_background_mark or low_overlap_stamp):
            return True
        return False

    def _cleanup_filtered_image_assets(
        self,
        all_images: list[dict[str, Any]],
        kept_images: list[dict[str, Any]],
    ) -> None:
        kept_src = {img.get("src") for img in kept_images}
        for image in all_images:
            src = image.get("src")
            if not src or src in kept_src:
                continue
            image_path = self.output_dir / src
            if image_path.exists():
                image_path.unlink()

    @staticmethod
    def _looks_like_signature_artifact(text: str) -> bool:
        t = PDFSemanticHTMLConverter._normalize_text(text).lower()
        if not t:
            return False
        if "signature not verified" in t:
            return True
        if "digitally signed by" in t:
            return True
        if re.fullmatch(r"reason:?", t):
            return True
        if "ist" in t and re.search(r"\d{1,2}:\d{2}:\d{2}", t):
            return True
        return False

    def _starts_list_item(self, text: str) -> bool:
        return bool(re.match(r"^\s*(?:[•\-*]|\(?[0-9ivxIVX]+[.)])\s+", text))

    def _is_ordered_list_item(self, text: str) -> bool:
        return bool(re.match(r"^\s*\(?[0-9ivxIVX]+[.)]\s+", text))

    def _strip_list_marker(self, text: str) -> str:
        return re.sub(r"^\s*(?:[•\-*]|\(?[0-9ivxIVX]+[.)])\s+", "", text).strip()

    def _all_lines_are_list_items(self, lines: list[Line]) -> bool:
        if len(lines) < 2:
            return False
        return all(self._starts_list_item(line.text) for line in lines)

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

    converter = PDFSemanticHTMLConverter(args.input_pdf, args.output_dir)
    manifest = converter.convert()
    LOGGER.info("HTML written to %s", manifest["output_html"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
