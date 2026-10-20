# IDR Literature Pipeline

Builds a literature corpus on **intrinsically disordered regions (IDRs)** for every human protein in
[DisProt](https://disprot.org/browse?sort_field=disprot_id&sort_value=asc&page_size=20&page=0&release=current&show_ambiguous=true&show_obsolete=false&ncbi_taxon_id=9606),
the curated database of disordered proteins (~1,339 human entries).

For each protein, one at a time, it:

1. **Finds papers** in [OpenAlex](https://openalex.org) whose title or abstract mentions the protein (gene symbol,
   synonyms or full name) **and** intrinsic disorder ("intrinsically disordered", "disordered region", ...).
   Research articles and preprints only; every result, no cap.
2. **Gets the Discussion and Conclusion text** from the article XML in PubMed Central's official cloud bucket.
3. **Downloads the full PDF** from free, legal sources: PubMed Central first, then the open-access copies OpenAlex
   knows about (publisher and repository sites).
4. **Files everything per protein**: a spreadsheet of papers plus a folder of PDFs.

It is built for a long, unattended run: it resumes where it stopped, waits out daily API limits, never re-downloads
a file it already has, and keeps going when individual websites fail.

---

## Contents
- [Quick start](#quick-start)
- [Step-by-step setup](#step-by-step-setup)
- [Running it](#running-it)
- [What you get](#what-you-get)
- [Time, cost and disk space](#time-cost-and-disk-space)
- [All options](#all-options)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Running the tests](#running-the-tests)
- [Repository layout](#repository-layout)
- [Older PubMed pipeline](#older-pubmed-pipeline)
- [Data sources and terms of use](#data-sources-and-terms-of-use)

---

## Quick start

```bash
git clone https://github.com/strawvivishortcake/IDR_Literature_Pipeline.git
cd IDR_Literature_Pipeline
python -m pip install -r requirements.txt

# Windows PowerShell                                   # macOS / Linux
$env:OPENALEX_API_KEY="your-openalex-key"              # export OPENALEX_API_KEY="your-openalex-key"
$env:CONTACT_EMAIL="you@university.edu"                # export CONTACT_EMAIL="you@university.edu"

python run_by_protein.py --only TP53,FUS               # 2-protein test (a few minutes)
python run_by_protein.py --full                        # everything (see "Time, cost and disk space")
```

---

## Step-by-step setup

### 1. Install Python 3.10 or newer
- **Windows:** download from [python.org](https://www.python.org/downloads/). In the installer, tick
  **"Add python.exe to PATH"**.
- **macOS:** `brew install python`, or the python.org installer.

Check it worked (open a new terminal first):
```bash
python --version          # should print Python 3.10 or higher
```

### 2. Download the code
With git:
```bash
git clone https://github.com/strawvivishortcake/IDR_Literature_Pipeline.git
cd IDR_Literature_Pipeline
```
Without git: on the GitHub page click **Code → Download ZIP**, unzip it, and open a terminal in that folder
(Windows: open the folder in File Explorer, type `powershell` in the address bar, press Enter).

### 3. Install the libraries
```bash
python -m pip install -r requirements.txt
```

### 4. Get a free OpenAlex API key
OpenAlex requires a key (free, includes $1 of usage per day).
1. Sign up at <https://openalex.org/signup> and click the activation link in the email.
2. Copy your key from <https://openalex.org/settings/api-key>.

### 5. Give the pipeline your key and email
Keys are **never** written into the code. Pick one of these:

**Option A: set them in the terminal** (lasts until you close that window)
```powershell
# Windows PowerShell
$env:OPENALEX_API_KEY="your-openalex-key"
$env:CONTACT_EMAIL="you@university.edu"
```
```bash
# macOS / Linux
export OPENALEX_API_KEY="your-openalex-key"
export CONTACT_EMAIL="you@university.edu"
```

**Option B: a personal launcher** (set once, remembered)
```bash
cp run_local.example.py run_local.py        # Windows: copy run_local.example.py run_local.py
```
Open `run_local.py`, paste your key and email, save. From then on, use `python run_local.py` wherever this README
says `python run_by_protein.py`. `run_local.py` is listed in `.gitignore`, so it is never uploaded to GitHub.

The email is sent to OpenAlex (and NCBI, in PubMed mode) so they can contact you if your requests cause
problems; it's polite and some services require it.

---

## Running it

### 1. Test on two proteins first
```bash
python run_by_protein.py --only TP53,FUS
```
You should see something like this within a minute (your numbers will differ):
```
DP00086 TP53: searching OpenAlex...
  TP53: 412 papers found; getting text and PDFs
  TP53: 10/412 papers | PDFs 3 | ~2 min left for this protein
...
[1/2] DP00086 TP53: done | 412 papers | PDFs 140, text-only 20, none 252 | 95s | ~2 min left | OpenAlex allowance left: 9970/10000
```
Open `by_protein_openalex/proteins/` to check a few PDFs.

`--only` takes gene symbols or DisProt IDs: `--only DP00086,SNCA`. `--proteins 20` runs the first 20 proteins.

### 2. Full run
```bash
python run_by_protein.py --full --refresh-proteins
```
`--refresh-proteins` downloads the current DisProt protein list; you only need it on the first full run.

Before a long run:
- **Keep the computer awake and plugged in.** Windows: Settings → System → Power → Sleep: *Never*.
  macOS: prefix the command with `caffeinate -i`.
- **Don't press Ctrl+C in the terminal to copy text.** With nothing selected, Ctrl+C stops the program
  (safely; see below). To watch progress, open a **second** terminal and run:
  ```powershell
  Get-Content by_protein_openalex\run.log -Tail 20 -Wait      # Windows (Ctrl+C here only stops watching)
  ```
  ```bash
  tail -f by_protein_openalex/run.log                          # macOS / Linux
  ```
- **Avoid opening the pipeline's CSV files in Excel while it runs.** Excel locks open files. If you do, the run
  pauses with a "close it in Excel" message and continues once you close the file.

### 3. Resuming
Stopping is always safe: Ctrl+C, a crash, a closed laptop, a dropped connection. Progress is saved after every
paper and every protein. To continue, run the **same command without `--refresh-proteins`**:
```bash
python run_by_protein.py --full
```
It skips finished proteins, retries any marked `incomplete`, and never downloads a file twice.

### 4. When OpenAlex's daily allowance runs out
The free tier covers roughly 450-500 proteins a day. When it's used up, the run stops cleanly with:
```
Stopping: OpenAlex's daily allowance for this API key is used up. It resets at midnight UTC ...
```
Run `python run_by_protein.py --full` again after midnight UTC (8 pm US Eastern in summer, 7 pm in winter).
To finish in one go instead, add a few dollars of prepaid credit at <https://openalex.org/settings/usage>.

### 5. After it finishes
1. Run `python run_by_protein.py --full` once more. It only retries proteins that ended `incomplete`.
2. Months later, `python run_by_protein.py --full --retry-missing` re-checks papers that had no free PDF
   (papers become open access over time).

---

## What you get

Everything is written to `by_protein_openalex/` next to the code:

```
by_protein_openalex/
├── papers_master.csv          one row per unique paper, with every protein it belongs to (start here)
├── protein_paper_links.csv    one row per protein-paper pair (no long text columns; quick to open)
├── protein_status.csv         one row per protein: status, paper and PDF counts
├── proteins/
│   └── DP00086_TP53/
│       ├── papers.csv         every paper found for this protein, incl. Discussion/Conclusion text
│       └── pdfs/
│           ├── W2590568317_PMC7434221.pdf          <OpenAlex ID>_<PMC ID>.pdf
│           ├── W3021435577_noPMC.pdf               (no PMC copy; came from the publisher or a repository)
│           └── W2911964829_PMC6500000_fulltext.txt (license allows text but not the PDF)
├── _pdf_store/                one copy of each file; protein folders hold links to it, so a paper shared by
│                              five proteins takes disk space once
├── run.log                    everything printed on screen
└── text_cache.csv, pdf_registry.csv, protein_progress.txt    bookkeeping for resuming (leave these alone)
```

### Key columns in `papers.csv` / `papers_master.csv`

| Column | Meaning |
|---|---|
| `paper_id` | OpenAlex work ID (e.g. `W2590568317`); the unique key. Many papers have no PMID. |
| `PMID`, `PMCID`, `DOI` | identifiers when they exist |
| `Track` | `IDR_general`, plus `IDR_interaction` if the title/abstract mentions binding, affinity, Kd, phase separation or condensates |
| `ambiguous_symbol` | gene symbol is 3 characters or fewer (e.g. `SET`), so worth spot-checking for false matches |
| `pdf_status` | `pdf` · `text_only` (plain-text full text saved) · `not_available` (no free copy, or the site blocks scripts) · `error` (temporary; retried next run) |
| `pdf_source` | `pmc_cloud` · `openalex_oa_link` · `openalex_cached` (paid) |
| `discussion`, `conclusion` | section text; `text_source` says where it came from: `pmc_xml`, `bioc`, or `+pdf` (extracted from the PDF) |
| `is_retracted` | `True` if retracted (Retraction Watch via OpenAlex); check before citing |
| `license`, `oa_status` | reuse license of the open-access copy; gold / green / hybrid / bronze / closed |

### `protein_status.csv` status values
`done` · `incomplete` (some papers hit temporary errors; rerunning fixes it) · `failed_search` ·
`skipped_no_search_term` (DisProt entry has no usable gene symbol or name).

---

## Time, cost and disk space

| | Typical full run (~1,339 proteins) |
|---|---|
| **Time** | ~9-12 hours of running (about 2 proteins/minute) |
| **OpenAlex search cost** | ~$2-5, i.e. about 3 days on the free $1/day allowance (it stops and resumes automatically) |
| **PDF cost** | $0. Free sources only, unless you opt in with `--openalex-pdfs N` ($0.01 each) |
| **Disk** | budget 50-150 GB free |
| **PDF coverage** | roughly a third to a half of papers. The rest are paywalled, or on sites that block scripts (e.g. bioRxiv) |

---

## All options

```bash
python run_by_protein.py [options]
```

| Option | What it does |
|---|---|
| *(none)* | test run on the first 10 proteins |
| `--only TP53,DP00070` | just these proteins (gene symbols and/or DisProt IDs) |
| `--proteins N` | the first N proteins |
| `--full` | every protein |
| `--refresh-proteins` | re-download the DisProt protein list |
| `--workers N` | papers downloaded at the same time (default 12; never more than 2 at once from any one website) |
| `--max-papers N` | keep only the N most relevant papers per protein (default: all) |
| `--idr-acronyms` | also match bare `IDR`/`IDP` acronyms. More papers, many off-topic: OpenAlex ignores case, so "IDP" also matches "internally displaced persons" |
| `--openalex-pdfs N` | buy up to N cached PDFs from OpenAlex this run, only where no free copy exists ($0.01 each; needs prepaid credit) |
| `--no-pdf` | text only, no PDF downloads |
| `--no-text-fallback` | don't save plain-text full text when a PDF isn't allowed |
| `--retry-missing` | re-check papers that had no free PDF last time |
| `--redo` | re-search proteins already marked done (downloaded files are reused) |
| `--fresh` | forget progress and caches (PDFs on disk are kept and reused) |
| `--source pubmed` | search PubMed instead of OpenAlex (outputs go to `by_protein/`; needs `CONTACT_EMAIL`) |

Settings (environment variables): `OPENALEX_API_KEY` (required for OpenAlex), `CONTACT_EMAIL` (recommended;
required for `--source pubmed`), `NCBI_API_KEY` (optional, PubMed mode).

Search details you can tune at the top of `openalex_search.py`: `IDR_PHRASES` (the disorder terms),
`EXCLUDE_REVIEWS` (articles + preprints only), `MAX_RESULTS_PER_PROTEIN`.

---

## How it works

```
DisProt API ──► protein list (gene symbol, synonyms, name)
                     │   one protein at a time; the next protein's search runs in the background
                     ▼
OpenAlex search:  (symbol OR synonyms OR full name) AND ("intrinsically disordered" OR "disordered region" OR ...)
                     │   every result, 100 per page
                     ▼
for each paper (12 at once):
   Discussion / Conclusion ── PMC Cloud article XML ── (NCBI BioC API, only if the bucket is unreachable)
   full PDF ── PMC Cloud bucket ── open-access PDF links from OpenAlex ── (OpenAlex cached copy, paid, opt-in)
                     ▼
by_protein_openalex/proteins/<DisProt ID>_<gene>/papers.csv + pdfs/
```

- **PMC Cloud Service** (`pmc-oa-opendata` on AWS) is NCBI's official route for automated downloads of PMC's
  open-access articles, built for bulk use. OpenAlex often omits the PMC ID, so the pipeline also reads it from
  OpenAlex's location data, which lets those papers reach the free PMC copy.
- **Politeness:** at most 2 simultaneous downloads per website, 0.5 s between starts. Any single download gives up
  after 2 minutes, and "retry in an hour" responses are ignored (that paper is retried next run instead).
- **Safety:** every file is checked to really be a PDF (not a login page); PMC files are checksum-verified.
- **Stuck detection:** if nothing completes for 5 minutes, `by_protein_openalex/stuck_report.txt` records exactly
  where the program is waiting.

---

## Troubleshooting

| Message / symptom | What it means | Fix |
|---|---|---|
| `No OpenAlex API key` | `OPENALEX_API_KEY` isn't set in this terminal | set it (setup step 5) or use `run_local.py` |
| `OpenAlex rejected the API key (HTTP 401/403)` | wrong key, or not activated yet | re-copy the key; click the activation email |
| `daily allowance ... is used up` | free tier spent for today | rerun after midnight UTC, or add prepaid credit |
| `Can't write ... it's probably open in Excel` | a CSV is open and locked | close it; the run continues by itself |
| `skipped unreachable site ...` | that website is down or blocks scripts | nothing; the paper is recorded without a PDF |
| `MuPDF error: ...` | a badly formed PDF | nothing; text is still extracted when possible |
| `paper W... took 80s` | one slow website | nothing; downloads are capped at 2 minutes |
| `python` not found (Windows) | Python not on PATH | reinstall with "Add python.exe to PATH", or use `py` instead of `python` |
| `Interrupted - progress is saved` | Ctrl+C was pressed | run the same command again |
| looks frozen | | check `by_protein_openalex/stuck_report.txt` and `run.log` |

---

## Running the tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests          # ~40 tests, a few seconds, no internet needed (all network calls are faked)
```
GitHub Actions runs the same tests on every push (Windows + Linux, Python 3.10-3.12): see the **Actions** tab.

---

## Repository layout

| File | Role |
|---|---|
| `run_by_protein.py` | **main entry point**: per-protein runner, resuming, parallel downloads, outputs |
| `openalex_search.py` | OpenAlex search: query building, cursor paging, budget handling, parsing works |
| `pdf_sources.py` | full text and PDFs: PMC Cloud bucket, open-access links, Unpaywall, per-site politeness |
| `00_fetch_disprot_proteins.py` | downloads the DisProt human protein list |
| `01_fetch_idr_pubmed.py` … `05_pdf_fallback_scraper.py` | the older step-by-step PubMed pipeline (see below); shared helpers are reused by the runner |
| `run_pipeline.py` | one-command runner for the older pipeline |
| `run_local.example.py` | template for a personal launcher holding your keys (copy to `run_local.py`) |
| `tests/` | offline tests |
| `.github/workflows/tests.yml` | runs the tests on GitHub |

---

## Older PubMed pipeline

The original version searches PubMed and runs as six separate steps. It still works and is kept for comparison:
```bash
python run_by_protein.py --source pubmed --only TP53     # per-protein runner, PubMed search -> by_protein/
python run_pipeline.py --full                            # classic steps 0-5, outputs in the repo folder
```
Both need `CONTACT_EMAIL` (NCBI requires it); `NCBI_API_KEY` is optional. NCBI rate-limits heavy use (HTTP 429/503),
which is why OpenAlex is now the default.

---

## Data sources and terms of use

- **DisProt**: protein list. <https://disprot.org>
- **OpenAlex**: paper search and open-access locations (CC0 metadata). API key required; usage-based pricing.
  <https://help.openalex.org/api/>
- **PubMed Central Cloud Service**: article XML and PDFs from the PMC Open Access Subset and author manuscripts.
  Acknowledge NLM as the source. <https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/>
- **Publisher / repository open-access copies**: downloaded only from links OpenAlex marks as open access.

Downloaded PDFs keep their original copyright. Check each paper's `license` before redistributing, and don't
re-host the PDFs publicly. This is also why outputs are kept out of this repository.

bioRxiv blocks scripted downloads from its website and offers bulk access through its text-and-data-mining bucket
instead (<https://www.biorxiv.org/tdm>). Preprints later published in a journal are usually picked up through the
published version.
