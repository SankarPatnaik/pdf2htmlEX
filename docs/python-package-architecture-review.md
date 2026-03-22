# Python Package Architecture Review for pdf2htmlEX

## Executive Summary

This repository is a maintained fork of **pdf2htmlEX**, a mature C++ converter that uses Poppler for PDF parsing, FontForge for font extraction/transcoding, and Cairo/Splash renderers for background generation. It is **not** a Python package today; it is a native binary plus shell scripts, test fixtures, and packaging artifacts.

For the user's goal — a production-grade Python package that converts PDFs into high-quality HTML for website rendering — this codebase is a **strong rendering engine foundation**, but a **weak package/application foundation**.

### Bottom line

- **Reuse** the core rendering logic where it preserves PDF fidelity better than any greenfield Python rewrite would.
- **Wrap only selectively**: do not blindly shell out to the existing CLI as the whole product design.
- **Rewrite the orchestration layer** in Python: API surface, configuration model, output post-processing, semantic enhancement, packaging, testing, and documentation.
- **Refactor or isolate native components** behind a narrow adapter boundary so the Python package can evolve independently.

## How the repository works today

### 1. Entry point and configuration

The binary entry point is `pdf2htmlEX/src/pdf2htmlEX.cc`. It defines a global `Param` instance, registers a large flat set of CLI flags through `ArgParser`, opens the PDF via Poppler, and runs conversion through `HTMLRenderer`. The control flow is classic monolithic command-line application design rather than library-first design.

### 2. Two-pass conversion model

The converter performs a preprocessing pass and a rendering pass.

- `Preprocessor` scans pages to collect per-font code usage and page size extremes.
- `HTMLRenderer` performs page rendering and HTML/CSS/font/background emission.

This is important: the project already understands that a single pass is insufficient if you want decent font extraction and layout preservation.

### 3. Rendering model

`HTMLRenderer` subclasses Poppler `OutputDev` and receives page/text/image/path callbacks during `PDFDoc::displayPage(...)` traversal.

At a high level, it:

- Tracks page state and text state changes.
- Emits text into positioned HTML lines/spans.
- Emits or embeds fonts.
- Emits background images/SVG for non-text content.
- Preserves links, outlines, and optionally forms.
- Uses a covered-text detector to avoid visually duplicated text in cases where text is obscured by drawings.

### 4. Text model

The text layer is assembled in two abstraction levels:

- `HTMLTextPage`: page-level collection of text lines and clip regions.
- `HTMLTextLine`: line-level store of characters, offsets, state changes, and absolute positioning.

This is a fidelity-first rendering approach. It is effective for visual preservation, but it naturally tends toward div/span-heavy output and only limited semantic structure.

### 5. Background and image model

The background pipeline is abstracted through `BackgroundRenderer`, with Cairo/Splash implementations. This is useful because it already separates text HTML from visual fallback/background rendering.

### 6. Build and dependency model

The project is built with CMake, but the build is tightly coupled to sibling checked-out source trees for Poppler and FontForge. That makes the current repo difficult to consume as a general-purpose Python package dependency.

### 7. Tests

The repository contains Python test scripts, but they are mostly **integration/system tests for the native binary**, not a modern Python package test suite. There is little evidence of unit-testable library boundaries in the core converter.

## What should be reused

### Reuse directly

1. **The Poppler `OutputDev`-based extraction/rendering strategy.**
   It is the heart of fidelity preservation and is the strongest technical asset in the repository.
2. **Text placement logic in `HTMLRenderer`, `HTMLTextPage`, and `HTMLTextLine`.**
   This logic encodes years of edge-case handling for glyph positioning, offsets, fonts, visibility, and browser rendering quirks.
3. **Background rendering abstraction.**
   The split between text-layer HTML and graphical background rendering should remain a core architectural concept.
4. **Covered text detection.**
   This is a nontrivial correctness feature that many simpler converters miss.
5. **Font extraction and export logic.**
   Font handling is one of the hardest parts of high-fidelity PDF-to-HTML. Reimplementing this in pure Python would be a major regression risk.
6. **Existing PDF fixtures and browser-style regression assets.**
   They are valuable seeds for a future fidelity test corpus.

### Reuse conceptually, but refactor

1. **The two-pass preprocessing/rendering pipeline.**
   Keep the concept, but redesign the interfaces around a Python-facing document pipeline.
2. **The CLI option surface.**
   Many flags are useful, but they should be regrouped into coherent configuration models rather than exposed as a long flat flag list.
3. **Split-page output support.**
   This is directly relevant for web embedding and long-document lazy loading.

## What should be rewritten

### Rewrite completely

1. **Python package structure.**
   There is currently no reusable Python API, no packaging metadata for a Python distribution, and no library-oriented module structure.
2. **Public configuration model.**
   The current `Param` approach is a native-global mutable struct optimized for a CLI program, not a stable SDK.
3. **Output post-processing and semantic enhancement layer.**
   The current HTML is primarily positioning-oriented. A production Python package needs a second-stage transformer that can infer paragraphs, headings, lists, tables, figures, reading order, and metadata when confidence is high.
4. **Documentation and examples.**
   The existing docs are project/build oriented, not package/user oriented.
5. **Test strategy.**
   Introduce unit tests, fixture-based conversion tests, HTML snapshot tests, and regression scoring for fidelity metrics.
6. **Build/distribution pipeline.**
   The current dependency model is not suitable for easy Python package installation.

### Rewrite substantially

1. **Orchestration and conversion lifecycle.**
   Replace the single large CLI-driven flow with composable stages such as parse -> extract -> analyze -> render -> enhance -> package assets.
2. **Error handling and diagnostics.**
   Production Python users need structured exceptions, warnings, conversion reports, and machine-readable diagnostics.
3. **Resource management model.**
   Temp files, fonts, page assets, manifests, and embedding policies should be managed via explicit asset stores rather than incidental filesystem writes.

## What should be wrapped

### Wrap as a transitional strategy

1. **The native converter executable or shared native adapter.**
   In phase 1, a Python package can wrap the native engine while introducing a clean Python API and artifact model.
2. **Existing rendering modes.**
   Expose fidelity-oriented modes like embedded output, split pages, SVG/bitmap background selection, and text-visibility handling through Python configuration.
3. **Current regression corpus.**
   Wrap current fixtures in pytest-based integration tests.

### Best wrapping approach

Prefer a **narrow native adapter** over a naive subprocess wrapper long term.

Recommended order:

1. **Phase 1**: controlled subprocess adapter around the current binary for rapid packaging and compatibility.
2. **Phase 2**: extract a native library boundary or stable JSON/manifest-based intermediate output contract.
3. **Phase 3**: optionally expose native bindings via `pybind11` or `nanobind` if operationally justified.

This avoids blindly exposing every legacy CLI flag while still reusing the rendering engine.

## Biggest technical risks

### 1. Reading order and semantics are not the same as visual fidelity

pdf2htmlEX is very good at reconstructing visual placement. That does **not** automatically produce semantically meaningful HTML for accessibility, SEO, responsive rendering, or clean DOM structure. Multi-column documents, sidebars, footnotes, rotated text, and tables can all look correct but have poor DOM reading order.

### 2. Table extraction is not a first-class abstraction in the current core

The existing engine preserves table appearance, but there is no obvious dedicated table model that would emit semantic `<table>` markup. Inferring tables reliably from PDF drawing/text primitives is hard and should be treated as an enhancement stage with confidence thresholds.

### 3. Build complexity and native dependency brittleness

The current CMake setup assumes tightly pinned local Poppler and FontForge builds. That is a serious operational risk for a Python package intended for production use across Linux/macOS/Windows or CI environments.

### 4. HTML quality vs exact fidelity tradeoffs

The closer you stay to exact PDF layout, the more absolutely positioned and CSS-heavy the HTML becomes. The more you push toward semantic HTML, the more risk you introduce of changing layout, reading order, pagination, and text flow.

### 5. Font licensing and embedding issues

Some PDFs reference external fonts or impose embedding restrictions. Any production package needs clear policy controls and diagnostics for font fallback, subsetting, licensing-sensitive behavior, and missing glyph handling.

### 6. Long-document performance

Large PDFs create large DOMs, large CSS/font payloads, and expensive browser rendering costs. A production package must support pagination, lazy page loading, asset deduplication, manifest-driven embedding, and progressive rendering.

### 7. Browser rendering differences

This project already contains browser tests for a reason: fidelity depends on browser layout behavior. Small CSS/transform changes can alter text overlap, clipping, and spacing.

## Is this the right foundation?

Yes — **for the rendering engine layer**.

No — **for the package/product layer**.

In practical terms:

- If the goal is **high-fidelity PDF-to-HTML rendering**, this repo is a strong starting point.
- If the goal is **clean semantic website HTML from arbitrary PDFs**, this repo alone is insufficient; you need additional analysis and enhancement layers.
- If the goal is **a production-grade Python package**, most of the packaging, API, lifecycle, and quality framework still needs to be built.

## Recommended target architecture

### Layer 1: Native fidelity engine

Responsibility:
- Parse PDF primitives.
- Extract fonts, text runs, links, images, annotations.
- Produce high-fidelity page model and visual assets.

Implementation:
- Reuse/refactor current C++ renderer.

### Layer 2: Intermediate representation (IR)

Responsibility:
- Represent pages, blocks, lines, spans, images, vector regions, links, tables, and assets in a stable machine-readable model.

Why this matters:
- It decouples native extraction from HTML generation.
- It makes testing easier.
- It allows multiple output strategies: fidelity HTML, semantic HTML, hybrid HTML.

### Layer 3: HTML renderers

Provide multiple output modes:

1. **Fidelity renderer**
   - Absolute positioning.
   - Background images/SVG.
   - Font embedding.
   - Best for exact reproduction.

2. **Hybrid renderer**
   - Preserve page boxes and difficult regions visually.
   - Upgrade obvious paragraphs/headings/lists/figures where confidence is high.

3. **Semantic-first renderer**
   - For simpler PDFs only.
   - Use flow layout where safe.
   - Fall back to fidelity blocks for hard regions.

### Layer 4: Enhancement analyzers

Independent analyzers should infer:
- reading order,
- multi-column segmentation,
- paragraph merging,
- heading detection,
- list detection,
- table candidates,
- figure/caption association,
- repeated header/footer detection.

These analyzers must be optional and confidence-driven.

### Layer 5: Python package API

Suggested modules:

- `converter.py`: main orchestration.
- `config.py`: typed configuration objects.
- `models/`: IR types and result artifacts.
- `native/`: adapter to the C++ engine.
- `renderers/`: fidelity/hybrid/semantic HTML renderers.
- `analysis/`: layout and semantic analyzers.
- `assets/`: asset store, embedding, deduplication.
- `cli.py`: command-line interface.

## File-by-file refactor direction

### Native code to retain and adapt

- `pdf2htmlEX/src/pdf2htmlEX.cc`
  - Keep only as legacy CLI bootstrap or replace with a small adapter entrypoint.
- `pdf2htmlEX/src/HTMLRenderer/*`
  - Core reuse candidate; refactor toward emitting a stable IR in addition to or instead of raw HTML.
- `pdf2htmlEX/src/HTMLTextPage.*`
  - Reuse concepts; extend for richer block/line/span abstractions.
- `pdf2htmlEX/src/HTMLTextLine.*`
  - Reuse low-level text state handling, but not as the final public HTML abstraction.
- `pdf2htmlEX/src/Preprocessor.*`
  - Reuse and extend to collect richer layout metadata.
- `pdf2htmlEX/src/BackgroundRenderer/*`
  - Retain with cleaner interfaces.

### Areas to de-emphasize or isolate

- legacy shell scripts under `buildScripts/`
- Debian/archive packaging under `archive/`
- old-style Python test harness under `pdf2htmlEX/test/old/`

### New Python package tree to add

Suggested top-level package:

- `src/pdf2htmlx/`
  - `__init__.py`
  - `api.py`
  - `cli.py`
  - `config.py`
  - `exceptions.py`
  - `models/ir.py`
  - `models/result.py`
  - `native/adapter.py`
  - `analysis/reading_order.py`
  - `analysis/columns.py`
  - `analysis/tables.py`
  - `analysis/semantics.py`
  - `renderers/fidelity.py`
  - `renderers/hybrid.py`
  - `assets/store.py`
  - `assets/embed.py`
  - `postprocess/html_cleanup.py`

## Recommended implementation roadmap

### Phase 1: Production wrapper foundation

- Build a Python package with typed config and result objects.
- Wrap current native converter in a controlled way.
- Standardize output directories and manifests.
- Add pytest integration tests on existing sample PDFs.
- Add a modern CLI.

### Phase 2: Introduce an intermediate representation

- Modify native layer to emit structured JSON/IR alongside assets.
- Rebuild HTML generation in Python or refactored native code against that IR.
- Add analyzer pipeline for reading order and semantic grouping.

### Phase 3: Hybrid semantic rendering

- Add paragraph/heading/list detection.
- Add table candidate detection with confidence scoring.
- Keep region-level visual fallbacks for unsafe transformations.

### Phase 4: Packaging hardening

- Produce wheels or containerized build workflows.
- Add performance benchmarks.
- Add deterministic regression corpus and fidelity scorecards.

## Honest limitations

A production system can get **very high fidelity**, but it cannot guarantee perfect semantic reconstruction for arbitrary PDFs because PDFs usually do not contain enough structural information.

Therefore the package should expose explicit output strategies:

- `fidelity`: preserve appearance first.
- `hybrid`: preserve appearance but upgrade structure when confidence is high.
- `semantic`: prefer semantic HTML where documents are simple enough.

That honesty should be built into both the API and the docs.

## Final recommendation

Use this repository as the **native rendering kernel**, not as the final product architecture.

The best path is a **hybrid redesign**:

- keep the native PDF understanding and fidelity-sensitive rendering pieces,
- introduce a stable intermediate model,
- build the Python package, CLI, docs, and tests around that model,
- add semantic enhancement as an optional higher-level phase rather than forcing it into the low-level renderer.
