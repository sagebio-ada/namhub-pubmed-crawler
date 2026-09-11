"""PubMed 'Crawler' of NAMHub Publications.

Discovers publications via PubMed using NAMHub grant numbers, then fetches
metadata in bulk from Europe PMC (faster than using NCBI E-utils). Open-access
status is determined via the Unpaywall API.

Modeled after the MC2 Center's pubmed-crawler
(https://github.com/mc2-center/pubmed-crawler), adapted for the NAMHub
Grants (syn75404715) and Publications (syn75404744) Synapse tables.
"""

import argparse
import getpass
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import requests
import synapseclient
from Bio import Entrez
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils.dataframe import dataframe_to_rows
from synapseclient.models import Table, query

PUBLICATIONS_SCHEMA_URL = (
    "https://raw.githubusercontent.com/sagebio-ada/nam-hub-models/main/"
    "json_schemas/Publications.json"
)
PENDING_ANNOTATION = "Pending Annotation"


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
        "-o",
        "--output_name",
        type=str,
        default=datetime.today().strftime("%Y-%m-%d") + "_publications-manifest",
        help="Filename for output manifest. (Default: <current-date>_publications-manifest)",
    )
    return parser.parse_args()


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


def get_pmids(grants):
    """Get list of PubMed IDs using grant numbers as search param.

    Returns:
        set: PubMed IDs
    """
    print("Getting PMIDs from NCBI... ")
    grant_numbers = grants["grantNumber"].tolist()
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
        normalize_grant_number(number): grant_id
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


def _fetch_oa_status(raw_doi, email):
    """Fetch open-access status from Unpaywall for a single DOI.

    Returns:
        tuple: (raw_doi, accessibility)
    """
    if not raw_doi:
        return raw_doi, "Unknown"
    try:
        response = requests.get(
            f"https://api.unpaywall.org/v2/{raw_doi}?email={email}", timeout=10
        )
        response.raise_for_status()
        if response.json().get("is_oa"):
            return raw_doi, "Open Access"
        return raw_doi, "Restricted Access"
    except (requests.exceptions.HTTPError, requests.exceptions.RequestException, json.JSONDecodeError):
        return raw_doi, "Unknown"


def pull_info(pmids, curr_grants, email):
    """Create dataframe of publications and their pulled data.

    Publication data is pulled in bulk using the Europe PMC API, since it's
    faster than Entrez. Open-access status is pulled from the Unpaywall API.

    Assumptions:
        Number of new publications per run is <1,000, as the Europe PMC API
        has a limit of 1,000 results per request.

    Returns:
        df: publications data, columns matching the NAMHub Publications schema
    """
    pmc_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/searchPOST"
    search_query = " OR ".join(pmids)
    data = {
        "query": search_query,
        "resultType": "core",
        "format": "json",
        "pageSize": 1_000,
    }
    response = json.loads(requests.post(url=pmc_url, data=data).content)
    results = response.get("resultList").get("result")

    # Filter down to only publications that are in the list of PMIDs and not errata.
    filtered_results = [
        r for r in results
        if r.get("pmid") in pmids
        and "Published Erratum" not in r.get("pubTypeList", {}).get("pubType", [])
    ]

    # Fetch OA statuses for all qualifying publications.
    unique_dois = {r.get("doi") for r in filtered_results}
    oa_map = {}
    with ThreadPoolExecutor() as executor:
        for raw_doi, accessibility in executor.map(
            lambda doi: _fetch_oa_status(doi, email), unique_dois
        ):
            oa_map[raw_doi] = accessibility

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
        keywords = result.get("keywordList", {}).get("keyword", "")

        accessibility = oa_map.get(raw_doi, "Unknown")

        grants = result.get("grantsList", {}).get("grant", [])
        grant_ids = match_grant_ids(grants, curr_grants)

        publication_info = {
            "pubMedId": [pmid],
            "pubMedLink": [f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"],
            "publicationTitle": [title],
            "publicationYear": [int(year) if year else None],
            "authors": [", ".join(authors)],
            "journal": [journal],
            # grantId, studyId, namId, dataType, and assay are all
            # multi-value (STRING_LIST) columns on the live Publications
            # table, so multiple values are comma-separated here.
            "keywords": [", ".join(keywords)],
            "doi": [doi],
            "grantId": [", ".join(sorted(grant_ids))],
            "namId": [PENDING_ANNOTATION],
            "studyId": [PENDING_ANNOTATION],
            "dataType": [PENDING_ANNOTATION],
            "assay": [PENDING_ANNOTATION],
            "synapseEntityId": [None],
            "accessibility": [accessibility],
        }
        row = pd.DataFrame(publication_info)
        table.append(row)
    return pd.concat(table)


def find_publications(syn, grant_id, table_id, email):
    """Get list of publications based on NAMHub grants.

    Returns:
        df: publications data
    """
    grants = get_grants(syn, grant_id)
    pmids = get_pmids(grants)

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
        table = pull_info(pmids, grants, email)
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

    # Get latest Assay/DataType controlled-vocab terms from the NAMHub
    # Publications JSON schema, so curators know what to fill in for the
    # "Pending Annotation" columns.
    schema = requests.get(PUBLICATIONS_SCHEMA_URL, timeout=10).json()
    terms = []
    for field in ("Assay", "DataType"):
        for value in schema["properties"][field]["items"]["enum"]:
            terms.append({"category": field, "value": value})
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

    table = find_publications(syn, args.grant_id, args.table_id.strip(), email)
    if table.empty:
        print("Manifest not generated.")
    else:
        print("Generating manifest... ")

        # Generate manifest with open-access publications listed first.
        generate_manifest(
            table.sort_values(by="accessibility"), args.output_name
        )

    print("-- DONE --")


if __name__ == "__main__":
    main()
