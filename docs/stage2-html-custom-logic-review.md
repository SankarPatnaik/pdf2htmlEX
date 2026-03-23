# Review of `stage2_html_convert_custom_logic 1.py`

## Scope

This review does **not** modify the script. It documents the most important defects, edge cases, and recommended fixes before you change production logic.

## Highest-risk content loss issues

### 1. Geometric header/footer cropping is too aggressive

Current logic drops any text block whose top is in the top 8% of the page or whose bottom is in the bottom 8% of the page.

Why this is risky:
- first lines of real content are often close to the top margin;
- signatures, annexures, table continuations, and short final paragraphs are often close to the bottom margin;
- a single tall block can slightly overlap the cutoff and get removed completely.

Recommended fix:
- do **not** discard a block only because it intersects a fixed top/bottom band;
- instead compute repeated header/footer candidates across pages and remove only text that repeats with high confidence;
- if you still keep geometry, remove only lines whose full bounding box is inside the candidate header/footer band, not blocks that merely overlap it.

## 2. The trailing page-marker regex is over-broad and can delete real case text

Current cleanup removes any line ending with a long case-pattern prefix followed by spaces and digits.

Why this is risky:
- legal text can naturally end with case references or numbered items;
- the pattern is broad enough to match legitimate content in body paragraphs;
- the same broad regex is applied again when merged order paragraphs are flushed, so the script gets two chances to remove valid text.

Recommended fix:
- anchor removal to known pagination zones or repeated header/footer lines only;
- require exact match against a page-marker template instead of partial trailing substitution;
- compare against the same line appearing on multiple pages before deleting it.

## 3. Block extraction loses table structure before HTML generation starts

The script concatenates span text into plain lines and stores only text, size, bold, italic, indent, and page.

Why this is risky:
- table rows depend on x/y coordinates, column bands, and line adjacency;
- once line coordinates are discarded, downstream code cannot reliably detect columns or preserve row grouping;
- indented table cells can be misread as quotes or normal paragraphs.

Recommended fix:
- preserve per-line bounding boxes and per-span bounding boxes in the intermediate Python objects;
- keep `x0`, `y0`, `x1`, `y1`, page width/height, and block/line ids;
- perform table detection before paragraph merging.

## 4. Reading-order assumptions can corrupt multi-column layouts and tables

The script uses `page.get_text("blocks")` with sorting in one path and `page.get_text("dict")` in another path, then merges lines mostly by indentation and sequential order.

Why this is risky:
- PDF extraction order is not guaranteed to equal human reading order;
- two-column pages can interleave lines;
- tables with narrow columns can be flattened into sentence-like text when rows are merged.

Recommended fix:
- sort by page, then by vertical bands, then by x-position within detected regions;
- run a column/table detection pass before paragraph joining;
- avoid merging lines when the next line changes x-position sharply or falls into a different column band.

## 5. Bare numeric-line filtering can remove valid content

The block extractor drops lines that match only `^\d{1,4}$`.

Why this is risky:
- numbered exhibits, issue numbers, table serial numbers, and single-cell rows may be valid content;
- some scanned/OCR PDFs split table cells into isolated numeric lines.

Recommended fix:
- only drop numeric-only lines when they are in a confirmed footer/header zone or repeat on many pages;
- otherwise keep them and let later structure detection decide.

## 6. Judge-name classification is position-insensitive

Lines matching the judge-name pattern are collected globally and later assigned either to the body opener or footer based on limited heuristics.

Why this is risky:
- the page number and local y-position are ignored after classification;
- a judge-name line in a header block, appendix, or certification section can be misplaced;
- multiple judges or concurring opinions may not fit the current single-opener/single-footer heuristic.

Recommended fix:
- retain page and y-position for judge-name candidates;
- classify relative to the nearest `JUDGMENT`/`ORDER` heading on the same page;
- keep a fallback that does not move the line if confidence is low.

## 7. Paragraph merging can swallow list items and table rows

Merged order paragraphs append every non-heading, non-quote line into the current paragraph until a new numbered paragraph is found.

Why this is risky:
- bullet points, sub-items `(i)`, `(a)`, `-`, and table rows do not trigger a flush;
- row-like lines from tables can be concatenated into prose;
- heading-like short lines inside annexures can be merged into adjacent paragraphs.

Recommended fix:
- flush on strong row/list signals such as repeated multi-space gaps, tab-like x jumps, bullet markers, roman numerals, or abrupt y-gap changes;
- add a `table_row` and `list_item` type before body paragraph merging;
- keep line-level geometry available until after segmentation.

## Table-preservation plan

## A. Preserve geometry in the intermediate objects

Before any cleanup, keep for each line:
- `page`, `block_id`, `line_id`;
- `bbox` (`x0`, `y0`, `x1`, `y1`);
- span-level boxes and text;
- font size and style.

This is mandatory if you want reliable table recovery.

## B. Detect table candidates before regex cleanup

Use a candidate score per region/page based on:
- many lines sharing similar left/right x bands;
- repeated vertical alignment of words into 2+ columns;
- ruling lines from `page.get_drawings()`;
- high density of short numeric/date/currency tokens;
- repeated y-gaps consistent with row spacing.

If the score is high, mark the region as `table_candidate` and skip paragraph merging/quote detection there.

## C. Build rows from y-clustering and columns from x-clustering

Recommended steps:
1. cluster lines into rows using overlapping or near-overlapping `y0/y1` ranges;
2. inside each row, cluster spans/words into column buckets using x-position gaps;
3. align rows to a stable set of column boundaries derived from the whole candidate region;
4. keep empty cells where a row has no text in a column.

## D. Emit semantic HTML only when confidence is high

When a region looks tabular with stable columns and rows:
- emit `<table>`, `<tr>`, `<td>` or `<th>`;
- use colspan only when adjacent cell bands merge;
- preserve original text order inside each cell.

When confidence is low:
- keep a visual block representation instead of forcing a bad semantic table.

## E. Header/footer removal should be table-aware

Do not run page-marker stripping inside a detected table region.

Reason:
- right-most numeric columns are often mistaken for page numbers;
- top rows of tables can sit near header cutoffs;
- bottom rows can sit near footer cutoffs.

## F. Add targeted regression fixtures

Create tests with at least these PDF classes:
- repeated running headers and footers;
- first paragraph close to the top margin;
- last paragraph close to the bottom margin;
- two-column judgments;
- simple bordered tables;
- borderless tables made only from alignment;
- tables with numeric-only cells;
- annexures and signature pages.

## Practical change order

1. Stop deleting by fixed top/bottom percentages alone.
2. Replace broad trailing regex deletion with repeated-line detection.
3. Preserve line/span geometry in the extracted intermediate objects.
4. Add `table_candidate` detection before paragraph merging.
5. Add row/column clustering for high-confidence tables.
6. Only after that, tighten paragraph/list/quote heuristics.

## Summary

The main issue is not just the regex itself. The bigger problem is that the script removes content **before** it has enough positional context to know whether a line is a footer, a paragraph, or a table row. If you first preserve geometry and then remove only repeated header/footer artifacts with confidence scoring, you will eliminate most accidental text loss and greatly improve table preservation.
