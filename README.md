# ![pdf2htmlEX logo](https://pdf2htmlEX.github.io/pdf2htmlEX/images/pdf2htmlEX-64x64.png) pdf2htmlEX

`pdf2htmlEX` converts PDF documents into HTML while preserving searchable text, layout, fonts, and images as closely as possible to the original document.

This repository tracks a maintained fork of `pdf2htmlEX` with fixes and build tooling for current Poppler and FontForge combinations.

## What this package does

`pdf2htmlEX` is a command-line converter for turning PDFs into web-friendly HTML output:

- Preserves text as selectable/searchable HTML when possible.
- Extracts and embeds fonts used by the PDF.
- Renders non-text content, figures, and complex backgrounds.
- Supports single-file output or split-page output for dynamic loading.
- Includes options for outlines, annotations, forms, printing support, and background rendering.

In short: if you want to publish a PDF on the web without flattening everything into a giant image, this package is designed for that job.

## Why this fork exists

Compared with the original upstream project, this branch focuses on keeping the project buildable and useful with newer dependency versions, and includes improvements such as:

- bug fixes for edge cases,
- updated Cairo integration,
- out-of-source building support,
- more accurate handling of obscured and partially obscured text,
- transparent-text support improvements, and
- DPI clamping to avoid oversized rendered graphics.

## Install options

Because `pdf2htmlEX` depends on tightly matched Poppler and FontForge versions, installation is more specialized than a typical single-package build.

### Option 1: Use a prebuilt release

If you just want to use the converter, start with the project releases. The repository includes build tooling for distributing:

- Debian packages,
- AppImages, and
- OCI/Docker container images.

See the GitHub releases page for downloadable artifacts:

- <https://github.com/pdf2htmlEX/pdf2htmlEX/releases>

### Option 2: Build locally with the provided scripts

For most source installs, use the helper scripts in `buildScripts/`.

#### Debian/Ubuntu and other `apt`-based systems

From the repository root:

```bash
./buildScripts/buildInstallLocallyApt
```

This script installs build dependencies, downloads compatible Poppler and FontForge sources, builds them statically, then builds and installs `pdf2htmlEX`.

#### Alpine Linux

```bash
./buildScripts/buildInstallLocallyAlpine
```

#### Build details

If you want more detail on the build pipeline and release artifacts, read:

- `buildScripts/Readme.md`

## Basic usage

The command format is:

```bash
pdf2htmlEX [options] <input-filename> [output-filename]
```

### Simplest example

Convert `document.pdf` into `document.html` in the current directory:

```bash
pdf2htmlEX document.pdf
```

### Write to a specific HTML file

```bash
pdf2htmlEX report.pdf report.html
```

### Write output into another directory

```bash
pdf2htmlEX --dest-dir output report.pdf
```

## Common examples

### Convert only selected pages

```bash
pdf2htmlEX --first-page 3 --last-page 5 report.pdf excerpt.html
```

### Generate external assets instead of embedding everything

```bash
pdf2htmlEX --embed cfijo report.pdf report.html
```

The `--embed` string controls whether CSS, fonts, images, JavaScript, and outlines are embedded directly in the HTML or emitted as separate files.

### Split pages for dynamic loading

```bash
pdf2htmlEX --split-pages 1 --page-filename page%03d.html report.pdf index.html
```

This is useful when serving large documents one page at a time.

### Improve text-placement accuracy

```bash
pdf2htmlEX --font-size-multiplier 1 --zoom 25 input.pdf output.html
```

This combination is recommended in this fork when maximum layout accuracy matters and you can scale the resulting HTML in your own viewer.

### Handle obscured text more carefully

```bash
pdf2htmlEX --correct-text-visibility 2 input.pdf output.html
```

Modes are:

- `0`: no visibility calculations,
- `1`: fully occluded text goes to the background layer,
- `2`: partially occluded text can also be pushed into the background layer.

## Important options

Here are some of the most useful options when using the package.

### Page selection

- `--first-page <n>`: start conversion at a specific page.
- `--last-page <n>`: stop conversion at a specific page.

### Sizing and rendering

- `--zoom <ratio>`: scale the output directly.
- `--fit-width <px>` / `--fit-height <px>`: constrain page dimensions.
- `--hdpi <dpi>` / `--vdpi <dpi>`: control image rendering DPI.
- `--use-cropbox 0|1`: choose CropBox vs MediaBox.

### Output structure

- `--dest-dir <dir>`: choose an output directory.
- `--split-pages 0|1`: emit one file per page.
- `--page-filename <pattern>`: choose page file names.
- `--css-filename <name>`: set the CSS file name if CSS is external.
- `--outline-filename <name>`: set the outline file name if outlines are external.

### Features to include

- `--process-nontext 0|1`: include non-text objects.
- `--process-outline 0|1`: include bookmarks/outlines.
- `--process-annotation 0|1`: include annotations.
- `--process-form 0|1`: include PDF forms.
- `--printing 0|1`: enable or disable printing support.
- `--fallback 0|1`: use larger but more compatible output.

### Font handling

- `--font-format <format>`: font output format, default `woff`.
- `--embed-external-font 0|1`: embed matched local fonts when PDFs do not contain them.
- `--decompose-ligature 0|1`: expand ligatures such as `fi`.
- `--turn-off-ligatures 0|1`: discourage browser ligature substitution.
- `--auto-hint 0|1`: generate hints using FontForge.

### Text tuning

- `--space-threshold <ratio>`: adjust space insertion sensitivity.
- `--font-size-multiplier <ratio>`: work around browser font-size rounding.
- `--space-as-offset 0|1`: treat spaces as positioning offsets.
- `--tounicode <-1|0|1>`: control ToUnicode mapping behavior.
- `--optimize-text 0|1`: reduce the number of generated text elements.
- `--correct-text-visibility <0|1|2>`: improve obscured-text handling.

## Typical workflow

A practical workflow for using this package looks like this:

1. Install a packaged release, or build with `buildScripts/buildInstallLocallyApt`.
2. Run `pdf2htmlEX input.pdf` to create HTML.
3. Open the resulting HTML in a browser.
4. If layout fidelity is not good enough, retry with options like:
   - `--zoom 25 --font-size-multiplier 1`
   - `--fallback 1`
   - `--correct-text-visibility 1` or `2`
5. If the output should be served as a document viewer, consider `--split-pages 1` and external assets.

## Output files you should expect

Depending on the options you choose, `pdf2htmlEX` may generate:

- one standalone `.html` file,
- a main HTML file plus external `.css`, font, image, JavaScript, or outline files,
- one file per page when `--split-pages 1` is used.

If everything is embedded, distribution is easiest. If assets are external, caching and incremental loading can be better.

## Troubleshooting tips

- If output is visually inaccurate, try `--zoom 25 --font-size-multiplier 1`.
- If text that should be hidden is showing through, try `--correct-text-visibility 1` or `2`.
- If file size becomes too large, avoid high DPI unless necessary and consider externalized assets instead of fully embedded output.
- If fonts render incorrectly, experiment with `--font-format`, `--embed-external-font`, `--decompose-ligature`, and `--tounicode`.
- If you are building from source, prefer the repository's build scripts rather than attempting a manual system-library build.

## Additional documentation

- Build/install overview: `buildScripts/Readme.md`
- General contribution guidance: `CONTRIBUTING.md`
- License details: `LICENSE`
- Changelog/history: `ChangeLog`
- Test notes: `pdf2htmlEX/test/README.md`

## License

`pdf2htmlEX` as a whole is licensed under GPLv3+. Some bundled resources use more permissive licenses; see `LICENSE` for details.

## Quick reference

```bash
# Convert a PDF to HTML
pdf2htmlEX input.pdf

# Convert into a specific directory
pdf2htmlEX --dest-dir output input.pdf

# Convert only pages 1 through 10
pdf2htmlEX --first-page 1 --last-page 10 input.pdf partial.html

# Split each page into separate page files
pdf2htmlEX --split-pages 1 input.pdf output.html

# Prioritize rendering accuracy
pdf2htmlEX --zoom 25 --font-size-multiplier 1 input.pdf output.html
```
