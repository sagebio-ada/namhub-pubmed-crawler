"""Import a NIH RePORTER-style publications CSV into a NAMHub Publications manifest.

Some NAMHub-relevant publications predate the program's own grants (e.g.
tied to predecessor/related NIH awards) and were surfaced by a collaborator
via NIH RePORTER's Publications search rather than found through PubMed's
own [Grant number] field indexing (see pubmed_crawler.py's known gap there).
This script converts that kind of export -- one row per publication, with
PMID/Title/Authors/.../DOI columns followed by one or more repeated
"Grant/funding" columns -- into an xlsx manifest in the same format
pubmed_crawler.py produces, ready for curator review before upload.

Any grant number in a row's funding columns that matches one of the NAMHub
Grants table's entries is recorded in that row's `grantId`; rows with no
such match still get a manifest entry (so nothing from the source list is
silently dropped), just with `grantId` left blank for a curator to assess.

Usage:
    python import_legacy_publications.py <input.csv> [-g GRANT_ID] [-o OUTPUT_NAME]
"""

import argparse
import csv
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
from Bio import Entrez

from pubmed_crawler import (
    PENDING_ANNOTATION,
    NIH_GRANT_NUMBER_RE,
    _fetch_oa_status,
    base_grant_number,
    generate_manifest,
    get_europepmc_records,
    get_grants,
    get_keywords,
    get_related_info,
    get_studies,
    login,
    parse_dbgap,
    parse_geo,
    parse_sra,
)


def get_args():
    """Set up command-line interface and get arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_csv",
        type=str,
        help="NIH RePORTER-style publications CSV to import.",
    )
    parser.add_argument(
        "-g",
        "--grant_id",
        type=str,
        default="syn75404715",
        help="Synapse table/view ID for the NAMHub Grants table. (Default: syn75404715)",
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
        default=datetime.today().strftime("%Y-%m-%d") + "_legacy-publications-manifest",
        help="Filename for output manifest. (Default: <current-date>_legacy-publications-manifest)",
    )
    return parser.parse_args()


def read_reporter_csv(path):
    """Read a NIH RePORTER-style publications CSV.

    Returns:
        list[dict]: one dict per row with keys pmid, title, authors, journal,
            year, doi, and grant_numbers (the set of NIH grant numbers found
            across that row's repeated "Grant/funding" columns).
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    funding_start = header.index("Grant/funding")
    records = []
    for row in rows:
        funding_cells = [c for c in row[funding_start:] if c.strip()]
        grant_numbers = set()
        for cell in funding_cells:
            for m in NIH_GRANT_NUMBER_RE.finditer(cell.upper().replace(" ", "")):
                grant_numbers.add(m.group(0))
        records.append(
            {
                "pmid": row[0].strip(),
                "title": row[1].strip(),
                "authors": row[2].strip(),
                "journal": row[5].strip(),
                "year": row[6].strip(),
                "doi": row[10].strip(),
                "grant_numbers": grant_numbers,
            }
        )
    return records


def match_grant_ids(grant_numbers, curr_grants):
    """Match a row's raw grant numbers to NAMHub grantIds.

    Returns:
        set: matched NAMHub grantId values
    """
    known = {
        base_grant_number(number): grant_id
        for number, grant_id in zip(curr_grants["grantNumber"], curr_grants["grantId"])
    }
    return {known[g] for g in grant_numbers if g in known}


def build_table(records, curr_grants, email, studies_by_grant=None):
    """Build a manifest dataframe, one row per input record."""
    unique_dois = {r["doi"] for r in records if r["doi"]}
    oa_map = {}
    with ThreadPoolExecutor() as executor:
        for raw_doi, accessibility in executor.map(
            lambda doi: _fetch_oa_status(doi, email), unique_dois
        ):
            oa_map[raw_doi] = accessibility

    all_pmids = {r["pmid"] for r in records if r["pmid"]}
    related_info_map = get_related_info(all_pmids)
    europepmc_by_pmid = {rec["pmid"]: rec for rec in get_europepmc_records(all_pmids)}

    rows = []
    for r in records:
        grant_ids = match_grant_ids(r["grant_numbers"], curr_grants)
        study_ids = {
            (studies_by_grant or {})[gid] for gid in grant_ids if gid in (studies_by_grant or {})
        }
        related_info = related_info_map.get(r["pmid"], {})
        gse_ids = parse_geo(related_info.get("gds"))
        _, srp = parse_sra(related_info.get("sra"))
        dbgaps = parse_dbgap(related_info.get("gap"))
        dataset_ids = {*gse_ids, *srp, *dbgaps}
        epmc_record = europepmc_by_pmid.get(r["pmid"], {})
        raw_abstract = epmc_record.get("abstractText")
        abstract = raw_abstract.replace("<h4>", " ").replace("</h4>", ": ").strip() if raw_abstract else None
        publication_info = {
            "pubMedId": [r["pmid"]],
            "pubMedLink": [f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}"],
            "publicationTitle": [r["title"]],
            "publicationYear": [int(r["year"]) if r["year"] else None],
            "authors": [r["authors"]],
            "journal": [r["journal"]],
            "abstract": [abstract],
            "keywords": [", ".join(get_keywords(epmc_record))],
            "doi": ["https://doi.org/" + r["doi"] if r["doi"] else None],
            "grantId": [", ".join(sorted(grant_ids))],
            "namId": [PENDING_ANNOTATION],
            "studyId": [", ".join(sorted(study_ids)) if study_ids else PENDING_ANNOTATION],
            "assay": [PENDING_ANNOTATION],
            "tissue": [PENDING_ANNOTATION],
            "datasetAlias": [", ".join(sorted(dataset_ids))],
            "publicationAccessibility": [oa_map.get(r["doi"])],
        }
        rows.append(pd.DataFrame(publication_info))
    return pd.concat(rows)


def main():
    """Main function."""
    syn = login()
    args = get_args()
    email = os.getenv("ENTREZ_EMAIL")
    Entrez.email = email
    Entrez.api_key = os.getenv("ENTREZ_API_KEY")

    curr_grants = get_grants(syn, args.grant_id)
    studies_by_grant = get_studies(syn, args.study_id)
    records = read_reporter_csv(args.input_csv)
    print(f"Read {len(records)} publication(s) from {args.input_csv}")

    matched = sum(1 for r in records if match_grant_ids(r["grant_numbers"], curr_grants))
    print(f"  {matched} row(s) matched a NAMHub grant directly; "
          f"{len(records) - matched} left with grantId blank for curator review\n")

    table = build_table(records, curr_grants, email, studies_by_grant)
    generate_manifest(table.sort_values(by="publicationAccessibility"), args.output_name)
    print("-- DONE --")


if __name__ == "__main__":
    main()
