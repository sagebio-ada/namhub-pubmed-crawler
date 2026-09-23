"""Validate a NAMHub Publications manifest and append it to the live table.

Reads the "manifest" sheet of an xlsx produced by pubmed_crawler.py or
import_legacy_publications.py, validates it against the live Publications
table's actual column schema, converts multi-value cells (comma-separated
in the manifest, for human readability) into real list values, skips any
row whose pubMedId is already in the table, and appends the rest.

Unlike the MC2 Center's sync_publications.py, this does NOT truncate and
fully replace the table -- pubmed_crawler.py-generated manifests are
inherently incremental (only new publications not already in the table),
so an append-only, dedupe-by-pubMedId approach fits how it's actually used.
A snapshot version of the table is taken before every write, for rollback.

Usage:
    python upload_publications.py <manifest.xlsx> [-t TABLE_ID] [--dry-run]
"""

import argparse

import pandas as pd

from pubmed_crawler import login

# Synapse columnType -> how to convert a manifest cell (str or None) into
# the value synapseclient expects for a Table row.
LIST_TYPES = {"STRING_LIST"}
INTEGER_TYPES = {"INTEGER"}

# Manifest-only helper columns that don't correspond to a table column (e.g.
# "accessibility", added by pubmed_crawler.py/import_legacy_publications.py
# to help curators sort/prioritize review) and are dropped before upload.
HELPER_COLUMNS = {"accessibility", "secondaryGrantMatch"}


def get_args():
    """Set up command-line interface and get arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest_path", type=str, help="Path to the manifest xlsx.")
    parser.add_argument(
        "-t",
        "--table_id",
        type=str,
        default="syn75404744",
        help="Synapse ID of the Publications table. (Default: syn75404744)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview only; do not write to Synapse.",
    )
    return parser.parse_args()


def load_manifest(path):
    """Load the "manifest" sheet of an xlsx as a dataframe of strings."""
    return pd.read_excel(path, sheet_name="manifest", dtype=str)


def validate_headers(manifest, columns):
    """Raise if the manifest's columns don't exactly match the table's.

    This is the exact bug class that made a previous version of this
    crawler silently generate manifests with PascalCase headers
    (e.g. "PubMedId") that didn't match the live table's camelCase
    columns (e.g. "pubMedId") -- catch it here before any upload.
    """
    table_cols = {c["name"] for c in columns}
    manifest_cols = set(manifest.columns)
    unknown = manifest_cols - table_cols - HELPER_COLUMNS
    if unknown:
        raise ValueError(
            f"Manifest has column(s) not present on {len(table_cols)}-column "
            f"live table: {sorted(unknown)}"
        )


def to_cell_value(raw, column_type):
    """Convert one manifest cell into the value synapseclient expects."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or raw == "":
        return [] if column_type in LIST_TYPES else None
    if column_type in LIST_TYPES:
        return [v.strip() for v in str(raw).split(",") if v.strip()]
    if column_type in INTEGER_TYPES:
        return int(float(raw))
    return str(raw)


def build_rows(manifest, columns, existing_pmids):
    """Convert manifest rows into Synapse row lists, in table column order.

    Skips rows whose pubMedId is already present in the live table.

    Returns:
        tuple: (list of row-value lists, number of rows skipped as duplicates)
    """
    col_names = [c["name"] for c in columns]
    col_types = {c["name"]: c["columnType"] for c in columns}

    rows = []
    skipped = 0
    for _, record in manifest.iterrows():
        pmid = record.get("pubMedId")
        if pmid in existing_pmids:
            skipped += 1
            continue
        rows.append(
            [to_cell_value(record.get(name), col_types[name]) for name in col_names]
        )
    return rows, skipped


def main():
    """Main function."""
    syn = login()
    args = get_args()

    manifest = load_manifest(args.manifest_path)
    print(f"Loaded {len(manifest)} row(s) from {args.manifest_path}")

    columns = list(syn.getTableColumns(args.table_id))
    validate_headers(manifest, columns)
    print("Manifest columns match the live table schema.")

    existing_pmids = set(
        syn.tableQuery(f'SELECT "pubMedId" FROM {args.table_id}')
        .asDataFrame()["pubMedId"]
        .astype(str)
    )
    rows, skipped = build_rows(manifest, columns, existing_pmids)
    print(f"{len(rows)} new row(s) to add; {skipped} skipped (pubMedId already in table)")

    if args.dry_run:
        print("\n[dry-run] No changes written to Synapse.")
        return

    if not rows:
        print("Nothing to upload.")
        return

    import synapseclient
    from datetime import datetime

    label = datetime.today().strftime("%Y-%m-%d-%H-%M-%S")
    print(f"Creating snapshot version of {args.table_id} (label: {label})...")
    syn.create_snapshot_version(args.table_id, label=label)

    print(f"Appending {len(rows)} row(s) to {args.table_id}...")
    syn.store(synapseclient.Table(args.table_id, rows))
    print("-- DONE --")


if __name__ == "__main__":
    main()
