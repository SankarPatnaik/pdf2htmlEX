#!/usr/bin/env python3
"""
Pipeline: SC_SRC_DATA_S3_OFFICIAL → PDF (S3) → HTML → S3 → SC_SRC_DATA_S3_AI_OFFICIAL

Routing logic (auto-detect per PDF, no script modifications):
  scanned / image-only  (total chars < 200)  →  pdf_to_semantic_html 1.py   (Tesseract OCR)
  digital / text-based  (total chars >= 200)  →  pdf_to_semantic_html_enhanced.py (block-aware merge)

Write policy: ONLY `s3_url_frontend` is written to the READ collection (SC_SRC_DATA_S3_OFFICIAL).
              No other field is created or modified.

Parallelism: ProcessPoolExecutor — each worker is a separate OS process, giving
             true CPU parallelism and bypassing Python's GIL for PDF conversion.

Usage:
    python pdf_to_html_pipeline.py                    # full run, 6 workers (M4 Pro sweet spot)
    python pdf_to_html_pipeline.py --workers 8        # try if network is faster
    python pdf_to_html_pipeline.py --limit 20         # process first 20 docs
    python pdf_to_html_pipeline.py --dry-run          # simulate without S3 upload or mongo write
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import multiprocessing
import os
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3
import fitz  # PyMuPDF — used for format-detection probe only
from dotenv import load_dotenv
from pymongo import MongoClient
from tqdm import tqdm

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Silence expected "Table detection failed on page N: not a textpage" warnings
# from converter scripts — these are harmless and spam the terminal for scanned PDFs.
logging.getLogger("pdf_to_semantic_html").setLevel(logging.ERROR)
logging.getLogger("pdf_s2").setLevel(logging.ERROR)
logging.getLogger("pdf_s3").setLevel(logging.ERROR)

# ── Constants ─────────────────────────────────────────────────────────────────
S3_BUCKET = "lawedcue-non-prod"
S3_HTML_PREFIX = "Supreme-Court-Html-Official-frontend"

# PDFs with fewer extractable chars than this are treated as scanned images.
# Real-world data: scanned JUDIS PDFs = 0 chars; digital PDFs = 949+ chars.
SCANNED_CHAR_THRESHOLD = 200

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PDF_TO_HTML_DIR = BASE_DIR / "pdf_to_html"


# ── Load converter scripts via importlib (zero modifications to either script) ─
def _load_module(alias: str, filepath: Path) -> Any:
    """Import a Python file by path, even if its filename contains spaces.

    The module must be registered in sys.modules before exec_module so that
    dataclasses (which call sys.modules[cls.__module__]) resolve correctly on
    Python 3.9.
    """
    spec = importlib.util.spec_from_file_location(alias, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from: {filepath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod  # register before exec so dataclass annotations resolve
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


log.info("Loading converter scripts...")
_script2 = _load_module("pdf_s2", PDF_TO_HTML_DIR / "pdf_to_semantic_html 1.py")
_script3 = _load_module("pdf_s3", PDF_TO_HTML_DIR / "pdf_to_semantic_html_enhanced.py")
log.info("Converters ready.")


# ── Per-process state (initialised once per worker via pool initializer) ──────
_worker_script2: Any = None
_worker_script3: Any = None
_worker_s3: Any = None
_worker_write_coll: Any = None
_worker_mongo_client: Any = None


def _worker_init(cfg: dict[str, str]) -> None:
    """
    Called once when each worker process starts.
    Loads converter modules and creates S3 + MongoDB connections in-process
    so they are not pickled across the process boundary.
    """
    global _worker_script2, _worker_script3
    global _worker_s3, _worker_write_coll, _worker_mongo_client

    # Silence converter warnings inside worker processes too
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    logging.getLogger("pdf_to_semantic_html").setLevel(logging.ERROR)

    base = Path(__file__).parent
    pdf_dir = base / "pdf_to_html"
    _worker_script2 = _load_module("pdf_s2", pdf_dir / "pdf_to_semantic_html 1.py")
    _worker_script3 = _load_module("pdf_s3", pdf_dir / "pdf_to_semantic_html_enhanced.py")

    _worker_s3 = boto3.client(
        "s3",
        aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"],
        region_name=cfg["AWS_DEFAULT_REGION"],
    )

    mongo_client = MongoClient(cfg["MONGO_URI"])
    _worker_mongo_client = mongo_client
    # Write s3_url_frontend back to the READ (source) collection
    _worker_write_coll = mongo_client[cfg["MONGO_DB"]][cfg["MONGO_COLLECTION_READ"]]


# ── Environment ───────────────────────────────────────────────────────────────
def load_config() -> dict[str, str]:
    load_dotenv(BASE_DIR / ".env")
    required = [
        "MONGO_URI",
        "MONGO_DB",
        "MONGO_COLLECTION_READ",
        "MONGO_COLLECTION_WRITE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
    ]
    cfg = {k: os.environ.get(k, "") for k in required}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required env variables: {missing}")
    return cfg


# ── Format detection ──────────────────────────────────────────────────────────
def _is_scanned(pdf_path: Path) -> bool:
    """Return True if the PDF is a scanned image (no/minimal extractable text)."""
    doc = fitz.open(str(pdf_path))
    total_chars = sum(len(doc[i].get_text()) for i in range(doc.page_count))
    doc.close()
    return total_chars < SCANNED_CHAR_THRESHOLD


# ── Conversion ────────────────────────────────────────────────────────────────
def convert_pdf(pdf_path: Path, out_dir: Path) -> Path:
    """
    Detect PDF format and route to the correct converter.
    Returns path to the generated document.html.
    """
    scanned = _is_scanned(pdf_path)
    module = _script2 if scanned else _script3
    converter = module.PDFSemanticHTMLConverter(pdf_path, out_dir)
    converter.convert()
    html_path = out_dir / "document.html"
    if not html_path.exists():
        raise FileNotFoundError(f"Converter produced no document.html in {out_dir}")
    return html_path


# ── Single-doc worker (runs in a worker process) ──────────────────────────────
def _process_one(doc: dict, dry_run: bool) -> dict:
    """
    Process one document end-to-end inside a worker process.
    Uses per-process globals set up by _worker_init.
    Returns: {file_id, status, detail}
      status: "ok" | "not_in_write_coll" | "dry_run" | "error"
    """
    file_id = doc["FileID"]
    s3_key_pdf = doc["s3_key_pdf"]
    html_s3_key = f"{S3_HTML_PREFIX}/{file_id}.html"
    s3_url_frontend = f"s3://{S3_BUCKET}/{html_s3_key}"

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            pdf_path = tmp / "input.pdf"
            html_out_dir = tmp / "html_out"
            html_out_dir.mkdir()

            # 1. Download PDF from S3
            _worker_s3.download_file(S3_BUCKET, s3_key_pdf, str(pdf_path))

            # 2. Detect format + convert
            scanned = _is_scanned(pdf_path)
            module = _worker_script2 if scanned else _worker_script3
            converter = module.PDFSemanticHTMLConverter(pdf_path, html_out_dir)
            converter.convert()
            html_path = html_out_dir / "document.html"
            if not html_path.exists():
                raise FileNotFoundError("Converter produced no document.html")

            if dry_run:
                fmt = "scanned" if scanned else "digital"
                return {"file_id": file_id, "status": "dry_run", "detail": f"{fmt} → {s3_url_frontend}"}

            # 3. Upload HTML to S3
            _worker_s3.upload_file(
                str(html_path),
                S3_BUCKET,
                html_s3_key,
                ExtraArgs={"ContentType": "text/html; charset=utf-8"},
            )

            # 4. Write ONLY s3_url_frontend to the source (read) collection — no other fields touched
            result = _worker_write_coll.update_one(
                {"FileID": file_id},
                {"$set": {"s3_url_frontend": s3_url_frontend}},
            )

            if result.matched_count == 0:
                return {"file_id": file_id, "status": "not_in_write_coll", "detail": s3_url_frontend}

            return {"file_id": file_id, "status": "ok", "detail": s3_url_frontend}

    except Exception as exc:
        return {"file_id": file_id, "status": "error", "detail": str(exc)}


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PDF → HTML pipeline for Supreme Court judgments.")
    p.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Parallel worker processes (default: 6, sweet spot on M4 Pro)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after processing this many docs (0 = no limit)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and convert but skip S3 upload and MongoDB write",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    args = build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        log.info("DRY-RUN mode — no S3 uploads, no MongoDB writes.")

    cfg = load_config()

    # ── Connect MongoDB ───────────────────────────────────────────────────────
    mongo_client = MongoClient(cfg["MONGO_URI"])
    db = mongo_client[cfg["MONGO_DB"]]
    read_coll = db[cfg["MONGO_COLLECTION_READ"]]
    write_coll = db[cfg["MONGO_COLLECTION_WRITE"]]

    # ── Step 1: build incremental skip set (from the READ collection itself) ─
    log.info("Reading already-processed FileIDs from read collection...")
    done_file_ids: set = {
        doc["FileID"]
        for doc in read_coll.find(
            {"s3_url_frontend": {"$exists": True}, "FileID": {"$exists": True}},
            {"FileID": 1},
        )
    }
    log.info("  already done: %d docs (will be skipped)", len(done_file_ids))

    # ── Step 2: fetch eligible docs ───────────────────────────────────────────
    query = {
        "skip_reason": None,
        "FileID": {"$exists": True},
        "s3_key_pdf": {"$exists": True},
    }
    projection = {"_id": 1, "FileID": 1, "s3_key_pdf": 1}

    log.info("Querying read collection (skip_reason=null, FileID exists, s3_key_pdf exists)...")
    eligible = list(read_coll.find(query, projection))
    log.info("  eligible: %d docs", len(eligible))

    to_process = [d for d in eligible if d["FileID"] not in done_file_ids]
    log.info("  to process after incremental filter: %d docs", len(to_process))

    if args.limit > 0:
        to_process = to_process[: args.limit]
        log.info("  capped to: %d docs (--limit %d)", len(to_process), args.limit)

    if not to_process:
        log.info("Nothing to process. Exiting.")
        mongo_client.close()
        return 0

    log.info("Starting parallel processing with %d worker processes...", args.workers)
    log.info("Press Ctrl+C to stop gracefully (in-flight docs will finish).")

    # ── Step 3: parallel processing loop ─────────────────────────────────────
    stats = {"ok": 0, "not_in_read_coll": 0, "failed": 0, "dry_run": 0}
    interrupted = False

    pool = ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_worker_init,
        initargs=(cfg,),
        mp_context=multiprocessing.get_context("spawn"),
    )
    futures = {
        pool.submit(_process_one, doc, args.dry_run): doc
        for doc in to_process
    }

    try:
        with tqdm(total=len(futures), desc="PDF→HTML", unit="doc") as pbar:
            for future in as_completed(futures):
                res = future.result()
                status = res["status"]
                file_id = res["file_id"]

                if status == "ok":
                    stats["ok"] += 1
                    log.debug("  ✓ FileID=%s", file_id)
                elif status == "dry_run":
                    stats["dry_run"] += 1
                    log.debug("  [DRY-RUN] FileID=%s  %s", file_id, res["detail"])
                elif status == "not_in_read_coll":
                    stats["not_in_read_coll"] += 1
                    log.warning("  FileID=%s — HTML uploaded but FileID not found in read collection", file_id)
                elif status == "error":
                    stats["failed"] += 1
                    doc = futures[future]
                    log.error(
                        "FAILED  FileID=%-12s  s3_key_pdf=%s  error=%s",
                        file_id, doc["s3_key_pdf"], res["detail"],
                    )

                pbar.update(1)
                pbar.set_postfix(ok=stats["ok"], fail=stats["failed"], refresh=False)

    except KeyboardInterrupt:
        interrupted = True
        log.warning("\nCtrl+C received — cancelling pending work, finishing in-flight docs...")
        for f in futures:
            f.cancel()

    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        mongo_client.close()

    if interrupted:
        log.info("Stopped early by user.")

    processed = stats["ok"] + stats["not_in_read_coll"] + stats["dry_run"]
    log.info(
        "Finished.  processed=%d  written_to_mongo=%d  not_in_read_coll=%d  failed=%d  skipped=%d",
        processed,
        stats["ok"],
        stats["not_in_read_coll"],
        stats["failed"],
        len(done_file_ids),
    )

    if stats["failed"] > 0:
        log.warning("%d doc(s) failed — check logs above.", stats["failed"])

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
