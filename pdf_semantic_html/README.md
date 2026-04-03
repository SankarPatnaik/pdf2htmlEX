# PDF Semantic HTML Scripts

This folder contains scripts to convert PDF files into semantic HTML.

## Scripts in this folder

- `pdf_to_semantic_html 2.py`  
  Base converter for text-based PDFs.
- `pdf_to_semantic_html_enhanced 1.py`  
  Enhanced converter with OCR-related options.
- `pdf_to_html_pipeline 1.py`  
  End-to-end batch pipeline (MongoDB + S3 based workflow).

## Prerequisites

1. Python 3.9+ (recommended).
2. Install Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install pymupdf boto3 pymongo python-dotenv tqdm
```

3. For OCR features (enhanced script), install Tesseract on your system.

## How to run

> Because filenames include spaces, always wrap script paths in quotes.

### 1) Run the base converter (single PDF)

```bash
python "pdf_semantic_html/pdf_to_semantic_html 2.py" /path/to/input.pdf -o /path/to/output_dir
```

Optional debug logging:

```bash
python "pdf_semantic_html/pdf_to_semantic_html 2.py" /path/to/input.pdf -o /path/to/output_dir --debug
```

### 2) Run the enhanced converter (single PDF)

```bash
python "pdf_semantic_html/pdf_to_semantic_html_enhanced 1.py" /path/to/input.pdf -o /path/to/output_dir
```

Common options:

```bash
python "pdf_semantic_html/pdf_to_semantic_html_enhanced 1.py" /path/to/input.pdf -o /path/to/output_dir --ocr-lang eng --ocr-dpi 300
python "pdf_semantic_html/pdf_to_semantic_html_enhanced 1.py" /path/to/input.pdf -o /path/to/output_dir --force-ocr
python "pdf_semantic_html/pdf_to_semantic_html_enhanced 1.py" /path/to/input.pdf -o /path/to/output_dir --disable-ocr
```

### 3) Run the batch pipeline

Create a `.env` file (in the same folder as `pdf_to_html_pipeline 1.py`) with:

```env
MONGO_URI=...
MONGO_DB=...
MONGO_COLLECTION_READ=...
MONGO_COLLECTION_WRITE=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=...
```

Then run:

```bash
python "pdf_semantic_html/pdf_to_html_pipeline 1.py"
```

Useful variants:

```bash
python "pdf_semantic_html/pdf_to_html_pipeline 1.py" --workers 6
python "pdf_semantic_html/pdf_to_html_pipeline 1.py" --limit 20
python "pdf_semantic_html/pdf_to_html_pipeline 1.py" --dry-run
python "pdf_semantic_html/pdf_to_html_pipeline 1.py" --verbose
```

## Output

For single-file conversion, output folder contains:

- `document.html`
- `assets/` (extracted images)
