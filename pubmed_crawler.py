"""PubMed 'Crawler' of NAMHub Publications.

Discovers publications via PubMed using NAMHub grant numbers, then fetches
metadata in bulk from Europe PMC (faster than using NCBI E-utils). Open-access
status is determined via the Unpaywall API.

Modeled after the MC2 Center's pubmed-crawler
(https://github.com/mc2-center/pubmed-crawler), adapted for the NAMHub
Grants (syn75404715) and Publications (syn75404744) Synapse tables.
"""

import argparse
import csv
import getpass
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import requests
import synapseclient
from Bio import Entrez
from bs4 import BeautifulSoup
from http.client import HTTPException
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils.dataframe import dataframe_to_rows
from synapseclient.models import Table, query
from urllib.error import HTTPError

PUBLICATIONS_SCHEMA_URL = (
    "https://raw.githubusercontent.com/sagebio-ada/nam-hub-models/main/"
    "json_schemas/Publications.json"
)
PENDING_ANNOTATION = "Pending Annotation"

# Matches the "base" part of an NIH grant number (activity code + IC code +
# serial number, e.g. "UM1TR006029"), ignoring the leading application-type
# digit and trailing "-<support year><suffix>" that appear in the full form
# (e.g. "1UM1TR006029-01"). PubMed's [Grant number] search field, and the
# grantId strings in its grantsList, use this base form.
NIH_GRANT_NUMBER_RE = re.compile(r"[A-Z][A-Z0-9]\d[A-Z]{2}\d{5,7}")


def base_grant_number(raw):
    """Extract the base NIH grant number from a full grant number string.

    Falls back to the punctuation-stripped, uppercased input if no NIH-style
    grant number is found (e.g. for non-NIH funders).
    """
    if not raw:
        return ""
    match = NIH_GRANT_NUMBER_RE.search(raw.upper())
    return match.group(0) if match else normalize_grant_number(raw)


def login():
    """Log into Synapse. If env variables not found, prompt user.

    Returns:
        syn: Synapse object
    """
    try:
        syn = synapseclient.login(
            authToken=os.getenv("SYNAPSE_AUTH_TOKEN"), silent=True
        )
    except synapseclient.core.exceptions.SynapseNoCredentialsError:
        print(
            "Credentials not found; please manually provide your",
            "Synapse username and password.",
        )
        username = input("Synapse username: ")
        password = getpass.getpass("Synapse password: ")
        syn = synapseclient.login(username, password, silent=True)
    return syn


def get_args():
    """Set up command-line interface and get arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Get PubMed information from a list of NAMHub grant numbers and "
            "put the results into an xlsx manifest. The Publications table "
            "ID is used to scrape for only new publications."
        )
    )
    parser.add_argument(
        "-g",
        "--grant_id",
        type=str,
        default="syn75404715",
        help=(
            "Synapse table/view ID containing grant numbers in the "
            "'grantNumber' column. (Default: syn75404715, the NAMHub "
            "Grants table)"
        ),
    )
    parser.add_argument(
        "-t",
        "--table_id",
        type=str,
        default="syn75404744",
        help=(
            "Synapse table holding already-curated PubMed info, used to "
            "filter out publications that have already been found. "
            "(Default: syn75404744, the NAMHub Publications table)"
        ),
    )
    parser.add_argument(
        "-u",
        "--study_id",
        type=str,
        default="syn75404711",
        help=(
            "Synapse table ID for the NAMHub Studies table, used to "
            "auto-fill 'studyId' from a matched 'grantId'. "
            "(Default: syn75404711)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output_name",
        type=str,
        default=datetime.today().strftime("%Y-%m-%d") + "_publications-manifest",
        help="Filename for output manifest. (Default: <current-date>_publications-manifest)",
    )
    parser.add_argument(
        "-s",
        "--supplemental_grants",
        type=str,
        default=None,
        help=(
            "Path to a CSV (with a 'grantNumber' column) of additional, "
            "non-primary grant numbers to also search PubMed with -- e.g. "
            "predecessor/related grants a NAMHub publication cited alongside "
            "one of the 6 core grants. Publications found only via these "
            "grants still get a manifest row, but with 'grantId' left blank "
            "(they're not NAMHub Grants table entries) and the matching "
            "supplemental grant number(s) recorded in 'secondaryGrantMatch' "
            "for curator review. Not set by default: the core grant set "
            "already covers new NAMHub publications going forward."
        ),
    )
    return parser.parse_args()


def get_studies(syn, study_id="syn75404711"):
    """Get the grantId -> studyId mapping from the Studies table.

    Only Studies rows with a non-empty grantId are included (a few, like
    NCATS-intramural TDC studies, have no NAMHub grant and can't be
    auto-derived this way).

    Returns:
        dict: grantId -> studyId
    """
    studies = query(f"SELECT studyId, grantId FROM {study_id}")
    studies = studies[studies["grantId"].notna() & (studies["grantId"] != "")]
    return dict(zip(studies["grantId"], studies["studyId"]))


def get_grants(syn, grant_id):
    """Get grant numbers and their Synapse grantId from the Grants table.

    Assumptions:
        Synapse table has `grantId` and `grantNumber` columns.

    Returns:
        df: grants with non-empty grant numbers
    """
    print("Querying for grant numbers... ")
    grants = query(f"SELECT grantId, grantNumber FROM {grant_id}")
    grants = grants[grants["grantNumber"].notna() & (grants["grantNumber"] != "")]
    print(f"  Number of grants: {len(grants)}\n")
    return grants


def read_supplemental_grants(path):
    """Read a supplemental-grants CSV (a 'grantNumber' column; other columns ignored).

    Returns:
        set: base NIH grant numbers
    """
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        numbers = {base_grant_number(row["grantNumber"]) for row in reader}
    numbers.discard("")
    return numbers


def get_pmids(grants, supplemental_grant_numbers=None):
    """Get list of PubMed IDs using grant numbers as search param.

    Returns:
        set: PubMed IDs
    """
    print("Getting PMIDs from NCBI... ")
    grant_numbers = {base_grant_number(n) for n in grants["grantNumber"]}
    grant_numbers |= supplemental_grant_numbers or set()
    grant_numbers.discard("")
    search_term = "[Grant number] OR ".join(grant_numbers) + "[Grant number]"
    handle = Entrez.esearch(
        db="pubmed", term=search_term, retmax=100_000, retmode="xml", sort="relevance"
    )
    pmids = set(Entrez.read(handle).get("IdList"))
    handle.close()

    # Entrez docs suggests to use HTTP POST when text query is >700
    # characters. If warning is received, replace above code with following:
    # base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    # results = json.loads(requests.post(
    #     f"{base_url}?db=pubmed&term={search_term}&retmax=100000&retmode=json"))
    # pmids = set(results.get('esearchresult').get('idlist'))

    print(f"  Total unique publications: {len(pmids)}\n")
    return pmids


def normalize_grant_number(raw):
    """Strip whitespace/punctuation and uppercase, for fuzzy grant matching."""
    return "".join(ch for ch in raw.upper() if ch.isalnum()) if raw else ""


def match_grant_ids(pubmed_grants, curr_grants):
    """Match a publication's raw PubMed grant strings to NAMHub grantIds.

    PubMed grant strings often include activity-code prefixes or suffixes
    (e.g. "5 UM1 TR006029 - 03") that don't exactly match the NAMHub
    `grantNumber` value, so numbers are compared with punctuation/whitespace
    stripped.

    Returns:
        set: matched NAMHub grantId values
    """
    known = {
        base_grant_number(number): grant_id
        for number, grant_id in zip(
            curr_grants["grantNumber"], curr_grants["grantId"]
        )
    }
    matched = set()
    for grant in pubmed_grants:
        raw = grant.get("grantId")
        if not raw:
            continue
        normalized = normalize_grant_number(raw)
        for grant_number, grant_id in known.items():
            if grant_number and grant_number in normalized:
                matched.add(grant_id)
    return matched


def match_secondary_grants(pubmed_grants, supplemental_grant_numbers):
    """Match a publication's raw PubMed grant strings to supplemental grant numbers.

    Unlike match_grant_ids(), these don't resolve to a NAMHub grantId (the
    grants aren't in the Grants table) -- this just reports which
    supplemental grant number(s) the publication actually cites, for
    curator review.

    Returns:
        set: matched base NIH grant numbers
    """
    matched = set()
    for grant in pubmed_grants:
        raw = grant.get("grantId")
        if not raw:
            continue
        normalized = normalize_grant_number(raw)
        for grant_number in supplemental_grant_numbers:
            if grant_number and grant_number in normalized:
                matched.add(grant_number)
    return matched


def _fetch_oa_status(raw_doi, email):
    """Fetch open-access status from Unpaywall for a single DOI.

    Returns:
        tuple: (raw_doi, accessibility). accessibility is None if it
        couldn't be determined -- publicationAccessibility's enum only
        allows "Open Access"/"Restricted Access", so an unknown status is
        left blank rather than given some third placeholder value.
    """
    if not raw_doi:
        return raw_doi, None
    try:
        response = requests.get(
            f"https://api.unpaywall.org/v2/{raw_doi}?email={email}", timeout=10
        )
        response.raise_for_status()
        if response.json().get("is_oa"):
            return raw_doi, "Open Access"
        return raw_doi, "Restricted Access"
    except (requests.exceptions.HTTPError, requests.exceptions.RequestException, json.JSONDecodeError):
        return raw_doi, None


def get_related_info(pmids, batch_size=200, max_retries=3):
    """Get related dataset information for a collection of PMIDs in batched elink calls.

    NCBI's pubmed->gds elink is based on the "PubMed ID" field GEO/SRA/dbGaP
    submitters attach to their own deposit -- i.e. it reflects datasets
    generated for a publication, not just cited/reused by it.

    Network issues may be encountered when making Entrez requests. Retry up
    to `max_retries` times before skipping.

    Returns:
        dict: mapping of pmid -> XML for GEO, SRA, and dbGaP
    """
    result_map = {}
    pmid_list = list(pmids)
    for start in range(0, len(pmid_list), batch_size):
        chunk = pmid_list[start : start + batch_size]
        linksets = []
        for attempt in range(max_retries):
            try:
                handle = Entrez.elink(
                    dbfrom="pubmed",
                    db="gds,sra,bioproject",
                    # Passing a list (not a comma-joined string) makes NCBI
                    # return one LinkSet per input PMID instead of a single
                    # LinkSet aggregating results across the whole batch
                    # (which would then get misattributed entirely to
                    # whichever PMID happens to be first in that LinkSet's
                    # IdList).
                    id=chunk,
                    retmode="xml",
                )
                linksets = Entrez.read(handle)
                handle.close()
                break
            except (RuntimeError, HTTPException, HTTPError):
                if attempt < max_retries - 1:
                    print(
                        f"  Network issue getting related info for PMID {chunk[0]}..{chunk[-1]}, trying again..."
                    )
                    time.sleep(1)
                else:
                    print(f"  ⚠️ Failed to get related info for PMID {chunk[0]}..{chunk[-1]}. Skipping...")
        for linkset in linksets:
            pmid = str(linkset.get("IdList", [None])[0])
            if pmid is None:
                continue
            related_info = {}
            for link_db in linkset.get("LinkSetDb", []):
                db = re.search(r"pubmed_(.*)", link_db.get("LinkName")).group(1)
                ids = [link.get("Id") for link in link_db.get("Link")]
                handle = Entrez.esummary(db=db, id=",".join(ids))
                soup = BeautifulSoup(handle, features="xml")
                handle.close()
                related_info[db] = soup
            result_map[pmid] = related_info
    return result_map


def parse_geo(info):
    """Parse and return GSE IDs."""
    gse_ids = []
    if info:
        tags = info.find_all("Item", attrs={"Name": "GSE"})
        gse_ids = ["GSE" + tag.text for tag in tags]
    return gse_ids


def parse_sra(info):
    """Parse and return SRX/SRP IDs."""
    srx_ids = srp_ids = []
    if info:
        tags = info.find_all("Item", attrs={"Name": "ExpXml"})
        srx_ids = [
            re.search(r'Experiment acc="(.*?)"', tag.text).group(1)
            for tag in tags
            if re.search(r'Experiment acc="(.*?)"', tag.text)
        ]
        srp_ids = {
            re.search(r'Study acc="(.*?)"', tag.text).group(1)
            for tag in tags
            if re.search(r'Study acc="(.*?)"', tag.text)
        }
    return srx_ids, srp_ids


def parse_dbgap(info):
    """Parse and return study IDs."""
    gap_ids = []
    if info:
        tags = info.find_all("Item", attrs={"Name": "d_study_id"})
        gap_ids = [tag.text for tag in tags]
    return gap_ids


def get_keywords(record):
    """Get a publication's keywords, falling back to MeSH terms.

    The Publications schema's `keywords` field is documented as "Publication
    keywords or MeSH terms" -- author-supplied keywords aren't always
    present (e.g. many journals only get NLM-assigned MeSH headings), so
    fall back to those when there's no keywordList.

    Returns:
        list: keyword or MeSH descriptor strings
    """
    keywords = record.get("keywordList", {}).get("keyword")
    if keywords:
        return keywords
    mesh_headings = record.get("meshHeadingList", {}).get("meshHeading", [])
    return [m["descriptorName"] for m in mesh_headings if m.get("descriptorName")]


def get_europepmc_records(pmids, max_retries=3):
    """Bulk-fetch Europe PMC "core" records for a set of PMIDs.

    Faster than Entrez efetch, and includes keywords, grantsList, etc.
    Retries on a transient Europe PMC search-service failure.

    Assumptions:
        Number of PMIDs per call is <1,000, as the Europe PMC API has a
        limit of 1,000 results per request.

    Returns:
        list: matching records (excludes errata), filtered to the given PMIDs
    """
    pmc_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/searchPOST"
    data = {
        "query": " OR ".join(pmids),
        "resultType": "core",
        "format": "json",
        "pageSize": 1_000,
    }
    results = None
    for attempt in range(max_retries):
        response = json.loads(requests.post(url=pmc_url, data=data).content)
        result_list = response.get("resultList")
        if result_list is not None:
            results = result_list.get("result")
            break
        if attempt < max_retries - 1:
            print(f"  Europe PMC search unavailable ({response.get('errMsg', response)}), retrying...")
            time.sleep(5)
    if results is None:
        raise RuntimeError(f"Europe PMC search failed after {max_retries} attempts: {response}")

    return [
        r for r in results
        if r.get("pmid") in pmids
        and "Published Erratum" not in r.get("pubTypeList", {}).get("pubType", [])
    ]


def pull_info(pmids, curr_grants, email, supplemental_grant_numbers=None, studies_by_grant=None):
    """Create dataframe of publications and their pulled data.

    Publication data is pulled in bulk using the Europe PMC API, since it's
    faster than Entrez. Open-access status is pulled from the Unpaywall API.

    Returns:
        df: publications data, columns matching the NAMHub Publications schema
    """
    filtered_results = get_europepmc_records(pmids)

    # Fetch OA statuses for all qualifying publications.
    unique_dois = {r.get("doi") for r in filtered_results}
    oa_map = {}
    with ThreadPoolExecutor() as executor:
        for raw_doi, accessibility in executor.map(
            lambda doi: _fetch_oa_status(doi, email), unique_dois
        ):
            oa_map[raw_doi] = accessibility

    # Fetch GEO/SRA/dbGaP accessions for datasets generated for each publication.
    related_info_map = get_related_info({r.get("pmid") for r in filtered_results})

    table = []
    for result in filtered_results:
        pmid = result.get("pmid")

        raw_doi = result.get("doi")
        doi = "https://doi.org/" + raw_doi if raw_doi else None
        journal_info = result.get("journalInfo").get("journal")
        journal = journal_info.get(
            "isoabbreviation", journal_info.get("medlineAbbreviation")
        )
        year = result.get("pubYear")
        title = result.get("title").rstrip(".")
        try:
            authors = [
                f"{author.get('firstName')} {author.get('lastName')}"
                for author in result.get("authorList").get("author")
            ]
        except AttributeError:
            authors = []  # There is not an author list with this publication.
        keywords = get_keywords(result)
        raw_abstract = result.get("abstractText")
        abstract = raw_abstract.replace("<h4>", " ").replace("</h4>", ": ").strip() if raw_abstract else None

        accessibility = oa_map.get(raw_doi)

        grants = result.get("grantsList", {}).get("grant", [])
        grant_ids = match_grant_ids(grants, curr_grants)
        secondary_matches = match_secondary_grants(grants, supplemental_grant_numbers or set())
        study_ids = {
            (studies_by_grant or {})[gid] for gid in grant_ids if gid in (studies_by_grant or {})
        }

        related_info = related_info_map.get(pmid, {})
        gse_ids = parse_geo(related_info.get("gds"))
        srx, srp = parse_sra(related_info.get("sra"))
        dbgaps = parse_dbgap(related_info.get("gap"))
        # SRX is experiment-level (many per submission); keep the coarser
        # study-level SRP/GSE/dbGaP accessions instead.
        dataset_ids = {*gse_ids, *srp, *dbgaps}

        publication_info = {
            "pubMedId": [pmid],
            "pubMedLink": [f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"],
            "publicationTitle": [title],
            "publicationYear": [int(year) if year else None],
            "authors": [", ".join(authors)],
            "journal": [journal],
            "abstract": [abstract],
            # grantId, studyId, namId, assay, and tissue are all
            # multi-value (STRING_LIST) columns on the live Publications
            # table, so multiple values are comma-separated here.
            "keywords": [", ".join(keywords)],
            "doi": [doi],
            "grantId": [", ".join(sorted(grant_ids))],
            "namId": [PENDING_ANNOTATION],
            "studyId": [", ".join(sorted(study_ids)) if study_ids else PENDING_ANNOTATION],
            "assay": [PENDING_ANNOTATION],
            "tissue": [PENDING_ANNOTATION],
            "datasetAlias": [", ".join(sorted(dataset_ids))],
            "publicationAccessibility": [accessibility],
            "secondaryGrantMatch": [", ".join(sorted(secondary_matches))],
        }
        row = pd.DataFrame(publication_info)
        table.append(row)
    return pd.concat(table)


def find_publications(syn, grant_id, table_id, email, supplemental_grant_numbers=None, study_id="syn75404711"):
    """Get list of publications based on NAMHub grants.

    Returns:
        df: publications data
    """
    grants = get_grants(syn, grant_id)
    studies_by_grant = get_studies(syn, study_id)
    pmids = get_pmids(grants, supplemental_grant_numbers)

    # If user provided a table ID, only scrape info from publications
    # not already listed in the provided table.
    if table_id:
        table_name = Table(id=table_id).get().name
        print(f"Comparing with table: {table_name}...")
        current_pmids = (
            query(f'SELECT "pubMedId" FROM {table_id}')["pubMedId"]
            .astype(str)
            .tolist()
        )
        pmids -= set(current_pmids)
        print(f"  New publications found: {len(pmids)}\n")

    if pmids:
        print("Pulling information from publications... ")
        table = pull_info(pmids, grants, email, supplemental_grant_numbers, studies_by_grant)
        print(f"  Publications pre-annotated: {len(table.index)}\n")
    else:
        table = pd.DataFrame()
        print()
    return table


def generate_manifest(table, output):
    """Generate manifest file (xlsx) with given publications data."""
    wb = Workbook()
    ws = wb.active
    ws.title = "manifest"
    for r in dataframe_to_rows(table, index=False, header=True):
        ws.append(r)

    # Get latest Assay controlled-vocab terms from the NAMHub Publications
    # JSON schema, so curators know what to fill in for the "Pending
    # Annotation" column. (Tissue has no controlled vocabulary -- it's a
    # free-text field.)
    schema = requests.get(PUBLICATIONS_SCHEMA_URL, timeout=10).json()
    terms = [
        {"category": "Assay", "value": value}
        for value in schema["properties"]["Assay"]["items"]["enum"]
    ]
    cv_terms = pd.DataFrame(terms)

    ws2 = wb.create_sheet("standard_terms")
    for row in dataframe_to_rows(cv_terms, index=False, header=True):
        ws2.append(row)

    # Style the worksheet.
    ft = Font(bold=True)
    ws2["A1"].font = ft
    ws2["B1"].font = ft
    ws2.column_dimensions["A"].width = 15
    ws2.column_dimensions["B"].width = 65

    wb.save(os.path.join("output", output + ".xlsx"))


def main():
    """Main function."""
    syn = login()
    args = get_args()

    # In order to make >3 Entrez requests/sec, 'email' and 'api_key'
    # params need to be set.
    email = os.getenv("ENTREZ_EMAIL")
    if not email:
        print(
            "⚠️ WARNING: No email address found in the environment.\n"
            "Requests to the Entrez and Unpaywall APIs may be rate-limited or "
            "return incomplete data. For optimal performance, please set the "
            "'ENTREZ_EMAIL' environment variable. See README for more details."
        )

    Entrez.email = email
    Entrez.api_key = os.getenv("ENTREZ_API_KEY")

    supplemental_grant_numbers = None
    if args.supplemental_grants:
        supplemental_grant_numbers = read_supplemental_grants(args.supplemental_grants)
        print(f"Loaded {len(supplemental_grant_numbers)} supplemental grant number(s) "
              f"from {args.supplemental_grants}\n")

    table = find_publications(
        syn, args.grant_id, args.table_id.strip(), email, supplemental_grant_numbers, args.study_id
    )
    if table.empty:
        print("Manifest not generated.")
    else:
        print("Generating manifest... ")

        # Generate manifest with open-access publications listed first.
        generate_manifest(
            table.sort_values(by="publicationAccessibility"), args.output_name
        )

    print("-- DONE --")


if __name__ == "__main__":
    main()
