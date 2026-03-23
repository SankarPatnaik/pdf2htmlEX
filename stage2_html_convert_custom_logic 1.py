import sys
import boto3
import logging
import fitz
import re
from pathlib import Path
from pymongo import MongoClient, UpdateOne
from datetime import datetime, timezone
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────
# Path Setup
# ─────────────────────────────────────────
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.config import (
    MONGO_URI, DB_NAME, COLLECTION_NAME, OLD_COLLECTION_NAME,
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION,
    S3_BUCKET, S3_HTML_FOLDER,
    BATCH_MONTH_FOLDER, STAGE2_WORKERS,
    COURT_NAME
)

# ─────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "stage2_html_convert.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

TEST_MODE_LIMIT = 0  # Set to >0 to limit records for testing

# ─────────────────────────────────────────
# S3 Client
# ─────────────────────────────────────────
def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION
    )

# ─────────────────────────────────────────
# FileID Management
# ─────────────────────────────────────────
def get_max_fileid(client) -> int:
    db = client[DB_NAME]

    old_col = db[OLD_COLLECTION_NAME]
    old_max = old_col.find_one(
        {"FileId": {"$exists": True}},
        sort=[("FileId", -1)]
    )


    cur_col = db[COLLECTION_NAME]
    cur_max = cur_col.find_one(
        {"FileID": {"$exists": True}},
        sort=[("FileID", -1)]
    )

    old_val = old_max["FileId"] if old_max else 0
    cur_val = cur_max["FileID"] if cur_max else 0

    return max(old_val, cur_val)

# ─────────────────────────────────────────
# PDF Text Extraction
# ─────────────────────────────────────────
def normalize_repeated_line(text: str) -> str:
    """Normalize line text so repeated header/footer detection is stable."""
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\b\d+\b', '#', text)
    return text.lower()


PAGE_MARKER_RE = re.compile(r'^Page\s+\d+\s+of\s+\d+$', re.IGNORECASE)
TRAILING_PAGE_MARKER_RE = re.compile(
    r'^(?:'
    r'(?:MAC\.APP|W\.P\.?|ARB\.P\.?|CS?\(?\w*\)?|CRL\.?|RFA?|FAO?|LPA?|OMP?|CW?|BAIL?)'
    r'[\w\s\-\.\/]*\s{2,}\d+'
    r')$',
    re.IGNORECASE,
)


def extract_line_entries(doc: fitz.Document) -> list:
    """Extract line-level text with geometry for later filtering and layout heuristics."""
    entries = []
    for page_num, page in enumerate(doc):
        page_dict = page.get_text("dict")
        page_width = page.rect.width
        page_height = page.rect.height
        for block_id, block in enumerate(page_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue
            for line_id, line in enumerate(block.get("lines", [])):
                spans = []
                parts = []
                max_size = 0
                is_bold = False
                is_italic = False
                min_x = float("inf")
                for span_id, span in enumerate(line.get("spans", [])):
                    span_text = span.get("text", "").strip()
                    if not span_text:
                        continue
                    parts.append(span_text)
                    size = span.get("size", 11)
                    max_size = max(max_size, size)
                    flags = span.get("flags", 0)
                    if flags & 16:
                        is_bold = True
                    if flags & 2:
                        is_italic = True
                    origin_x = span.get("origin", (0, 0))[0]
                    min_x = min(min_x, origin_x)
                    spans.append({
                        "span_id": span_id,
                        "text": span_text,
                        "bbox": tuple(span.get("bbox", (0, 0, 0, 0))),
                        "origin": tuple(span.get("origin", (0, 0))),
                        "size": size,
                        "flags": flags,
                        "font": span.get("font", ""),
                    })

                line_text = " ".join(parts).strip()
                if not line_text:
                    continue

                x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                entries.append({
                    "text": line_text,
                    "bbox": (x0, y0, x1, y1),
                    "page": page_num + 1,
                    "page_width": page_width,
                    "page_height": page_height,
                    "block_id": block_id,
                    "line_id": line_id,
                    "spans": spans,
                    "size": max_size,
                    "bold": is_bold,
                    "italic": is_italic,
                    "indent": round(min_x if min_x != float("inf") else x0),
                })
    return entries


def detect_repeated_margin_lines(entries: list, total_pages: int) -> set:
    """Detect repeated header/footer lines instead of dropping by geometry alone."""
    if total_pages < 2:
        return set()

    grouped = {}
    for entry in entries:
        text = entry["text"].strip()
        if not text or PAGE_MARKER_RE.match(text) or TRAILING_PAGE_MARKER_RE.match(text):
            continue

        x0, y0, x1, y1 = entry["bbox"]
        page_height = entry["page_height"]
        in_margin = y1 <= page_height * 0.08 or y0 >= page_height * 0.92
        if not in_margin:
            continue

        normalized = normalize_repeated_line(text)
        grouped.setdefault(normalized, {"pages": set(), "entries": []})
        grouped[normalized]["pages"].add(entry["page"])
        grouped[normalized]["entries"].append(entry)

    repeated = set()
    for data in grouped.values():
        if len(data["pages"]) >= 2:
            for entry in data["entries"]:
                repeated.add((entry["page"], entry["block_id"], entry["line_id"]))

    return repeated


def extract_text_from_pdf(pdf_bytes: bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = len(doc)
    entries = extract_line_entries(doc)
    repeated_margin_lines = detect_repeated_margin_lines(entries, total_pages)
    lines = []

    for entry in entries:
        key = (entry["page"], entry["block_id"], entry["line_id"])
        stripped = entry["text"].strip()
        if not stripped:
            continue
        if key in repeated_margin_lines:
            continue
        if PAGE_MARKER_RE.match(stripped):
            continue
        if TRAILING_PAGE_MARKER_RE.match(stripped):
            continue
        stripped = re.sub(r'\s{2,}', ' ', stripped).strip()
        if stripped:
            lines.append(stripped)

    doc.close()
    return lines, total_pages

# ─────────────────────────────────────────
# HC PDF Metadata Extraction
# ─────────────────────────────────────────
def extract_pdf_meta(lines: list, total_pages: int, source_doc: dict = None) -> dict:
    meta = {
        "citation"               : "",
        "neutral_citation_year"  : None,
        "neutral_citation_number": None,
        "judgment_date"          : "",
        "reserved_on"            : "",
        "case_number"            : "",
        "appellant"              : "",
        "respondent"             : "",
        "advocates"              : {"petitioner": [], "respondent": []},
        "bench"                  : [],
        "is_division_bench"      : False,
        "judgment_by"            : "",
        "is_oral_judgment"       : False,
        "via_video_conferencing" : False,
        "total_pages"            : total_pages,
        "referred_cases"         : [],
        "acts_referred"          : [],
        "is_connected_matter"    : False,
        "lead_case"              : ""
    }

    header_lines = lines[:60]
    full_text    = " ".join(lines)
    # Strip $~ / #~ annotation lines from header
    clean_header_lines = [
        l for l in header_lines
        if not re.match(r'^\s*[\$#~]', l)
    ]
    clean_header_text = " ".join(clean_header_lines)

    # ── 1. DHC Citation ──────────────────────────────────────────────────────
    citation_match = re.search(r'\d{4}:DHC:\d+(?:-DB)?', clean_header_text)
    if citation_match:
        citation = citation_match.group(0)
        meta["citation"] = citation
        parts = citation.split(":")
        if len(parts) >= 3:
            try:
                meta["neutral_citation_year"]   = int(parts[0])
                meta["neutral_citation_number"] = int(parts[2].replace("-DB", ""))
            except ValueError:
                pass
        if citation.endswith("-DB"):
            meta["is_division_bench"] = True

    # ── 2. Case Number — Excel ALWAYS wins, PDF is fallback only ─────────────
    case_from_excel = ""
    if source_doc:
        raw_case = (source_doc.get("Case No.") or "").strip()
        # Strip trailing neutral citation: "RFA-411/2021 2022:DHC:377" → "RFA-411/2021"
        case_from_excel = re.sub(
            r'\s+\d{4}:\w+:\d+(?:-DB)?\s*$', '', raw_case
        ).strip()

    if case_from_excel:
        meta["case_number"] = case_from_excel
    else:
        # PDF fallback — scan clean header lines
        for line in clean_header_lines:
            cn_match = re.match(
                r'^[+\*%]?\s*((?:W\.P\.\s*\((?:C|CRL)\)|CM\s*\(M\)|CS\s*\(OS\)|'
                r'CS\s*\(COMM\)|CRL\.A\.|CRL\.M\.C\.|CRL\.REV\.P\.|'
                r'RFA|RSA|FAO|ARB\.P\.|ARB\.A\.|O\.M\.P\.|'
                r'BAIL\s+APPLN\.|MAT\.APP\.|MAC\.APP\.|LPA|ITA|EFA|'
                r'EX\.P\.|TEST\.CAS\.|CW|CONMT\.|CRL\.M\.A\.|'
                r'W\.P\.Crl\.|C\.R\.P\.|CM\b)'
                r'[\w\s\(\)\.\/\-\&,]+)',
                line.strip(), re.IGNORECASE
            )
            if cn_match:
                meta["case_number"] = cn_match.group(1).strip()
                break

    # ── 3. Judgment Date ─────────────────────────────────────────────────────
    date_patterns = [
        r'(?:Judgment\s+(?:pronounced|delivered)\s+on\s*[:\-]\s*)(.+?)(?:\n|$)',
        r'(?:Date\s+of\s+(?:decision|Decision|Pronouncement)\s*[:\-]\s*)(.+?)(?:\n|$)',
        r'(?:Decided\s+on\s*[:\-]\s*)(.+?)(?:\n|$)',
        r'(?:Pronounced\s+on\s*[:\-]\s*)(.+?)(?:\n|$)',
        r'(?:Date\s*[:\-]\s*)(\d{1,2}[\.\-/]\d{1,2}[\.\-/]\d{4})(?:\s|$)',
    ]
    for line in clean_header_lines:
        for pat in date_patterns:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                raw_dt = m.group(1).strip().rstrip(".").lstrip("- –").strip()
                if raw_dt:
                    meta["judgment_date"] = raw_dt
                    break
        if meta["judgment_date"]:
            break

    # Fallback: Excel date
    if not meta["judgment_date"] and source_doc:
        raw_date = (source_doc.get("Date of Judgment/Order") or "").strip()
        if raw_date:
            meta["judgment_date"] = raw_date

    # Strip any leading dash artifact
    meta["judgment_date"] = re.sub(r'^[\-–]\s*', '', meta["judgment_date"]).strip()

    # ── 4. Reserved On ───────────────────────────────────────────────────────
    reserved_patterns = [
        r'(?:Judgment\s+[Rr]eserved\s+on\s*[:\-]\s*)(.+?)(?:\n|$)',
        r'(?:Reserved\s+on\s*[:\-]\s*)(.+?)(?:\n|$)',
        r'(?:Heard\s+on\s*[:\-]\s*)(.+?)(?:\n|$)',
    ]
    for line in clean_header_lines:
        for pat in reserved_patterns:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                meta["reserved_on"] = m.group(1).strip().rstrip(".")
                break
        if meta["reserved_on"]:
            break

    # ── 5. Appellant & Respondent ────────────────────────────────────────────
    for line in clean_header_lines:
        stripped = line.strip()
        if not meta["appellant"]:
            m = re.match(
                r'^([A-Z][A-Z0-9\s\.\,\&\/\(\)\-]+?)\s*\.{2,}\s*'
                r'(?:Petitioner|Petitioners|Appellant|Appellants)\s*$',
                stripped, re.IGNORECASE
            )
            if m:
                cand = m.group(1).strip()
                if 2 <= len(cand) <= 120:
                    meta["appellant"] = cand

        if not meta["respondent"]:
            m = re.match(
                r'^([A-Z][A-Z0-9\s\.\,\&\/\(\)\-]+?)\s*\.{2,}\s*'
                r'(?:Respondent|Respondents|Opposite\s+Party|Opposite\s+Parties)\s*$',
                stripped, re.IGNORECASE
            )
            if m:
                cand = m.group(1).strip()
                if 2 <= len(cand) <= 120:
                    meta["respondent"] = cand

        if meta["appellant"] and meta["respondent"]:
            break

    # Fallback: Excel Party field
    if (not meta["appellant"] or not meta["respondent"]) and source_doc:
        party = source_doc.get("Party", "")
        for sep in [" Vs ", " VS ", " vs ", " v. ", " V. "]:
            if sep in party:
                parts = party.split(sep, 1)
                if not meta["appellant"] and parts[0].strip():
                    meta["appellant"] = parts[0].strip()
                if not meta["respondent"] and parts[1].strip():
                    meta["respondent"] = parts[1].strip()
                break

    # ── Connected Matters Detection ──────────────────────────────────────────
    CONNECTED_SIGNALS = [
        r'connected\s+matters',
        r'batch\s+matters',
        r'tagged\s+matters',
        r'clubbed?\s+(?:with|matters)',
        r'analogous\s+matters',
    ]
    case_num_count = len(re.findall(
        r'\b(?:CS\s*\(OS\)|CS\s*\(COMM\)|WP\(C\)|CRL|RFA|RSA|FAO|ARB|OMP|CW)'
        r'[\s\-\.]*\d+[\s\/]\d+',
        clean_header_text, re.IGNORECASE
    ))
    for signal in CONNECTED_SIGNALS:
        if re.search(signal, clean_header_text, re.IGNORECASE):
            meta["is_connected_matter"] = True
            break
    if not meta["is_connected_matter"] and case_num_count >= 5:
        meta["is_connected_matter"] = True
    if meta["is_connected_matter"]:
        lead_match = re.search(
            r'((?:RFA|CS\s*\(OS\)|CS\s*\(COMM\)|WP\(C\)|CRL\.\w+|'
            r'ARB\.\w+|OMP|FAO|RSA|LPA)[\s\-\.]*[\w\/\-\s]+?\d{4})',
            clean_header_text, re.IGNORECASE
        )
        if lead_match:
            meta["lead_case"] = lead_match.group(1).strip()

    # ── 6. Bench — CORAM block ───────────────────────────────────────────────
    # Prefix list — order matters (longest first to avoid partial matches)
    BENCH_PREFIXES = [
        "HON'BLE MR. JUSTICE",
        "HON'BLE MR JUSTICE",
        "HON'BLE MS. JUSTICE",
        "HON'BLE MS JUSTICE",
        "HON'BLE MRS. JUSTICE",
        "HON'BLE MRS JUSTICE",
        "HON'BLE DR. JUSTICE",
        "HON'BLE DR JUSTICE",
        "HON'BLE THE CHIEF JUSTICE",
        "HON'BLE CHIEF JUSTICE",
        "HON'BLE",
        "MR. JUSTICE",
        "MR JUSTICE",
        "MS. JUSTICE",
        "MS JUSTICE",
        "JUSTICE",
        "THE CHIEF JUSTICE",
    ]
    # Compile a single stripping regex — strips any known prefix + optional space
    PREFIX_RE = re.compile(
        r'^(?:' +
        '|'.join(re.escape(p) for p in BENCH_PREFIXES) +
        r')[\s\.]*',
        re.IGNORECASE
    )

    coram_idx = None
    for i, line in enumerate(clean_header_lines):
        if re.search(r'\bCORAM\s*:?', line, re.IGNORECASE):
            coram_idx = i
            break

    if coram_idx is not None:
        for line in clean_header_lines[coram_idx + 1: coram_idx + 10]:
            raw = line.strip().rstrip(",").strip()
            if not raw:
                continue
            upper = raw.upper()
            # Stop at JUDGMENT / ORDER heading
            if re.match(r'^J\s*U\s*D\s*G\s*M\s*E\s*N\s*T$', upper) or upper in ("JUDGMENT", "ORDER"):
                break
            # Skip lines that are clearly not judge names
            if re.match(r'^(THROUGH|FOR\s+THE|VERSUS|V\.S\.)', upper):
                break
            # Strip prefix using regex
            judge_name = PREFIX_RE.sub('', raw).strip().rstrip(",").strip()
            if judge_name and len(judge_name) >= 3:
                meta["bench"].append(judge_name.upper())

    if len(meta["bench"]) >= 2:
        meta["is_division_bench"] = True

    # ── 7. Judgment By ───────────────────────────────────────────────────────
    JBY_RE = re.compile(
        r'^([A-Z][A-Z\s\'\.]+),\s*J\.?\s*(?:\(ORAL\))?'
        r'(?:\s*\[VIA\s+VIDEO\s+CONFERENCING\])?$',
        re.IGNORECASE
    )

    judgment_start_idx = None
    for i, line in enumerate(lines):
        u = line.strip().upper()
        if re.match(r'^J\s*U\s*D\s*G\s*M\s*E\s*N\s*T$', u) or u in ("JUDGMENT", "ORDER"):
            judgment_start_idx = i
            break

    if judgment_start_idx is not None:
        for line in lines[judgment_start_idx: judgment_start_idx + 10]:
            m = JBY_RE.match(line.strip())
            if m:
                meta["judgment_by"] = m.group(1).strip().upper()
                break

    # Fallback: last 20 lines
    if not meta["judgment_by"]:
        for line in lines[-20:]:
            m = JBY_RE.match(line.strip())
            if m:
                meta["judgment_by"] = m.group(1).strip().upper()
                break

    # ── 8. Oral / Video ──────────────────────────────────────────────────────
    if re.search(r'J\.\s*\(Oral\)|\(ORAL\)', full_text, re.IGNORECASE):
        meta["is_oral_judgment"] = True
    if re.search(r'\bVIA\s+VIDEO\s+CONFERENCING\b', full_text, re.IGNORECASE):
        meta["via_video_conferencing"] = True

    # ── 9. Advocates ─────────────────────────────────────────────────────────
    def parse_advocates(raw: str) -> list:
        raw = re.sub(r'\s+for\s+(?:respondent|petitioner|appellant|opposite\s+party)\s*[\w\s\.]*',
                     '', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\b(?:Senior\s+Advocate|Sr\.?\s*Adv\.?|Advocate|Adv\.?)\b',
                     '', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\s+with\s+', ', ', raw, flags=re.IGNORECASE)
        names = [n.strip().rstrip('.') for n in re.split(r',\s*', raw) if n.strip()]
        names = [n for n in names if re.match(r'^(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s*\S', n)]
        return names[:8]  # cap at 8 per side

    through_blocks = list(re.finditer(
        r'Through\s*:\s*(.+?)(?=(?:Through\s*:|versus|Versus|VERSUS|\.{3,}|CORAM\b|$))',
        clean_header_text, re.IGNORECASE | re.DOTALL
    ))
    if len(through_blocks) >= 1:
        meta["advocates"]["petitioner"] = parse_advocates(through_blocks[0].group(1))
    if len(through_blocks) >= 2:
        meta["advocates"]["respondent"] = parse_advocates(through_blocks[1].group(1))

    # Also handle "For the Petitioner/Respondent:" style
    if not meta["advocates"]["petitioner"] or not meta["advocates"]["respondent"]:
        for_pet = re.search(
            r'For\s+the\s+Petitioner\s*:\s*(.+?)(?=For\s+the\s+Respondent|CORAM\b|$)',
            clean_header_text, re.IGNORECASE | re.DOTALL)
        for_res = re.search(
            r'For\s+the\s+Respondent\s*:\s*(.+?)(?=For\s+the\s+Petitioner|CORAM\b|$)',
            clean_header_text, re.IGNORECASE | re.DOTALL)
        if for_pet and not meta["advocates"]["petitioner"]:
            meta["advocates"]["petitioner"] = parse_advocates(for_pet.group(1))
        if for_res and not meta["advocates"]["respondent"]:
            meta["advocates"]["respondent"] = parse_advocates(for_res.group(1))

    # ── 10. Referred Cases ───────────────────────────────────────────────────
    # Scan body only (skip header) to avoid watermark self-citation
    body_text = " ".join(lines[60:]) if len(lines) > 60 else ""

    REF_PATTERN = re.compile(
        r'AIR\s+\d{4}\s+[A-Z]{2,6}\s+\d+|'           # AIR 2005 SC 1234
        r'\(\d{4}\)\s+\d+\s+SCC\s+\d+|'              # (2013) 1 SCC 641
        r'\(\d{4}\)\s+\d+\s+SCR\s+\d+|'              # (2005) 7 SCR 234
        r'\[\d{4}\]\s+\d+\s+SCC\s+\d+|'              # [2013] 1 SCC 641
        r'\d{4}:\w{2,8}:\d+(?:-DB)?',                 # 2022:DHC:377
    )
    refs = set(r.strip() for r in REF_PATTERN.findall(body_text))
    # Always discard own citation — watermark bleeds into every page
    own_cit = meta["citation"]
    refs.discard(own_cit)
    # Discard any citation that is only numeric year (artifact)
    refs = {r for r in refs if not re.match(r'^\d{4}$', r)}
    meta["referred_cases"] = sorted(refs)

    # ── 11. Acts Referred ────────────────────────────────────────────────────
    # Match: "Word Word Act, 1234" or "Word Word Code" etc.
    # Strict: must start with capital, end cleanly, max 80 chars, min 2 words before keyword
    ACTS_RE = re.compile(
        r'\b([A-Z][a-zA-Z]{1,40}'
        r'(?:\s+[A-Za-z\(\)]{1,40}){0,6}'
        r'\s+(?:Act|Code|Rules|Regulations|Ordinance)'
        r'(?:,\s*\d{4})?)\b'
    )
    ACTS_NOISE = re.compile(
        r'(?:application\s+under|in\s+accordance|pursuant\s+to|'
        r'provisions?\s+of\s+the|under\s+the\s+said|'
        r'terms\s+of\s+the|filed\s+an?\s+|considering\s+an?\s+|'
        r'submitted\s+that|present\s+(?:case|petition)|'
        r'section\s+\d+\s+of\s+the|order\s+xi|order\s+vii|'
        r'^the\s+said|^an?\s+application|^while\s+|^further\s+|'
        r'^learned\s+|^on\s+the\s+other)',
        re.IGNORECASE
    )
    VALID_ACT_WORDS = re.compile(
        r'\b(?:Act|Code|Rules|Regulations|Ordinance)\b', re.IGNORECASE
    )

    acts_raw = ACTS_RE.findall(full_text)
    acts_clean = set()
    for a in acts_raw:
        a = a.strip()
        if len(a) < 8 or len(a) > 80:
            continue
        if not VALID_ACT_WORDS.search(a):
            continue
        if ACTS_NOISE.search(a):
            continue
        # Must have at least one capitalised word before the keyword
        words = a.split()
        if len(words) < 2:
            continue
        # Reject if first word is a stop/noise word
        if words[0].lower() in {'the','an','a','this','that','such','said','present',
                                  'while','further','on','in','under','as','by','to',
                                  'for','of','at','its','any','no','not','be','is',
                                  'was','has','have','had','been','being','learned',
                                  'while','considering','filed','submitted','order'}:
            continue
        acts_clean.add(a)

    meta["acts_referred"] = sorted(acts_clean)

    return meta

# ─────────────────────────────────────────
# HTML Builder — Structured Frontend-Ready (PyMuPDF)
# ─────────────────────────────────────────
CSS = """<style>
  body {
    font-family: 'Georgia', serif;
    font-size: 16px;
    line-height: 1.8;
    color: #1a1a1a;
    background: #f5f5f5;
    margin: 0;
    padding: 0;
  }
  .judgment-wrapper {
    max-width: 860px;
    margin: 40px auto;
    padding: 40px 48px;
    background: #fff;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
  }
  .judgment-header {
    text-align: center;
    border-bottom: 2px solid #222;
    padding-bottom: 16px;
    margin-bottom: 24px;
  }
  .court-name {
    font-size: 1.1em;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .citation-bar {
    font-size: 0.9em;
    color: #555;
    margin-top: 6px;
  }
  .case-title {
    text-align: center;
    margin: 24px 0;
    padding-bottom: 16px;
    border-bottom: 1px solid #ddd;
  }
  .case-number {
    font-size: 0.95em;
    font-weight: bold;
    color: #333;
    margin-bottom: 10px;
  }
  .parties {
    font-size: 1.05em;
    font-weight: bold;
    line-height: 1.6;
  }
  .vs-divider {
    font-style: italic;
    color: #666;
    font-size: 0.9em;
    margin: 4px 0;
  }
  .coram {
    margin: 20px 0;
    padding: 12px 16px;
    background: #f9f9f9;
    border-left: 3px solid #555;
    font-size: 0.95em;
  }
  .coram-label {
    font-weight: bold;
    text-transform: uppercase;
    font-size: 0.85em;
    letter-spacing: 0.05em;
    color: #444;
    margin-bottom: 4px;
  }
  .appearances {
    margin: 20px 0;
    font-size: 0.92em;
    color: #333;
    padding: 12px 16px;
    background: #fafafa;
    border: 1px solid #eee;
  }
  .appearances-label {
    font-weight: bold;
    font-size: 0.85em;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 6px;
    color: #444;
  }
  .judgment-body {
    margin-top: 28px;
  }
  .para {
    margin: 0 0 16px 0;
    text-align: justify;
  }
  .para-block {
    margin-bottom: 8px;
  }
  .para-num {
    font-weight: bold;
    margin-right: 6px;
    color: #222;
  }
  blockquote {
    margin: 16px 0 16px 32px;
    padding: 10px 16px;
    border-left: 3px solid #aaa;
    color: #444;
    font-style: italic;
    background: #fafafa;
  }
  .order-section {
    margin-top: 32px;
    padding-top: 16px;
    border-top: 2px solid #222;
  }
  .order-heading {
    text-transform: uppercase;
    font-size: 1em;
    font-weight: bold;
    letter-spacing: 0.06em;
    margin-bottom: 12px;
    color: #111;
  }
  .judgment-footer {
    margin-top: 40px;
    text-align: right;
    font-style: italic;
    color: #333;
    border-top: 1px solid #ddd;
    padding-top: 16px;
    font-size: 0.95em;
  }
  .page-break {
    border: none;
    border-top: 1px dashed #ccc;
    margin: 24px 0;
  }
</style>"""


def extract_blocks_from_pdf(pdf_bytes: bytes) -> list:
    """Extract text lines with geometry and font metadata using PyMuPDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    entries = extract_line_entries(doc)
    repeated_margin_lines = detect_repeated_margin_lines(entries, len(doc))
    blocks = []

    for entry in entries:
        line_text = entry["text"]
        key = (entry["page"], entry["block_id"], entry["line_id"])
        if not line_text:
            continue
        if "qrserver.com" in line_text or re.match(r'^\s*[\$#~]', line_text):
            continue
        if re.match(r'^\d{4}:\w+:\d+(?:-DB)?$', line_text):
            continue
        if PAGE_MARKER_RE.match(line_text) or TRAILING_PAGE_MARKER_RE.match(line_text):
            continue
        if key in repeated_margin_lines:
            continue
        blocks.append(entry)

    doc.close()
    return blocks


def classify_block(block: dict, base_indent: float) -> str:
    """Classify a block into a document zone."""
    text  = block["text"].strip()
    upper = text.upper()

    if re.search(r'HIGH COURT|SUPREME COURT', upper):
        return "header"
    if re.match(r'^CORAM\s*:?', upper):
        return "coram_label"
    if re.match(r"^HON'?BLE|^JUSTICE\b|^MR\.\s*JUSTICE|^MS\.\s*JUSTICE", upper):
        return "coram_judge"
    if re.match(r'^(THROUGH\s*:|FOR\s+THE\s+(PETITIONER|RESPONDENT|APPELLANT)|APPEARANCES?)', upper):
        return "appearances"
    if re.match(r'^(ORDER|JUDGMENT|J\s*U\s*D\s*G\s*M\s*E\s*N\s*T)\s*$', upper):
        return "order_heading"
    if re.match(r'^\d+[\.\)]\s+\S', text):
        return "para"
    if re.match(r'^[A-Z][A-Z\s\'\.]+,\s*J\.?\s*(\(ORAL\))?$', text, re.IGNORECASE):
        return "judge_name"   # ← neutral zone, NOT footer yet
    if re.match(r'^(Date\s+of\s+Decision|Pronounced\s+on|Decided\s+on)', text, re.IGNORECASE):
        return "header"
    if block["indent"] > base_indent + 60 and block["italic"]:
        return "quote"   # indented AND italic → definite quote
    if block["indent"] > base_indent + 80:
        return "quote"   # very heavily indented alone → quote
    return "body"


def build_html(file_id: int, pdf_bytes: bytes, pdf_meta: dict = None) -> str:
    """Build structured, styled, frontend-ready HTML from raw PDF bytes."""
    from collections import Counter

    blocks = extract_blocks_from_pdf(pdf_bytes)
    if not blocks:
        return (
            f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            f"<title>FileID {file_id}</title></head>"
            f"<body><p>No content extracted for FileID {file_id}</p></body></html>"
        )

    indent_counts = Counter(b["indent"] for b in blocks)
    base_indent   = indent_counts.most_common(1)[0][0]

    pm            = pdf_meta or {}
    citation      = pm.get("citation", "")
    judgment_date = pm.get("judgment_date", "")
    case_number   = pm.get("case_number", "")
    appellant     = pm.get("appellant", "")
    respondent    = pm.get("respondent", "")
    bench         = pm.get("bench", [])

    classified = [(b, classify_block(b, base_indent)) for b in blocks]

    header_lines     = []
    coram_judges     = []
    appearance_lines = []
    body_items       = []
    order_lines      = []
    footer_lines     = []
    judge_name_lines = []   # ← staging area, classified later by position
    in_order         = False
    prev_page        = 1


    for block, zone in classified:
        if block["page"] != prev_page:
            if not in_order:
                body_items.append(("pagebreak", "", ""))
            prev_page = block["page"]

        if zone == "header":
            header_lines.append(block["text"])
        elif zone in ("coram_label", "coram_judge"):
            if zone == "coram_judge":
                coram_judges.append(block["text"])
        elif zone == "appearances":
            appearance_lines.append(block["text"])
        elif zone == "order_heading":
            in_order = True
            order_lines.append(("heading", block["text"]))  # ← tuple
        elif zone == "judge_name":
            judge_name_lines.append(block["text"])   # ← stage, don't place yet

        elif zone == "para":
            m = re.match(r'^(\d+[\.\)])\s+(.*)', block["text"], re.DOTALL)
            if m:
                body_items.append(("para", m.group(1), m.group(2)))
            else:
                body_items.append(("body", "", block["text"]))
        elif zone == "quote":
            if in_order:
                order_lines.append(("quote", block["text"]))  # ← tuple
            else:
                body_items.append(("quote", "", block["text"]))
        elif zone == "body":
            if in_order:
                order_lines.append(("body", block["text"]))   # ← tuple
            else:
                body_items.append(("body", "", block["text"]))

    display_bench = coram_judges if coram_judges else bench

    # ── Positional judge name classifier ─────────────────────────────────────
    # Rule: judge name BEFORE Para 1 → body opener
    #       judge name AFTER last para → footer signature
    body_opener_line = None

    # Find index of first numbered para in order_lines raw list
    first_para_order_idx = next(
        (i for i, (lt, txt) in enumerate(order_lines)
         if lt != "heading" and re.match(r'^\d+[\.\)]', txt.strip())),
        None
    )

    for jline in judge_name_lines:
        has_oral = bool(re.search(r'\(oral\)', jline, re.IGNORECASE))
        if has_oral:
            # Always body opener regardless of position
            body_opener_line = jline
        elif first_para_order_idx is not None and first_para_order_idx <= 2:
            # Immediately after JUDGMENT heading → body opener
            # Only set once — first judge name wins
            if not body_opener_line:
                body_opener_line = jline
        else:
            # After paragraphs → footer signature
            footer_lines.append(jline)


    # ── KEY FIX: Merge order_lines into proper paragraphs ────────────────────
    def merge_into_paragraphs(raw_lines: list) -> list:
        paragraphs   = []
        current_num  = None
        current_text = []

        def flush():
            if current_text:
                merged = " ".join(current_text).strip()
                merged = re.sub(r'\s{2,}', ' ', merged).strip()
                if merged:
                    if current_num:
                        paragraphs.append(("para", current_num, merged))
                    else:
                        paragraphs.append(("body", "", merged))

        for line_type, text in raw_lines:
            if line_type == "heading":
                flush()
                current_num  = None
                current_text = []
                paragraphs.append(("heading", "", text))
                continue

            if line_type == "quote":
                flush()
                current_text = []
                paragraphs.append(("quote", "", text))
                continue

            # Standalone para number e.g. "1." or "12."
            if re.match(r'^\d+[\.\)]$', text.strip()):
                flush()
                current_num  = text.strip()
                current_text = []
                continue

            # Line that starts with para number e.g. "1. The hearing..."
            m = re.match(r'^(\d+[\.\)])\s+(.*)', text.strip())
            if m:
                flush()
                current_num  = m.group(1)
                current_text = [m.group(2)]
                continue

            # Regular body line — append to current paragraph
            current_text.append(text.strip())

        flush()
        return paragraphs

    # ── Assemble HTML ─────────────────────────────────────────────────────────
    h = []
    h.append("<!DOCTYPE html>")
    h.append("<html lang='en'>")
    h.append("<head>")
    h.append('  <meta charset="UTF-8">')
    h.append('  <meta name="viewport" content="width=device-width, initial-scale=1.0">')
    h.append(f"  <title>Judgment — {citation or f'FileID {file_id}'}</title>")
    h.append(CSS)
    h.append("</head>")
    h.append("<body>")
    h.append(f'<div class="judgment-wrapper" id="fileid-{file_id}">')

    # Header
    h.append('<div class="judgment-header">')
    h.append('  <div class="court-name">In The High Court of Delhi at New Delhi</div>')
    cite_bar = ""
    if citation:
        cite_bar += f"Citation: <strong>{citation}</strong>"
    if judgment_date:
        cite_bar += f" &nbsp;|&nbsp; Date: {judgment_date}"
    if cite_bar:
        h.append(f'  <div class="citation-bar">{cite_bar}</div>')
    h.append('</div>')

    # Case Title
    h.append('<div class="case-title">')
    if case_number:
        h.append(f'  <div class="case-number">{case_number}</div>')
    if appellant:
        h.append(f'  <div class="parties">{appellant}</div>')
    if appellant and respondent:
        h.append('  <div class="vs-divider">versus</div>')
    if respondent:
        h.append(f'  <div class="parties">{respondent}</div>')
    h.append('</div>')

    # CORAM
    if display_bench:
        h.append('<div class="coram">')
        h.append('  <div class="coram-label">Coram</div>')
        for judge in display_bench:
            h.append(f"  <div>{judge}</div>")
        h.append('</div>')

    # Appearances
    if appearance_lines:
        h.append('<div class="appearances">')
        h.append('  <div class="appearances-label">Appearances</div>')
        for a in appearance_lines:
            h.append(f"  <div>{a}</div>")
        h.append('</div>')

    # Judgment Body (pre-order)
    h.append('<div class="judgment-body">')
    for item in body_items:
        if item[0] == "pagebreak":
            h.append('<hr class="page-break">')
        elif item[0] == "para":
            num     = item[1]
            text    = item[2]
            para_id = re.sub(r'\D', '', num)
            h.append(
                f'<p class="para" id="para-{para_id}">'
                f'<span class="para-num">{num}</span> {text}</p>'
            )
        elif item[0] == "quote":
            h.append(f'<blockquote>{item[2]}</blockquote>')
        else:
            h.append(f'<p class="para">{item[2]}</p>')
    h.append('</div>')

    # Order / Judgment Section — now with merged paragraphs
    if order_lines:
        merged_paras = merge_into_paragraphs(order_lines)
        h.append('<div class="order-section">')
        if body_opener_line:
            h.append(f'  <p class="para judgment-oral-header">{body_opener_line}</p>')

        open_block = False
        for item in merged_paras:
            if item[0] == "heading":
                if open_block:
                    h.append('  </div>')   # close previous para-block
                    open_block = False
                h.append(f'  <div class="order-heading">{item[2]}</div>')

            elif item[0] == "para":
                if open_block:
                    h.append('  </div>')   # close previous para-block
                num     = item[1]
                text    = item[2]
                para_id = re.sub(r'\D', '', num)
                h.append(f'  <div class="para-block" id="para-block-{para_id}">')
                h.append(
                    f'    <p class="para" id="para-{para_id}">'
                    f'<span class="para-num">{num}</span> {text}</p>'
                )
                open_block = True

            elif item[0] == "quote":
                if open_block:
                    # Quote belongs to current open para-block
                    h.append(f'    <blockquote>{item[2]}</blockquote>')
                else:
                    h.append(f'  <blockquote>{item[2]}</blockquote>')

            else:
                if open_block:
                    h.append(f'    <p class="para">{item[2]}</p>')
                else:
                    h.append(f'  <p class="para">{item[2]}</p>')

        if open_block:
            h.append('  </div>')   # close last para-block

        h.append('</div>')

    # Footer
    if footer_lines:
        h.append('<div class="judgment-footer">')
        for fl in footer_lines:
            h.append(f"  <div>{fl}</div>")
        h.append('</div>')

    h.append('</div>')
    h.append('</body>')
    h.append('</html>')

    return "\n".join(h)

# ─────────────────────────────────────────
# Process Single Record
# ─────────────────────────────────────────
def process_record(doc, s3_client, collection):
    sno        = doc.get("S.No.", "")
    file_id    = doc.get("FileID")
    s3_key_pdf = doc.get("s3_key_pdf")

    # Skip if HTML already uploaded
    if doc.get("s3_status") == "uploaded":
        log.warning(f"S.No. {sno}: HTML already uploaded — skipping")
        return "skipped"

    # Download PDF from S3
    try:
        pdf_obj   = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key_pdf)
        pdf_bytes = pdf_obj["Body"].read()
        log.info(f"S.No. {sno}: PDF downloaded from S3")
    except ClientError as e:
        log.error(f"S.No. {sno}: PDF download failed — {e}")
        return "failed"

    # Extract text
    try:
        lines, total_pages = extract_text_from_pdf(pdf_bytes)
    except Exception as e:
        log.error(f"S.No. {sno}: PDF parsing failed — {e}")
        collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"s3_status": "failed"}}
        )
        return "failed"

    if not lines:
        log.error(f"S.No. {sno}: No text extracted from PDF")
        return "failed"

    # Extract HC metadata
    pdf_meta = extract_pdf_meta(lines, total_pages, source_doc=doc)
    log.info(
        f"S.No. {sno}: Metadata extracted — "
        f"Citation: {pdf_meta['citation']}, "
        f"Date: {pdf_meta['judgment_date']}, "
        f"Bench: {pdf_meta['bench']}, "
        f"DB: {pdf_meta['is_division_bench']}, "
        f"Pages: {pdf_meta['total_pages']}, "
        f"Cases referred: {len(pdf_meta['referred_cases'])}, "
        f"Acts referred: {len(pdf_meta['acts_referred'])}, "
        f"Connected: {pdf_meta['is_connected_matter']}"
    )

    # Build HTML (PyMuPDF structured frontend-ready)
    html_content = build_html(file_id, pdf_bytes, pdf_meta=pdf_meta)

    # Upload HTML to S3
    html_key = f"{S3_HTML_FOLDER}/{file_id}.html"
    html_url = f"s3://{S3_BUCKET}/{html_key}"
    try:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=html_key,
            Body=html_content.encode("utf-8"),
            ContentType="text/html"
        )
        log.info(f"S.No. {sno}: HTML uploaded — {html_key}")
    except ClientError as e:
        log.error(f"S.No. {sno}: HTML upload failed — {e}")
        collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"s3_status": "failed"}}
        )
        return "failed"

    # Update MongoDB
    collection.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "FileID"             : file_id,
            "s3_status"          : "uploaded",
            "s3_url"             : html_url,
            "s3_key"             : html_key,
            "s3_html_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "pdf_meta"           : pdf_meta
        }}
    )
    log.info(f"S.No. {sno}: MongoDB updated — FileID: {file_id}")
    return "processed"

# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info(f"Stage 2 HTML Convert Started — Batch: {BATCH_MONTH_FOLDER} | Court: {COURT_NAME}")
    log.info("=" * 60)

    client     = MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION_NAME]
    s3_client  = get_s3_client()

    # Step 1 — Assign FileIDs
    max_fileid = get_max_fileid(client)
    log.info(f"Max existing FileID: {max_fileid}")

    query  = {"s3_status_pdf": "uploaded"}
    cursor = collection.find(query)
    if TEST_MODE_LIMIT > 0:
        cursor = cursor.limit(TEST_MODE_LIMIT)
    docs  = list(cursor)
    total = len(docs)
    log.info(f"Total records to process: {total}")

    # Assign FileIDs via bulk_write
    bulk_ops = []
    for i, doc in enumerate(docs):
        if not doc.get("FileID"):
            assigned_id = max_fileid + i + 1
            bulk_ops.append(
                UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {"FileID": assigned_id}}
                )
            )
            doc["FileID"] = assigned_id

    if bulk_ops:
        BATCH_SIZE = 1000
        total_ops  = len(bulk_ops)
        for start in range(0, total_ops, BATCH_SIZE):
            batch = bulk_ops[start: start + BATCH_SIZE]
            collection.bulk_write(batch, ordered=False)
            log.info(f"FileIDs bulk assigned: {start + len(batch):,} / {total_ops:,}")
        log.info(f"FileIDs assigned: {max_fileid + 1} → {max_fileid + total}")

    # Step 2 — Convert PDFs to HTML in parallel
    processed = 0
    skipped   = 0
    failed    = 0

    with ThreadPoolExecutor(max_workers=STAGE2_WORKERS) as executor:
        futures = {
            executor.submit(process_record, doc, s3_client, collection): doc
            for doc in docs
        }
        for future in as_completed(futures):
            result = future.result()
            if result == "processed":
                processed += 1
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1

    # Summary
    log.info("=" * 60)
    log.info(f"Stage 2 Complete — Batch: {BATCH_MONTH_FOLDER} | Court: {COURT_NAME}")
    log.info(f"Total    : {total}")
    log.info(f"Processed: {processed}")
    log.info(f"Skipped  : {skipped}")
    log.info(f"Failed   : {failed}")
    log.info("=" * 60)

if __name__ == "__main__":
    run()
