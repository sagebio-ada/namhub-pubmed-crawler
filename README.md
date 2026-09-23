<h1 align="center">
  NAMHub Pubmed Crawler
</h1>

<h3 align="center">
  Publications manifest generator for
  <a href="https://github.com/sagebio-ada/nam-hub-models" target="_blank">NAMHub</a>
</h3>
<br/>

Modeled after the MC2 Center's
[pubmed-crawler](https://github.com/mc2-center/pubmed-crawler), adapted to
query the NAMHub **Grants** table (`syn75404715`) for grant numbers and
scrape PubMed for new publications, then diff against the NAMHub
**Publications** table (`syn75404744`) so only new publications are output.

Manifests can be generated using Docker or Python (3.12+). Regardless of
approach, a Synapse account is required, as well as an Entrez account
(strongly recommended). Failing to provide Entrez credentials will most
likely result in timeout errors from NCBI.

## :whale: Generate with Docker

### Setup

Create a file called `.env` and update its contents with your Synapse
[Personal Access Token] (PAT) and [NCBI account info].

```
# Synapse Credentials
SYNAPSE_AUTH_TOKEN=<PAT>

# Entrez Credentials
ENTREZ_EMAIL=<email>
ENTREZ_API_KEY=<apikey>
```

### Usage

Run the Docker container, replacing `/path/to/.env` with your path to `.env`.

```
docker run --rm -ti \
  --env-file /path/to/.env \
  --volume $PWD/output:/tmp/output:rw \
  ghcr.io/sagebio-ada/namhub-pubmed-crawler
```

If this is your first time running the command, Docker will first pull the
image (max. 1-2 minutes) before running the container.

To pull the latest Docker changes, run the following command:

```bash
docker pull ghcr.io/sagebio-ada/namhub-pubmed-crawler
```

### Output

Depending on how many new publications have been added to PubMed since the
last scrape (and NCBI's current requests traffic), this step could take
anywhere from 30 seconds to 15ish minutes. Once complete, a manifest will be
found in a folder called `output`, with a name like
`<yyyy-mm-dd>_publications-manifest.xlsx`.

## :snake: Generate with Python

### Setup

1. Clone this repo where you want on your local machine, e.g. current
   directory, `Desktop`, etc.

    ```
    git clone https://github.com/sagebio-ada/namhub-pubmed-crawler.git
    ```

2. In the `namhub-pubmed-crawler` directory, copy `.envTemplate` as `.env`,
   then update its contents with your Synapse [Personal Access Token] (PAT)
   and [NCBI account info].

3. Install [uv] if not already available, then install dependencies:

    ```
    uv sync
    ```

   `.env` is loaded automatically at runtime (via `python-dotenv`), so no
   need to `export` its contents manually.

### Usage

Run the command:

```
uv run pubmed_crawler.py
```

By default, this queries [`syn75404715`] (the NAMHub Grants table) for grant
numbers, and compares any publications found in PubMed against
[`syn75404744`] (the NAMHub Publications table) so that only new
publications are scraped. Both can be overridden:

```
uv run pubmed_crawler.py -g <grant_table_id> -t <publications_table_id>
```

When using a different table of grants, ensure that its schema has at least
the following columns:

- `grantId`
- `grantNumber`

### Supplemental grants

Some NAMHub-relevant publications cite predecessor or related NIH grants
rather than (or in addition to) one of the 6 core NAMHub grants -- e.g. a
TDC's earlier R01/U-series award that a paper's core NAMHub-funded work
grew out of. `supplemental_grants.csv` in this repo tracks such grant
numbers (with a `sourcePmid`/`note` per row for provenance). Pass it with
`-s` to also search PubMed using these grant numbers:

```
uv run pubmed_crawler.py -s supplemental_grants.csv
```

Publications found *only* via a supplemental grant still get a manifest
row, but `grantId` is left blank (they're not linked to a Grants table
entry) and the matching supplemental grant number(s) are recorded in an
extra `secondaryGrantMatch` column for curator review. This isn't run by
default -- new NAMHub publications are expected to cite one of the 6 core
grants going forward, so the core grant set alone should catch them; the
supplemental list is for occasionally checking whether related predecessor
grants turned up anything worth a second look.

Below is the full usage of the script:

```
usage: pubmed_crawler.py [-h] [-g GRANT_ID] [-t TABLE_ID] [-o OUTPUT_NAME] [-s SUPPLEMENTAL_GRANTS]

Get PubMed information from a list of NAMHub grant numbers and put the
results into an xlsx manifest. The Publications table ID is used to scrape
for only new publications.

optional arguments:
  -h, --help            show this help message and exit
  -g GRANT_ID, --grant_id GRANT_ID
                        Synapse table/view ID containing grant numbers in
                        the 'grantNumber' column. (Default: syn75404715, the
                        NAMHub Grants table)
  -t TABLE_ID, --table_id TABLE_ID
                        Synapse table holding already-curated PubMed info,
                        used to filter out publications that have already
                        been found. (Default: syn75404744, the NAMHub
                        Publications table)
  -o OUTPUT_NAME, --output_name OUTPUT_NAME
                        Filename for output manifest. (Default:
                        <current-date>_publications-manifest)
  -s SUPPLEMENTAL_GRANTS, --supplemental_grants SUPPLEMENTAL_GRANTS
                        Path to a CSV (with a 'grantNumber' column) of
                        additional, non-primary grant numbers to also search
                        PubMed with. Not set by default.
```

### Output

Any PMIDs found in PubMed that are not found in the Publications table will
be scraped. Depending on the number of new publications (and NCBI's current
requests traffic), this step could take anywhere from 30 seconds to 15ish
minutes. Once complete, a manifest will be found in a folder called
`output`, with a name like `<yyyy-mm-dd>_publications-manifest.xlsx`.

The manifest's `manifest` sheet has one row per new publication, with
columns matching the live NAMHub Publications table (`camelCase`, e.g.
`pubMedId`, `grantId`) rather than the LinkML/JSON-schema's `PascalCase`
field names. `studyId` is auto-filled whenever `grantId` resolves to one
of the 6 core grants (via the Studies table's own grant linkage);
otherwise, along with `namId`, `assay`, and `tissue`, it's filled in as
`Pending Annotation` for a curator to fill in by hand before the rows are
uploaded to Synapse. `grantId`, `studyId`, `namId`, `assay`, and `tissue`
are all multi-value (`STRING_LIST`) columns on the live table, so
manually-curated values should be comma-separated. A `standard_terms`
sheet lists the current controlled-vocabulary values for `assay` to help
with that curation (`tissue` is free-text, with no controlled
vocabulary). An extra `accessibility` column (not part of the
Publications table) reports each publication's open-access status from
Unpaywall, and is used to sort open-access publications first, since
those are generally easier to review.

<!-- Links -->

[synapse account]: https://www.synapse.org/#!RegisterAccount:0
[personal access token]: https://www.synapse.org/#!PersonalAccessTokens:
[ncbi account info]: https://support.nlm.nih.gov/knowledgebase/article/KA-05317/en-us
[uv]: https://docs.astral.sh/uv/getting-started/installation/
[`syn75404715`]: https://www.synapse.org/#!Synapse:syn75404715/tables/
[`syn75404744`]: https://www.synapse.org/#!Synapse:syn75404744/tables/
[NAMHub Publications schema]: https://github.com/sagebio-ada/nam-hub-models/blob/main/json_schemas/Publications.json
