"""Extract assay/tissue mentions from downloaded publication PDFs.

NAMHub-specific connector adapted from the MC2 Center's ai-curation-pipeline
parse_publication_pdfs.py (https://github.com/mc2-center/ai-curation-pipeline).
Reuses that script's generic PDF-parsing and alias-matching logic, but swaps
out its MC2-specific vocabulary sources:

  - assay:  NAMHub's own Assay enum, read from Publications.json (must match
            exactly, since assay is a real constrained enum on the live
            table -- unlike MC2's, this has no Nonpreferred Terms/alias list,
            so matching is against the bare canonical term names only).
  - tissue: NAMHub's `tissue` field is free text with no fixed vocabulary of
            its own, so this reuses MC2's already-curated tissue.csv
            (modules/shared/tissue.csv in mc2-center/data-models) as the set
            of alias-matching candidates -- a reasonable, ready-made
            controlled vocabulary rather than inventing one from scratch.
            Tumor type (MC2's third field) is dropped entirely: NAMHub isn't
            a cancer-specific portal.

No ANTHROPIC_API_KEY / Claude step here -- alias-matching only. This is
less complete than MC2's Claude-based extraction (it only catches
near-exact vocabulary mentions in text, not novel phrasing), but requires
no API key.

Usage:
    python extract_assay_tissue.py pdfs/
    python extract_assay_tissue.py pdfs/ --output results.csv
"""

import argparse
import csv
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

from pubmed_crawler import PUBLICATIONS_SCHEMA_URL

MC2_TISSUE_VOCAB_URL = (
    "https://raw.githubusercontent.com/mc2-center/data-models/main/"
    "modules/shared/tissue.csv"
)

# Canonical Assay enum values that are also common English words used
# constantly outside their technical meaning ("a wide array of...",
# "large-scale study", "patient survival", body "weight" mentioned in
# passing). Confirmed empirically: these fired on 4-5 of 8 test papers
# regardless of actual content. "Unknown" is the enum's own placeholder
# value and should never itself be treated as a positive match.
EXCLUDED_ASSAY_TERMS = {"Array", "Scale", "Weight", "Survival", "Unknown"}

# tissue.csv "Nonpreferred Terms" aliases that are generic words, not
# tissue-specific: "Female"/"Male" (fires on any paper's sex/demographics
# mentions -- confirmed "Female" -> Cervix Uteri/Ovary matched 6 of 8 test
# papers) and "Not Applicable"/"N/A"/"NA"/"Unspecified" (placeholder rows in
# the source vocabulary, not real tissue types).
EXCLUDED_TISSUE_ALIASES = {"female", "male", "not applicable", "n/a", "na", "unspecified"}
EXCLUDED_TISSUE_TERMS = {"Not Applicable", "Unspecified"}


# ── vocabulary loading ──────────────────────────────────────────────────────

def load_assay_vocab() -> List[str]:
    """Return NAMHub's own Assay enum values from Publications.json, minus
    the generic/ambiguous ones excluded from alias matching (see
    EXCLUDED_ASSAY_TERMS)."""
    schema = json.loads(urllib.request.urlopen(PUBLICATIONS_SCHEMA_URL).read())
    terms = schema["properties"]["Assay"]["items"]["enum"]
    return [t for t in terms if t not in EXCLUDED_ASSAY_TERMS]


def load_tissue_vocab_and_aliases(source: Optional[str] = None) -> "tuple[List[str], Dict[str, str]]":
    """Return (canonical tissue terms, {alias -> canonical}) from MC2's tissue.csv,
    minus placeholder rows and generic aliases (see EXCLUDED_TISSUE_TERMS/
    EXCLUDED_TISSUE_ALIASES)."""
    if source is None:
        text = urllib.request.urlopen(MC2_TISSUE_VOCAB_URL).read().decode("utf-8-sig")
        rows = csv.DictReader(text.splitlines())
    else:
        with open(source, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

    terms: List[str] = []
    alias_map: Dict[str, str] = {}
    for row in rows:
        attr = row.get("Attribute", "").strip()
        if not attr or attr in EXCLUDED_TISSUE_TERMS:
            continue
        terms.append(attr)
        raw_terms = [attr] + [
            t.strip() for t in re.split(r",\s*", row.get("Nonpreferred Terms", "")) if t.strip()
        ]
        for raw in raw_terms:
            norm = _normalize_term(raw)
            if norm and norm not in EXCLUDED_TISSUE_ALIASES and norm not in alias_map:
                alias_map[norm] = attr
    return terms, alias_map


def _normalize_term(text: str) -> str:
    """Lowercase and collapse hyphens/underscores/slashes to single spaces."""
    text = re.sub(r"[-_/]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def build_alias_map_from_list(terms: List[str]) -> Dict[str, str]:
    """Build {normalized term -> canonical term} from a bare list of canonical values."""
    alias_map: Dict[str, str] = {}
    for term in terms:
        norm = _normalize_term(term)
        if norm and norm not in alias_map:
            alias_map[norm] = term
    return alias_map


def match_by_alias(text: str, alias_map: Dict[str, str]) -> List[str]:
    """Return canonical vocabulary terms whose aliases appear in text.

    Word-boundary-checked so e.g. "liver" doesn't fire inside "delivered".
    Very short aliases (< 3 chars) are skipped to avoid spurious matches.
    """
    norm_text = _normalize_term(text)
    matched: Set[str] = set()
    for alias in sorted(alias_map, key=len, reverse=True):
        if len(alias) < 3:
            continue
        idx = norm_text.find(alias)
        while idx != -1:
            before_ok = idx == 0 or not norm_text[idx - 1].isalnum()
            after_ok = (
                idx + len(alias) >= len(norm_text)
                or not norm_text[idx + len(alias)].isalnum()
            )
            if before_ok and after_ok:
                matched.add(alias_map[alias])
                break
            idx = norm_text.find(alias, idx + 1)
    return sorted(matched)


# ── PDF text extraction ─────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: Path) -> str:
    """Return the full plain text of a PDF."""
    if not HAS_FITZ:
        raise RuntimeError("PyMuPDF is required. Install with: pip install pymupdf")
    doc = fitz.open(str(pdf_path))
    pages = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(pages)


_METHODS_RE = re.compile(
    r"(?m)^\s*(?:\d+\.?\s+)?(?:Materials?\s+(?:and|&)\s+)?(?:Experimental\s+)?Methods?\s*$",
    re.I,
)
_END_SECTION_RE = re.compile(
    r"(?m)^\s*(?:\d+\.?\s+)?(?:Results?|Discussion|Conclusion|References?|"
    r"Acknowledgements?|Author\s+Contributions?|Funding|Supplementary)\s*$",
    re.I,
)


def extract_methods_section(text: str) -> str:
    """Return the methods section text, or the full text if no header found.

    Uses the LAST match: a bare "Method"/"Methods" line earlier in the
    document (running header, citation) can false-positive before the real
    heading.
    """
    matches = list(_METHODS_RE.finditer(text))
    if not matches:
        return text
    start = matches[-1].start()
    end_match = _END_SECTION_RE.search(text, matches[-1].end())
    end = end_match.start() if end_match else len(text)
    return text[start:end]


# ── per-text / per-PDF processing ───────────────────────────────────────────

def process_text(
    full_text: str,
    assay_alias: Dict[str, str],
    tissue_alias: Dict[str, str],
    methods_text: Optional[str] = None,
) -> Dict[str, str]:
    """Return {assay, tissue} extracted from already-retrieved plain text.

    assay is matched against `methods_text` if given (i.e. when full paper
    text is available and a methods section could be isolated from it),
    otherwise against `full_text` itself -- e.g. an abstract, which has no
    separate methods section to extract but is still worth alias-matching
    against as a fallback when a PDF isn't available at all.
    """
    return {
        "assay": ", ".join(match_by_alias(methods_text or full_text, assay_alias)),
        "tissue": ", ".join(match_by_alias(full_text, tissue_alias)),
    }


def process_pdf(pdf_path: Path, assay_alias: Dict[str, str], tissue_alias: Dict[str, str]) -> Dict[str, str]:
    """Return {filename, assay, tissue, error} for one PDF."""
    result = {"filename": pdf_path.name, "assay": "", "tissue": "", "error": ""}
    try:
        full_text = extract_text_from_pdf(pdf_path)
        methods_text = extract_methods_section(full_text)
        result.update(process_text(full_text, assay_alias, tissue_alias, methods_text))
    except Exception as exc:
        result["error"] = str(exc)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_dir", help="Directory containing downloaded PDF files.")
    parser.add_argument(
        "--output", "-o", default="assay_tissue_metadata.csv",
        help="Path for the output CSV (default: assay_tissue_metadata.csv).",
    )
    parser.add_argument(
        "--tissue-vocab", default=None,
        help="Local path to a tissue vocabulary CSV, overriding the mc2-center default.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.is_dir():
        print(f"ERROR: '{pdf_dir}' is not a directory.", file=sys.stderr)
        return 2
    if not HAS_FITZ:
        print("ERROR: PyMuPDF is required. pip install pymupdf", file=sys.stderr)
        return 2

    print("Loading assay vocabulary (NAMHub Publications.json)...")
    assay_alias = build_alias_map_from_list(load_assay_vocab())
    print("Loading tissue vocabulary (mc2-center/data-models tissue.csv)...")
    _, tissue_alias = load_tissue_vocab_and_aliases(args.tissue_vocab)

    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in '{pdf_dir}'.", file=sys.stderr)
        return 2

    print(f"Found {len(pdf_files)} PDF(s). Extraction mode: alias matching")
    results = []
    for i, pdf_path in enumerate(pdf_files, start=1):
        print(f"  [{i}/{len(pdf_files)}] {pdf_path.name}", end=" ... ", flush=True)
        result = process_pdf(pdf_path, assay_alias, tissue_alias)
        print(f"ERROR: {result['error']}" if result["error"] else "OK")
        results.append(result)

    output_path = Path(args.output)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "assay", "tissue", "error"])
        writer.writeheader()
        writer.writerows(results)

    errors = sum(1 for r in results if r["error"])
    print(f"\nDone. Output: {output_path}")
    print(f"  Processed: {len(results)}  |  Errors: {errors}")
    return 0 if errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
