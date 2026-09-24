#!/usr/bin/env python3
"""Download publication PDFs from a CSV manifest.

Ported unmodified from the MC2 Center's ai-curation-pipeline
(https://github.com/mc2-center/ai-curation-pipeline) -- domain-agnostic, so
it works as-is against namhub-pubmed-crawler's manifest CSVs (column
detection is fuzzy: e.g. "doi", "publicationTitle", and "pubMedId" are all
recognized).

This script reads a CSV file with publication metadata (such as DOI, PubMed ID,
and title columns), attempts to locate an open-access PDF for each publication,
and saves the PDFs to an output directory.

Search order per row:
1. Direct PDF URL if present in the row
2. PMC Open Access Service API via PubMed ID (official bulk-access API)
3. PubMed Central article-page scrape via PubMed ID (fallback; PMC's PDF
   endpoint now gates behind a client-side proof-of-work challenge, so this
   rarely succeeds any more)
4. Unpaywall via DOI (requires --email)
5. OpenAlex via DOI
6. Europe PMC via DOI or PubMed ID
7. DOI landing page -> look for common PDF links / PDF meta tags

It writes a download report CSV containing status and resolved URLs.

Examples:
    python download_publication_pdfs.py manifest.csv
    python download_publication_pdfs.py manifest.csv --output-dir pdfs
    python download_publication_pdfs.py manifest.csv --limit 25 --delay 1.0
"""

from __future__ import annotations

import argparse
import csv
import ftplib
import os
import re
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from xml.etree import ElementTree as ET

USER_AGENT = "publication-pdf-downloader/1.0 (research automation; contact: user-provided email if needed)"
TIMEOUT = 30

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class Result:
    index: int
    title: str
    doi: str
    pmid: str
    status: str
    pdf_path: str = ""
    source_url: str = ""
    message: str = ""


class PdfDownloadError(Exception):
    pass


# ---------- networking ----------
def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", TIMEOUT)
    resp = session.get(url, **kwargs)
    return resp


def head(session: requests.Session, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", TIMEOUT)
    resp = session.head(url, allow_redirects=True, **kwargs)
    return resp


# ---------- row parsing ----------
def normalize_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return WHITESPACE_RE.sub(" ", str(value)).strip()


def extract_doi(value: str) -> str:
    value = normalize_text(value)
    if not value:
        return ""
    match = DOI_RE.search(value)
    if not match:
        return ""
    doi = match.group(0).rstrip(" .;,)")
    return doi


def normalize_doi(value: str) -> str:
    doi = extract_doi(value)
    return doi.lower()


def normalize_pmid(value: str) -> str:
    value = normalize_text(value)
    if not value:
        return ""
    m = re.search(r"(\d{5,10})", value)
    return m.group(1) if m else ""


def slugify(text: str, max_length: int = 140) -> str:
    text = normalize_text(text)
    text = SAFE_FILENAME_RE.sub("", text)
    text = text.replace("/", "-")
    text = WHITESPACE_RE.sub("_", text).strip("._-")
    if not text:
        text = "untitled_publication"
    return text[:max_length].rstrip("._-")


def detect_columns(fieldnames: Iterable[str]) -> Dict[str, Optional[str]]:
    names = list(fieldnames)
    lowered = {name.lower(): name for name in names}
    # Leading-underscore columns are this pipeline's own internal bookkeeping
    # (_pdf_path, _download_status, ...) — never a real source column. Without
    # this exclusion, a fuzzy substring match on "pdf" would misidentify
    # _pdf_path (which stores a bare filename, not a URL) as the direct-PDF-URL
    # column when a prior run's output CSV is reused as input, silently
    # skipping all real PDF discovery for every row that already had one.
    fuzzy_names = [n for n in names if not n.startswith("_")]

    def choose(*candidates: str) -> Optional[str]:
        for candidate in candidates:
            if candidate.lower() in lowered:
                return lowered[candidate.lower()]
        for name in fuzzy_names:
            lname = name.lower()
            if any(candidate.lower() in lname for candidate in candidates):
                return name
        return None

    return {
        "title": choose("Publication Title", "title", "article title"),
        "doi": choose("Publication Doi", "doi", "DOI"),
        "pmid": choose("Pubmed Id", "PMID", "pubmed"),
        "pubmed_url": choose("Pubmed Url", "pubmed url"),
        "journal": choose("Publication Journal", "journal"),
        "year": choose("Publication Year", "year"),
        "pdf_url": choose("pdf", "pdf url", "full text pdf", "publication pdf"),
    }


# ---------- discovery helpers ----------
def is_pdf_response(resp: requests.Response) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type in PDF_MIME_TYPES:
        return True
    # Reject if server explicitly says it's HTML (e.g. NCBI gating pages)
    if "html" in content_type or "text" in content_type:
        return False
    if resp.url.lower().endswith(".pdf"):
        return True
    return False


def find_direct_pdf_in_html(html: str, base_url: str) -> Optional[str]:
    patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r'href=["\']([^"\']*/pdf(?:/|\?)[^"\']*)["\']',
        r'href=["\']([^"\']*download[^"\']*pdf[^"\']*)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I)
        if match:
            return urljoin(base_url, match.group(1))
    return None


def discover_pdf_from_doi(session: requests.Session, doi: str) -> Optional[str]:
    doi_url = f"https://doi.org/{quote(doi, safe='/')}"
    resp = get(session, doi_url, allow_redirects=True)
    if resp.status_code >= 400:
        return None
    if is_pdf_response(resp):
        return resp.url
    html = resp.text[:2_000_000]
    candidate = find_direct_pdf_in_html(html, resp.url)
    if candidate:
        return candidate
    return None


def discover_pdf_via_europe_pmc(session: requests.Session, query: str, query_type: str) -> Optional[str]:
    search_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    if query_type == "doi":
        q = f'DOI:"{query}"'
    else:
        q = f'EXT_ID:{query} AND SRC:MED'
    resp = get(session, search_url, params={"query": q, "format": "json", "pageSize": 1})
    if resp.status_code >= 400:
        return None
    data = resp.json()
    results = data.get("resultList", {}).get("result", [])
    if not results:
        return None
    result = results[0]
    for field in ("hasPDF", "isOpenAccess"):
        # keep as hints; not required
        _ = result.get(field)
    full_text_url = normalize_text(result.get("fullTextUrl"))
    if full_text_url and full_text_url.lower().endswith(".pdf"):
        return full_text_url
    return None


def pubmed_to_pmcid(session: requests.Session, pmid: str) -> Optional[str]:
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
    resp = get(
        session,
        url,
        params={
            "dbfrom": "pubmed",
            "db": "pmc",
            "id": pmid,
            "retmode": "xml",
        },
    )
    if resp.status_code >= 400:
        return None
    root = ET.fromstring(resp.text)
    id_nodes = root.findall(".//LinkSetDb/Link/Id")
    if not id_nodes:
        return None
    pmc_numeric = normalize_text(id_nodes[0].text)
    if not pmc_numeric:
        return None
    return f"PMC{pmc_numeric}"


def discover_pdf_via_pmc(session: requests.Session, pmcid: str) -> Optional[str]:
    """Scrape the PMC article page for a direct PDF file link.

    NCBI now gates PMC's PDF-serving endpoint behind a client-side
    proof-of-work challenge for most requests, so this rarely succeeds any
    more — kept as a fallback behind discover_pdf_via_pmc_oa_service.
    """
    article_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    resp = get(session, article_url)
    if resp.status_code >= 400:
        return None
    pdf_link = find_direct_pdf_in_html(resp.text, article_url)
    if not pdf_link:
        return None
    # Validate: confirm the URL actually serves a PDF (not a JS gating page)
    check = head(session, pdf_link)
    return pdf_link if is_pdf_response(check) else None


def discover_pdf_via_pmc_oa_service(session: requests.Session, pmcid: str) -> Optional[str]:
    """Return a PDF/tarball URL from NCBI's PMC Open Access Service API.

    This is NCBI's official, sanctioned API for automated bulk access to the
    PMC Open Access Subset — unlike scraping the article page, it isn't
    subject to PMC's proof-of-work bot challenge. Only covers OA-licensed
    articles: returns None (not an error) for anything outside that subset,
    e.g. the idIsNotOpenAccess response.

    The returned URL may be an ftp:// link to a .pdf file or a .tar.gz
    package containing one — stream_download_pdf resolves either shape.
    """
    resp = get(
        session,
        "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
        params={"id": pmcid},
    )
    if resp.status_code >= 400:
        return None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None
    if root.find(".//error") is not None:
        return None
    links = root.findall(".//record/link")
    if not links:
        return None
    pdf_link = next((l.get("href") for l in links if l.get("format") == "pdf"), None)
    if pdf_link:
        return pdf_link
    tgz_link = next((l.get("href") for l in links if l.get("format") == "tgz"), None)
    return tgz_link or None


def discover_pdf_via_unpaywall(session: requests.Session, doi: str, email: str) -> Optional[str]:
    """Return a direct PDF URL from Unpaywall for the given DOI."""
    resp = get(
        session,
        f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
        params={"email": email},
    )
    if resp.status_code >= 400:
        return None
    data = resp.json()
    best = data.get("best_oa_location") or {}
    return best.get("url_for_pdf") or None


def discover_pdf_via_openalex(session: requests.Session, doi: str) -> Optional[str]:
    """Return a direct PDF URL from OpenAlex for the given DOI.

    open_access.oa_url is not guaranteed to be a direct PDF — it can be a
    landing page (e.g. the DOI resolver link itself) — so it's validated
    with a HEAD-check before being trusted as a candidate, same as
    discover_pdf_via_pmc does for its scraped link.
    """
    resp = get(
        session,
        f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}",
        params={"select": "best_oa_location,open_access"},
        headers={"User-Agent": USER_AGENT},
    )
    if resp.status_code >= 400:
        return None
    data = resp.json()
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("pdf_url")
    if not pdf_url:
        oa = data.get("open_access") or {}
        pdf_url = oa.get("oa_url")
    if not pdf_url:
        return None
    check = head(session, pdf_url)
    return pdf_url if is_pdf_response(check) else None


# ---------- download ----------
def fetch_article_text(session: requests.Session, doi: str) -> str:
    """Fetch the DOI landing page and return stripped plain text.

    Strips HTML tags and collapses whitespace.  Returns an empty string if the
    page cannot be fetched or contains no useful body text.
    """
    if not doi:
        return ""
    doi_url = f"https://doi.org/{quote(doi, safe='/')}"
    try:
        resp = get(session, doi_url, allow_redirects=True)
    except Exception:
        return ""
    if resp.status_code >= 400:
        return ""
    html = resp.text
    # Remove script / style blocks first
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    text = (
        text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
    )
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _candidate_urls(
    session: requests.Session,
    row: Dict[str, str],
    columns: Dict[str, Optional[str]],
    email: str = "",
) -> List[Tuple[str, str]]:
    """Return an ordered list of (url, source_label) candidates to try."""
    doi = normalize_doi(row.get(columns["doi"], "") if columns.get("doi") else "")
    pmid = normalize_pmid(row.get(columns["pmid"], "") if columns.get("pmid") else "")
    candidates: List[Tuple[str, str]] = []

    direct_pdf = normalize_text(row.get(columns["pdf_url"], "") if columns.get("pdf_url") else "")
    if direct_pdf:
        candidates.append((direct_pdf, "row_pdf_url"))
        return candidates  # authoritative; no need to search further

    if pmid:
        pmcid = pubmed_to_pmcid(session, pmid)
        if pmcid:
            url = discover_pdf_via_pmc_oa_service(session, pmcid)
            if url:
                candidates.append((url, "pmc_oa_service"))
            url = discover_pdf_via_pmc(session, pmcid)
            if url:
                candidates.append((url, "pmc"))

    if doi and email:
        url = discover_pdf_via_unpaywall(session, doi, email)
        if url:
            candidates.append((url, "unpaywall"))

    if doi:
        url = discover_pdf_via_openalex(session, doi)
        if url:
            candidates.append((url, "openalex"))

    if doi:
        url = discover_pdf_via_europe_pmc(session, doi, "doi")
        if url:
            candidates.append((url, "europe_pmc"))

    if pmid:
        url = discover_pdf_via_europe_pmc(session, pmid, "pmid")
        if url:
            candidates.append((url, "europe_pmc"))

    if doi:
        url = discover_pdf_from_doi(session, doi)
        if url:
            candidates.append((url, "doi_landing_page"))

    return candidates


def _fetch_geo_soft(session: requests.Session, gse: str) -> str:
    """Fetch the brief soft text for a GSE accession. Returns empty string on failure."""
    resp = get(session, "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi",
               params={"acc": gse, "targ": "self", "form": "text", "view": "brief"})
    if resp.status_code >= 400:
        return ""
    return resp.text


def _doi_from_pmid(session: requests.Session, pmid: str) -> str:
    """Return the DOI for a PubMed ID, or empty string if not found."""
    resp = get(session, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
               params={"db": "pubmed", "id": pmid, "retmode": "json"})
    if resp.status_code >= 400:
        return ""
    doc = resp.json().get("result", {}).get(pmid, {})
    return next(
        (a["value"] for a in doc.get("articleids", []) if a["idtype"] == "doi"),
        "",
    )


def is_geo_superseries(session: requests.Session, gse: str) -> bool:
    """Return True if the GSE accession is a GEO SuperSeries."""
    return "SuperSeries of:" in _fetch_geo_soft(session, gse)


def fetch_geo_metadata(session: requests.Session, gse: str, pub_doi: str = "") -> Dict[str, str]:
    """Return design, species, and doi for a GSE accession via GEO eutils.

    Dataset DOI is resolved from the GSE's linked PubMed IDs. Falls back to
    pub_doi if no dataset-specific DOI is found.
    """
    taxon_map = {
        "homo sapiens": "Human",
        "mus musculus": "Mouse",
        "rattus norvegicus": "Rat",
        "saccharomyces cerevisiae": "Yeast",
        "caenorhabditis elegans": "Worm",
        "drosophila melanogaster": "Fruit Fly",
        "danio rerio": "Zebrafish",
        "macaca mulatta": "Rhesus monkey",
    }
    r = get(session, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "gds", "term": f"{gse}[Accession]", "retmode": "json"})
    if r.status_code >= 400:
        return {}
    ids = [i for i in r.json().get("esearchresult", {}).get("idlist", []) if i.startswith("2")]
    if not ids:
        return {}
    r2 = get(session, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
             params={"db": "gds", "id": ids[0], "retmode": "json"})
    if r2.status_code >= 400:
        return {}
    doc = r2.json().get("result", {}).get(ids[0], {})

    # Species
    taxon = doc.get("taxon", "")
    parts = [t.strip().lower() for t in taxon.split(";") if t.strip()]
    mapped = {taxon_map.get(p, p.title()) for p in parts}
    species = "Multispecies" if len(mapped) > 1 else (mapped.pop() if mapped else "")

    # Dataset DOI: resolve from GEO-linked PMIDs via PubMed esummary
    soft_text = _fetch_geo_soft(session, gse)
    geo_pmids = re.findall(r"!Series_pubmed_id\s*=\s*(\d+)", soft_text)
    dataset_doi = ""
    for pmid in geo_pmids:
        doi = _doi_from_pmid(session, pmid)
        if doi:
            dataset_doi = f"https://doi.org/{doi}"
            break
    if not dataset_doi:
        dataset_doi = pub_doi

    return {"design": doc.get("summary", ""), "species": species, "doi": dataset_doi}


def fetch_mesh_terms(session: requests.Session, pmid: str) -> List[str]:
    """Return a list of MeSH descriptor names for a PubMed ID."""
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    resp = get(session, url, params={"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"})
    if resp.status_code >= 400:
        return []
    return re.findall(r"<DescriptorName[^>]*>([^<]+)</DescriptorName>", resp.text)


def _fetch_ftp_file(url: str, timeout: float = 30) -> Path:
    """Download a file over FTP (e.g. an NCBI PMC OA package) to a temp path."""
    parsed = urlparse(url)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=Path(parsed.path).suffix)
    os.close(tmp_fd)
    tmp_path = Path(tmp_path)
    try:
        ftp = ftplib.FTP(parsed.hostname, timeout=timeout)
        try:
            ftp.login()  # anonymous
            with open(tmp_path, "wb") as f:
                ftp.retrbinary(f"RETR {parsed.path}", f.write)
        finally:
            ftp.quit()
    except ftplib.all_errors as exc:
        tmp_path.unlink(missing_ok=True)
        raise PdfDownloadError(f"FTP fetch failed for {url}: {exc}") from exc
    return tmp_path


def _extract_pdf_from_tarball(tarball_path: Path, output_path: Path) -> None:
    """Extract the first .pdf member of a tarball to output_path."""
    with tarfile.open(tarball_path, mode="r:gz") as tar:
        member = next(
            (m for m in tar.getmembers() if m.name.lower().endswith(".pdf")), None
        )
        if member is None:
            raise PdfDownloadError(f"No PDF found inside tarball {tarball_path}")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise PdfDownloadError(f"Could not read PDF member from tarball {tarball_path}")
        with open(output_path, "wb") as f:
            f.write(extracted.read())


def stream_download_pdf(session: requests.Session, url: str, output_path: Path) -> None:
    if url.startswith("ftp://"):
        tmp_path = _fetch_ftp_file(url)
        try:
            if url.lower().endswith(".pdf"):
                tmp_path.replace(output_path)
            else:
                _extract_pdf_from_tarball(tmp_path, output_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return

    with get(session, url, stream=True, allow_redirects=True) as resp:
        if resp.status_code >= 400:
            raise PdfDownloadError(f"HTTP {resp.status_code} from {url}")
        if not is_pdf_response(resp):
            # sometimes PDF is served without a clean content-type until GET body begins;
            # allow URLs ending with pdf, otherwise reject.
            raise PdfDownloadError(
                f"URL did not resolve to a PDF (content-type={resp.headers.get('Content-Type', '')})"
            )
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)


# ---------- main workflow ----------
def make_output_filename(row: Dict[str, str], columns: Dict[str, Optional[str]], index: int) -> str:
    title = normalize_text(row.get(columns["title"], "") if columns.get("title") else "")
    year = normalize_text(row.get(columns["year"], "") if columns.get("year") else "")
    journal = normalize_text(row.get(columns["journal"], "") if columns.get("journal") else "")
    doi = normalize_doi(row.get(columns["doi"], "") if columns.get("doi") else "")

    base = slugify(title) if title else f"publication_{index:04d}"
    if year:
        base = f"{year}_{base}"
    if journal:
        base = f"{base}__{slugify(journal, 40)}"
    if doi:
        suffix = slugify(doi.replace("/", "_"), 50)
        base = f"{base}__{suffix}"
    return f"{base}.pdf"


def process_csv(
    csv_path: Path,
    output_dir: Path,
    report_path: Path,
    delay: float = 0.5,
    limit: Optional[int] = None,
    overwrite: bool = False,
    email: str = "",
) -> List[Result]:
    session = build_session()
    output_dir.mkdir(parents=True, exist_ok=True)
    results: List[Result] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row.")
        columns = detect_columns(reader.fieldnames)
        if not columns.get("doi") and not columns.get("pmid") and not columns.get("pdf_url"):
            raise ValueError(
                "Could not detect DOI, PubMed ID, or PDF URL columns. "
                f"Detected headers: {reader.fieldnames}"
            )

        for idx, row in enumerate(reader, start=1):
            if limit is not None and idx > limit:
                break

            title = normalize_text(row.get(columns["title"], "") if columns.get("title") else "")
            doi = normalize_doi(row.get(columns["doi"], "") if columns.get("doi") else "")
            pmid = normalize_pmid(row.get(columns["pmid"], "") if columns.get("pmid") else "")
            output_name = make_output_filename(row, columns, idx)
            output_path = output_dir / output_name

            if output_path.exists() and not overwrite:
                results.append(
                    Result(
                        index=idx,
                        title=title,
                        doi=doi,
                        pmid=pmid,
                        status="skipped_exists",
                        pdf_path=str(output_path),
                        message="File already exists",
                    )
                )
                continue

            try:
                candidates = _candidate_urls(session, row, columns, email=email)
                if not candidates:
                    raise PdfDownloadError("No downloadable PDF URL found")
                last_exc: Exception = PdfDownloadError("No downloadable PDF URL found")
                downloaded = False
                for pdf_url, source in candidates:
                    try:
                        stream_download_pdf(session, pdf_url, output_path)
                        downloaded = True
                        break
                    except Exception as exc:
                        last_exc = exc
                        if output_path.exists():
                            try:
                                output_path.unlink()
                            except OSError:
                                pass
                if downloaded:
                    results.append(
                        Result(
                            index=idx,
                            title=title,
                            doi=doi,
                            pmid=pmid,
                            status="downloaded",
                            pdf_path=str(output_path),
                            source_url=pdf_url,
                            message=source,
                        )
                    )
                else:
                    raise last_exc
            except Exception as exc:
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        pass
                results.append(
                    Result(
                        index=idx,
                        title=title,
                        doi=doi,
                        pmid=pmid,
                        status="failed",
                        source_url="",
                        message=str(exc),
                    )
                )
            time.sleep(delay)

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "title", "doi", "pmid", "status", "pdf_path", "source_url", "message"],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(result.__dict__)

    return results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download PDFs for publications listed in a CSV manifest.")
    parser.add_argument("csv_path", help="Path to the publication manifest CSV file")
    parser.add_argument("--output-dir", default="downloaded_pdfs", help="Directory where PDFs will be saved")
    parser.add_argument(
        "--report-path",
        default="pdf_download_report.csv",
        help="CSV report describing success/failure for each publication",
    )
    parser.add_argument("--delay", type=float, default=0.5, help="Delay in seconds between publications")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N publications")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing PDF files")
    parser.add_argument(
        "--email",
        default="",
        help="Email address for the Unpaywall API (enables broader OA PDF discovery).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    report_path = Path(args.report_path)

    try:
        results = process_csv(
            csv_path=csv_path,
            output_dir=output_dir,
            report_path=report_path,
            delay=args.delay,
            limit=args.limit,
            overwrite=args.overwrite,
            email=args.email,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    total = len(results)
    downloaded = sum(r.status == "downloaded" for r in results)
    skipped = sum(r.status == "skipped_exists" for r in results)
    failed = sum(r.status == "failed" for r in results)

    print(f"Processed:   {total}")
    print(f"Downloaded:  {downloaded}")
    print(f"Skipped:     {skipped}")
    print(f"Failed:      {failed}")
    print(f"Report CSV:  {report_path}")
    print(f"Output dir:  {output_dir}")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
