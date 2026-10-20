"""
Per-protein runner: processes DisProt proteins ONE AT A TIME, start to finish, and downloads full-text PDFs.

For each protein, in DisProt order:
  1. search for papers mentioning the protein (gene symbol / synonyms / full name) AND IDR terms
       --source openalex  (default) OpenAlex, titles + abstracts. Needs a free key in OPENALEX_API_KEY.
       --source pubmed    NCBI PubMed (the original route)
  2. Discussion + Conclusion text from the article XML in the PMC Cloud bucket (NCBI BioC as a slow fallback)
  3. full PDF, first one that works:
       PMC Cloud bucket -> open-access PDF links from OpenAlex -> (Unpaywall, PubMed mode) -> OpenAlex's cached copy
       (paid, $0.01 each, only with --openalex-pdfs N). If PMC only has text for that license, the plain-text full
       text is saved instead. Missing Discussion/Conclusion are pulled out of the PDF.
  4. write <output>/proteins/<DisProt ID>_<gene>/papers.csv and the PDFs next to it, then mark the protein done

    python run_by_protein.py                    # TEST: first 10 proteins
    python run_by_protein.py --only TP53,FUS
    python run_by_protein.py --full             # all ~1,339 human proteins (Ctrl-C and re-run to resume)

Options:
    --source openalex|pubmed   search source (default openalex); outputs go to by_protein_openalex/ or by_protein/
    --openalex-pdfs N    also buy up to N PDFs per run from OpenAlex's cache when no free copy exists ($0.01 each)
    --no-pdf             text only (skip PDF downloads)
    --no-unpaywall       PubMed mode: skip the Unpaywall fallback
    --no-text-fallback   don't save PMC plain text when a paper has no PDF
    --retry-missing      re-check papers that had no PDF on an earlier run (e.g. newly open access)
    --redo               re-process proteins already marked done (searches again; caches keep downloads)
    --fresh              forget progress and caches (PDF files already on disk are kept and reused)
    --refresh-proteins   re-download the DisProt protein list

Credentials come from the environment: OPENALEX_API_KEY (OpenAlex), NCBI_EMAIL / NCBI_API_KEY (PubMed).
Everything printed is also saved to <output>/run.log.
"""
from __future__ import annotations

import argparse
import csv
import faulthandler
import importlib.util
import logging
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback
from pathlib import Path

import pandas as pd

import pdf_sources
import openalex_search

BASE = Path(__file__).resolve().parent
PROTEINS_CSV = BASE / "disprot_human_proteins.csv"
FULL_LIST_CSV = BASE / "disprot_full.csv"  # run_pipeline.py's test mode keeps the full list here
PAPERS_CACHE = BASE / "idr_papers_cache.csv"   # PubMed metadata cache (shared with 01_fetch_idr_pubmed.py)

# output locations - set by configure_output() for the chosen source
OUT = BASE / "by_protein_openalex"
PROTEIN_DIR = PDF_STORE = TEXT_CACHE = PDF_REGISTRY = STATUS_CSV = PROGRESS = LOG_FILE = None


def configure_output(source: str) -> None:
    global OUT, PROTEIN_DIR, PDF_STORE, TEXT_CACHE, PDF_REGISTRY, STATUS_CSV, PROGRESS, LOG_FILE
    OUT = BASE / ("by_protein" if source == "pubmed" else "by_protein_openalex")
    PROTEIN_DIR = OUT / "proteins"
    PDF_STORE = OUT / "_pdf_store"         # one copy of each file; protein folders get hard links (no extra disk space)
    TEXT_CACHE = OUT / "text_cache.csv"
    PDF_REGISTRY = OUT / "pdf_registry.csv"
    STATUS_CSV = OUT / "protein_status.csv"
    PROGRESS = OUT / "protein_progress.txt"
    LOG_FILE = OUT / "run.log"


configure_output("openalex")

DEFAULT_TEST_PROTEINS = 10
PROGRESS_EVERY = 10                        # log a progress line every N papers
SLOW_PAPER_SEC = 60                        # warn when one paper takes longer than this
WORKERS = 12                               # papers fetched at the same time (max 2 per publisher site)
WATCHDOG_SEC = 300                         # if one step takes longer, write where it is stuck to stuck_report.txt
BIOC_DELAY = 1.0                           # BioC is only a fallback; 1 request/sec (NCBI answers 429 above this)
BIOC_MAX_FAILS = 5                         # this many BioC errors in a row (e.g. NCBI 503s) ...
BIOC_PAUSE_MIN = 15                        # ... and BioC is skipped for this many minutes
FETCH_BATCH = 200
MISSING_TEXT = {"Not Found", "API Fetch Failed", "PDF Download Failed", ""}

logger = logging.getLogger("by_protein")

PAPER_OUT_COLS = ["disprot_id", "gene", "paper_id", "PMID", "PMCID", "DOI", "Date", "Title", "Journal", "Authors",
                  "Article Type", "Track", "Interaction_In_Abstract", "ambiguous_symbol", "oa_status",
                  "pdf_status", "pdf_source", "pdf_file", "fulltext_file", "pmc_version", "is_retracted", "license",
                  "text_source", "discussion", "conclusion", "notes", "Abstract", "URL"]
TEXT_COLS = ["pmcid", "discussion", "conclusion", "text_status"]
REG_COLS = ["paper_id", "pdf_status", "pdf_source", "pdf_file", "fulltext_file", "pmc_version", "is_retracted",
            "license", "notes"]


# ------------------------------------------------------------------ small helpers
def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def slug(text: str, n: int = 30) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "")).strip("_")
    return s[:n] or "unnamed"


def clean(v) -> str:
    v = "" if v is None else str(v).strip()
    return "" if v.lower() == "nan" else v


LOCK_WAIT_SEC = 1800                       # wait up to 30 min for a file someone has open (e.g. in Excel)


def write_retry(path, write) -> None:
    """
    Run write(); on Windows a CSV that's open in Excel can't be written ("Permission denied").
    Instead of crashing, wait and keep retrying, with a message saying which file to close.
    """
    started, warned = time.monotonic(), 0.0
    while True:
        try:
            return write()
        except PermissionError:
            waited = time.monotonic() - started
            if waited > LOCK_WAIT_SEC:
                raise
            if waited - warned >= 30 or warned == 0:
                logger.warning(f"Can't write {Path(path).name} - it's probably open in Excel or another program. "
                               f"Close it and the run continues automatically (waited {waited:.0f}s).")
                warned = waited or 0.001
            time.sleep(5)


class AppendCache:
    """A CSV keyed by one column; new rows are appended immediately, so a crash loses nothing."""
    def __init__(self, path: Path, cols: list[str], key: str, legacy_key: str | None = None):
        self.path, self.cols, self.key = path, cols, key
        self.data: dict[str, dict] = {}
        if path.exists():
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            if key not in df.columns and legacy_key in df.columns:      # older files used 'pmid'
                df = df.rename(columns={legacy_key: key})
                df.reindex(columns=cols).to_csv(path, index=False)
            for r in df.to_dict("records"):
                self.data[r[key]] = r          # later rows win (e.g. a retried paper)

    def get(self, k):
        return self.data.get(k)

    _lock = threading.Lock()

    def add(self, row: dict) -> None:
        row = {c: clean(row.get(c, "")) for c in self.cols}
        with self._lock:
            self.data[row[self.key]] = row
            def write():
                new = not self.path.exists()
                with open(self.path, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=self.cols)
                    if new:
                        w.writeheader()
                    w.writerow(row)
            write_retry(self.path, write)


def link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        os.link(src, dest)          # same file on disk, appears in both places
    except OSError:
        shutil.copy2(src, dest)


# ------------------------------------------------------------------ the runner
class Runner:
    def __init__(self, args):
        self.args = args
        self.source = getattr(args, "source", "pubmed")
        OUT.mkdir(parents=True, exist_ok=True)
        self.s1 = load_module("step1", "01_fetch_idr_pubmed.py")
        self.s3 = load_module("step3", "03_api_text_extractor.py")
        self.s5 = load_module("step5", "05_pdf_fallback_scraper.py")
        self.email = self.s1.Entrez.email
        self._local = threading.local()
        self._lock = threading.Lock()          # BioC fallback + paid-PDF counter
        self.bioc = self.s3.create_requests_session()
        self.bioc_fails, self.bioc_paused_until = 0, 0.0
        self.oa = None
        self.paid_pdfs_left = int(getattr(args, "openalex_pdfs", 0) or 0)
        if self.source == "openalex":
            self.oa = openalex_search.OpenAlexClient(os.environ.get("OPENALEX_API_KEY", "").strip(), self.email)

        self.papers: dict[str, dict] = {}       # PubMed metadata cache
        if self.source == "pubmed" and PAPERS_CACHE.exists():
            for r in pd.read_csv(PAPERS_CACHE, dtype=str, keep_default_na=False).to_dict("records"):
                self.papers[r["PMID"]] = r
        self.text = AppendCache(TEXT_CACHE, TEXT_COLS, "pmcid")
        self.registry = AppendCache(PDF_REGISTRY, REG_COLS, "paper_id", legacy_key="pmid")
        self.status: dict[str, dict] = ({r["disprot_id"]: r for r in pd.read_csv(STATUS_CSV, dtype=str).to_dict("records")}
                                        if STATUS_CSV.exists() else {})
        self.done = set(PROGRESS.read_text().split()) if PROGRESS.exists() else set()
        self._watchdog_file = open(OUT / "stuck_report.txt", "a")

    def session(self):
        """One HTTP session per worker thread (requests sessions aren't guaranteed thread-safe)."""
        if not hasattr(self._local, "http"):
            self._local.http = pdf_sources.make_session()
        return self._local.http

    # ---- PubMed metadata (one efetch per 200 new papers)
    def ensure_papers(self, pmids: list[str]) -> list[str]:
        s1 = self.s1
        todo = [p for p in pmids if p not in self.papers]
        for start in range(0, len(todo), FETCH_BATCH):
            batch = todo[start:start + FETCH_BATCH]
            records = s1.entrez_retry(s1.Entrez.efetch, db="pubmed", id=",".join(batch), retmode="xml")
            time.sleep(s1.DELAY)
            if not records:
                continue
            rows = [s1.parse_pubmed_article(a) for a in records.get("PubmedArticle", [])]
            if rows:
                pd.DataFrame(rows, columns=s1.PAPER_COLS).to_csv(PAPERS_CACHE, mode="a", header=not PAPERS_CACHE.exists(), index=False)
                for r in rows:
                    self.papers[r["PMID"]] = r
        return [p for p in pmids if p not in self.papers]

    # ---- search: returns (list of (paper dict, info dict), capped, status) ; status: ok | failed | incomplete
    def search_pubmed(self, row: dict, clause: str):
        s1 = self.s1
        result = s1.search_one_protein(row, clause)
        if result is None:
            return [], False, "failed"
        hits, log_rows = result
        capped = any(str(r["capped"]) == "True" for r in log_rows)
        if not hits:
            return [], capped, "ok"
        hits_df = pd.DataFrame(hits)
        pmids = sorted(hits_df["pmid"].astype(str).unique())
        missing = self.ensure_papers(pmids)
        papers_df = pd.DataFrame([self.papers[p] for p in pmids if p in self.papers], columns=s1.PAPER_COLS)
        links = s1.build_links(hits_df, papers_df)
        if links.empty:
            return [], capped, "incomplete" if missing else "ok"
        per_paper = links.groupby("pmid").agg(
            Track=("track", lambda x: ", ".join(sorted(set(x)))),
            Interaction_In_Abstract=("interaction_match", "any"), ambiguous_symbol=("ambiguous_symbol", "first"))
        items = []
        for pmid, info in per_paper.iterrows():
            p = {**self.papers[pmid], "paper_id": pmid}
            items.append((p, info.to_dict()))
        return items, capped, "incomplete" if missing else "ok"

    def search_openalex(self, row: dict):
        terms = self.s1.search_terms(row)
        result = self.oa.search_protein(terms, getattr(self.args, "max_papers", None))   # BudgetExhausted stops the run
        if result is None:
            return [], False, "failed"
        works, total, capped = result
        gene = clean(row.get("gene"))
        if capped:
            logger.warning(f"{row['disprot_id']} ({gene}): {total} OpenAlex hits, kept the top {len(works)} by relevance "
                           f"(--max-papers).")
        items = []
        for w in works:
            info = {"Track": "IDR_general, IDR_interaction" if w["Interaction_In_Abstract"] else "IDR_general",
                    "Interaction_In_Abstract": w["Interaction_In_Abstract"], "ambiguous_symbol": 0 < len(gene) <= 3}
            items.append((w, info))
        return items, capped, "ok"

    # ---- Discussion / Conclusion
    def get_text(self, pmcid: str) -> tuple[str, str, str]:
        """
        Primary source: the article XML in the PMC Cloud bucket (AWS, built for bulk downloads).
        NCBI's BioC API is only tried, slowly, when the bucket itself can't be reached.
        Status: ok_pmc_xml | ok (BioC) | not_oa | no_pmcid | error.
        """
        if not pmcid:
            return "Not Found", "Not Found", "no_pmcid"
        cached = self.text.get(pmcid)
        if cached:
            return cached["discussion"], cached["conclusion"], cached["text_status"]

        xml, xstatus = pdf_sources.fetch_pmc_xml(self.session(), pmcid)
        if xstatus == "ok":
            disc, concl = pdf_sources.sections_from_jats(xml)
            self.text.add({"pmcid": pmcid, "discussion": disc, "conclusion": concl, "text_status": "ok_pmc_xml"})
            return disc, concl, "ok_pmc_xml"
        if xstatus == "not_available":
            # Not in PMC's open-access data (BioC serves the same subset). The PDF step may still fill the sections.
            self.text.add({"pmcid": pmcid, "discussion": "API Fetch Failed", "conclusion": "API Fetch Failed",
                           "text_status": "not_oa"})
            return "API Fetch Failed", "API Fetch Failed", "not_oa"

        # bucket unreachable -> BioC as a slow fallback (one request at a time across all workers)
        with self._lock:
            return self._bioc_fallback(pmcid)

    def _bioc_fallback(self, pmcid: str) -> tuple[str, str, str]:
        if time.time() >= self.bioc_paused_until:
            time.sleep(BIOC_DELAY)
            data, status = self.s3.fetch_bioc_json(self.bioc, pmcid)
            if status == "ok":
                self.bioc_fails = 0
                disc, concl = self.s3.extract_section_text(data, "DISCUSS"), self.s3.extract_section_text(data, "CONCL")
                self.text.add({"pmcid": pmcid, "discussion": disc, "conclusion": concl, "text_status": "ok"})
                return disc, concl, "ok"
            if status == "not_oa":
                self.bioc_fails = 0
                self.text.add({"pmcid": pmcid, "discussion": "API Fetch Failed", "conclusion": "API Fetch Failed",
                               "text_status": "not_oa"})
                return "API Fetch Failed", "API Fetch Failed", "not_oa"
            self.bioc_fails += 1
            if status == "rate_limited" or self.bioc_fails >= BIOC_MAX_FAILS:
                self.bioc_paused_until = time.time() + BIOC_PAUSE_MIN * 60
                self.bioc_fails = 0
                logger.warning(f"NCBI's BioC API is {'rate-limiting us (429)' if status == 'rate_limited' else 'failing'}; "
                               f"not using it for {BIOC_PAUSE_MIN} min. Affected papers are retried on the next run.")
        return "API Fetch Failed", "API Fetch Failed", "error"          # not cached -> retried next run

    # ---- full PDF
    def get_pdf(self, paper: dict) -> dict:
        pid = paper["paper_id"]
        pmcid, doi = pdf_sources.normalize_pmcid(paper.get("PMCID")), clean(paper.get("DOI"))
        rec = self.registry.get(pid)
        if rec and rec["pdf_status"] in ("pdf", "text_only") and (PDF_STORE / (rec["pdf_file"] or rec["fulltext_file"])).exists():
            return rec
        if rec and rec["pdf_status"] == "not_available" and not self.args.retry_missing:
            return rec
        existing = PDF_STORE / f"{pid}.pdf"
        if existing.exists():                             # left over from an earlier (e.g. --fresh) run
            self.registry.add({"paper_id": pid, "pdf_status": "pdf", "pdf_source": "existing_file", "pdf_file": existing.name})
            return self.registry.get(pid)

        cached_pdf = None
        if self.oa is not None and paper.get("openalex_pdf_available") and self.paid_pdfs_left > 0:
            def cached_pdf():
                with self._lock:
                    if self.paid_pdfs_left <= 0:
                        return None, "budget"
                    self.paid_pdfs_left -= 1
                return self.oa.download_cached_pdf(pid)
        res = pdf_sources.get_pdf(self.session(), pmcid, doi, self.email,
                                  use_unpaywall=self.source == "pubmed" and not self.args.no_unpaywall,
                                  want_text_fallback=not self.args.no_text_fallback,
                                  oa_pdf_urls=paper.get("oa_pdf_urls") or [], cached_pdf=cached_pdf)
        rec = {"paper_id": pid, "pdf_source": res.source, "pmc_version": res.pmc_version,
               "license": res.license or clean(paper.get("license")),
               "is_retracted": "" if res.is_retracted is None else str(res.is_retracted), "notes": "; ".join(res.notes)}
        PDF_STORE.mkdir(parents=True, exist_ok=True)
        if res.pdf:
            (PDF_STORE / f"{pid}.pdf").write_bytes(res.pdf)
            rec["pdf_file"] = f"{pid}.pdf"
        if res.fulltext and not res.pdf:
            (PDF_STORE / f"{pid}.txt").write_text(res.fulltext, encoding="utf-8")
            rec["fulltext_file"] = f"{pid}.txt"
        if res.status == "error" and not res.pdf:
            rec["pdf_status"] = "error"                   # not registered -> retried next run
            return rec
        rec["pdf_status"] = "pdf" if res.pdf else ("text_only" if res.fulltext else "not_available")
        self.registry.add(rec)
        return self.registry.get(pid)

    # ---- search for one protein (runs one protein ahead, in a background thread)
    def search(self, row: dict):
        if not self.s1.protein_clause(row):
            return None
        label = clean(row.get("gene")) or slug(row.get("protein_name"), 20)
        logger.info(f"{row['disprot_id']} {label}: searching {'OpenAlex' if self.source == 'openalex' else 'PubMed'}...")
        if self.source == "openalex":
            return self.search_openalex(row)
        return self.search_pubmed(row, self.s1.protein_clause(row))

    # ---- one protein, start to finish
    def process(self, row: dict, found="search") -> dict:
        did, gene = row["disprot_id"], clean(row.get("gene"))
        label = gene or slug(row.get("protein_name"), 20)
        folder = PROTEIN_DIR / f"{did}_{slug(label)}"
        st = {"disprot_id": did, "gene": gene, "uniprot_acc": clean(row.get("uniprot_acc")),
              "protein_name": clean(row.get("protein_name")), "folder": str(folder.relative_to(OUT)),
              "n_papers": 0, "n_interaction_track": 0, "capped": False,
              "n_processed": 0, "n_pdf": 0, "n_text_only": 0, "n_no_fulltext": 0, "n_discussion_text": 0,
              "n_retracted": 0, "n_errors": 0}

        if found == "search":
            found = self.search(row)
        if found is None:
            st["status"] = "skipped_no_search_term"
            return st
        items, capped, sstatus = found
        st["capped"] = capped
        if sstatus == "failed":
            st["status"] = "failed_search"
            return st
        st["n_papers"] = len(items)
        st["n_interaction_track"] = sum("IDR_interaction" in info["Track"] for _, info in items)
        if items:
            logger.info(f"  {label}: {len(items)} papers found; getting text and PDFs")

        def fetch(p):                       # runs in a worker thread: network only
            t0 = time.time()
            pmcid = pdf_sources.normalize_pmcid(p.get("PMCID"))
            disc, concl, tstatus = self.get_text(pmcid)
            rec = None if self.args.no_pdf else self.get_pdf(p)
            return disc, concl, tstatus, rec, time.time() - t0

        rows_out = []
        started = time.time()
        workers = max(1, int(getattr(self.args, "workers", WORKERS) or 1))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(fetch, p): (p, info) for p, info in items}
            for n, fut in enumerate(as_completed(futures), 1):
                p, info = futures[fut]
                disc, concl, tstatus, rec, took = fut.result()
                self.watchdog_reset()
                rows_out.append(self.finish_paper(p, info, disc, concl, tstatus, rec, folder, st, did, gene))
                if took > SLOW_PAPER_SEC:
                    logger.warning(f"  {label}: paper {p['paper_id']} took {took:.0f}s "
                                   f"({(rec or {}).get('pdf_source') or 'no PDF'})")
                if n % PROGRESS_EVERY == 0 or n == len(items):
                    rate = (time.time() - started) / n
                    logger.info(f"  {label}: {n}/{len(items)} papers | PDFs {st['n_pdf']} | "
                                f"~{rate * (len(items) - n) / 60:.0f} min left for this protein")
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)

        folder.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows_out).reindex(columns=PAPER_OUT_COLS)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
        write_retry(folder / "papers.csv", lambda: df.sort_values("Date").to_csv(folder / "papers.csv", index=False))
        st["status"] = "incomplete" if (sstatus == "incomplete" or st["n_errors"]) else "done"
        return st

    def finish_paper(self, p, info, disc, concl, tstatus, rec, folder, st, did, gene) -> dict:
        pid = p["paper_id"]
        pmcid, doi = pdf_sources.normalize_pmcid(p.get("PMCID")), clean(p.get("DOI"))
        out = {**{c: clean(p.get(c)) for c in ["Date", "Title", "Journal", "Authors", "Article Type", "Abstract",
                                               "URL", "oa_status", "license", "is_retracted"]},
               "disprot_id": did, "gene": gene, "paper_id": pid, "PMID": clean(p.get("PMID")), "PMCID": pmcid,
               "DOI": doi, **info}
        st["n_processed"] += 1
        text_source = {"ok": "bioc", "ok_pmc_xml": "pmc_xml"}.get(tstatus, "")
        if tstatus == "error":
            st["n_errors"] += 1
        if rec is not None:
            out.update({k: rec.get(k, "") for k in ["pdf_status", "pdf_source", "pmc_version", "notes"]})
            for k in ("is_retracted", "license"):
                out[k] = rec.get(k) or out.get(k, "")
            if rec["pdf_status"] == "error":
                st["n_errors"] += 1
            stem = f"{pid}_{pmcid or 'noPMC'}"
            if rec.get("pdf_file"):
                link_or_copy(PDF_STORE / rec["pdf_file"], folder / "pdfs" / f"{stem}.pdf")
                out["pdf_file"] = f"pdfs/{stem}.pdf"
                st["n_pdf"] += 1
                if disc in MISSING_TEXT or concl in MISSING_TEXT:      # no XML text: read the PDF (main thread only)
                    try:
                        pd_disc, pd_concl = self.s5.extract_sections_from_pdf(str(PDF_STORE / rec["pdf_file"]))
                    except Exception as e:                            # damaged PDF
                        logger.warning(f"{pid}: could not read PDF ({e})")
                        pd_disc = pd_concl = "Not Found in PDF Text"
                    if disc in MISSING_TEXT:
                        disc = pd_disc
                    if concl in MISSING_TEXT:
                        concl = pd_concl
                    text_source = (text_source + "+pdf").lstrip("+")
            elif rec.get("fulltext_file"):
                link_or_copy(PDF_STORE / rec["fulltext_file"], folder / "pdfs" / f"{stem}_fulltext.txt")
                out["fulltext_file"] = f"pdfs/{stem}_fulltext.txt"
                st["n_text_only"] += 1
            else:
                st["n_no_fulltext"] += 1
        if str(out.get("is_retracted")) == "True":
            st["n_retracted"] += 1
        out.update(discussion=disc, conclusion=concl, text_source=text_source or tstatus)
        if disc not in MISSING_TEXT and not disc.startswith(("Not Found", "Requires OCR")):
            st["n_discussion_text"] += 1
        return out

    def watchdog_reset(self) -> None:
        """If the next step hangs for WATCHDOG_SEC, Python writes the exact line it is stuck on to stuck_report.txt."""
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(WATCHDOG_SEC, repeat=True, file=self._watchdog_file)

    def save_status(self) -> None:
        status = pd.DataFrame(list(self.status.values()))
        write_retry(STATUS_CSV, lambda: status.to_csv(STATUS_CSV, index=False))
        write_retry(PROGRESS, lambda: PROGRESS.write_text("\n".join(sorted(self.done))))

    # ---- the loop
    def run(self, proteins: pd.DataFrame) -> None:
        todo = [r for r in proteins.to_dict("records") if self.args.redo or r["disprot_id"] not in self.done]
        logger.info(f"{len(proteins)} proteins selected, {len(proteins) - len(todo)} already done, {len(todo)} to process.")
        started = time.time()
        searcher = ThreadPoolExecutor(max_workers=1)
        next_search = searcher.submit(self.search, todo[0]) if todo else None
        try:
            for i, row in enumerate(todo, 1):
                t0 = time.time()
                self.watchdog_reset()
                found = next_search.result()                 # BudgetExhausted surfaces here, before this protein starts
                next_search = searcher.submit(self.search, todo[i]) if i < len(todo) else None
                st = self.process(row, found)
                self.status[row["disprot_id"]] = st
                if st["status"] in ("done", "skipped_no_search_term"):
                    self.done.add(row["disprot_id"])
                self.save_status()
                eta = (time.time() - started) / i * (len(todo) - i) / 60
                budget = ""
                if self.oa is not None and self.oa.remaining is not None:
                    budget = f" | OpenAlex allowance left: {self.oa.remaining:g}" + (f"/{self.oa.limit:g}" if self.oa.limit else "")
                logger.info(f"[{i}/{len(todo)}] {row['disprot_id']} {st['gene'] or st['protein_name'][:20]}: {st['status']} | "
                            f"{st['n_papers']} papers | PDFs {st['n_pdf']}, text-only "
                            f"{st['n_text_only']}, none {st['n_no_fulltext']} | {time.time() - t0:.0f}s | "
                            f"~{eta:.0f} min left{budget}")
        except openalex_search.BudgetExhausted as e:
            logger.warning(f"Stopping: {e}")
        except KeyboardInterrupt:
            where = [l for l in traceback.format_exc().splitlines() if l.strip().startswith("File ")][-3:]
            logger.warning("Interrupted - progress is saved; run the same command again to resume. "
                           "It was busy in:\n" + "\n".join(where))
        finally:
            searcher.shutdown(wait=False, cancel_futures=True)
            faulthandler.cancel_dump_traceback_later()
            self.save_status()
            build_masters()


# ------------------------------------------------------------------ combined outputs
def build_masters() -> None:
    files = sorted(PROTEIN_DIR.glob("*/papers.csv"))
    frames = [pd.read_csv(f, dtype=str) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return
    links = pd.concat(frames, ignore_index=True)
    if "paper_id" not in links.columns or links["paper_id"].isna().all():     # files written by an older version
        links["paper_id"] = links["PMID"]
    links["paper_id"] = links["paper_id"].fillna(links.get("PMID"))
    slim = links.drop(columns=["discussion", "conclusion", "Abstract"], errors="ignore")
    write_retry(OUT / "protein_paper_links.csv", lambda: slim.to_csv(OUT / "protein_paper_links.csv", index=False))

    def join(x):
        return ", ".join(sorted({v for v in x.dropna().astype(str) if v}))
    links["_rank"] = links["pdf_status"].map({"pdf": 0, "text_only": 1}).fillna(2)     # keep the row with the best text
    first = links.sort_values("_rank").drop_duplicates("paper_id").set_index("paper_id")
    merged = links.groupby("paper_id").agg(DisProt_IDs=("disprot_id", join), Genes=("gene", join), Tracks=("Track", join))
    master = first.drop(columns=["disprot_id", "gene", "Track", "pdf_file", "fulltext_file", "_rank"], errors="ignore").join(merged)
    master["pdf_store_file"] = master.index.map(lambda p: f"_pdf_store/{p}.pdf" if (PDF_STORE / f"{p}.pdf").exists() else "")
    write_retry(OUT / "papers_master.csv", lambda: master.reset_index().to_csv(OUT / "papers_master.csv", index=False))
    logger.info(f"Combined outputs: {len(links)} protein-paper rows, {len(master)} unique papers "
                f"({(master['pdf_status'] == 'pdf').sum()} with PDFs) -> {OUT.name}/papers_master.csv")


def get_proteins(args) -> pd.DataFrame:
    if args.refresh_proteins or not PROTEINS_CSV.exists():
        s0 = load_module("step0", "00_fetch_disprot_proteins.py")
        df = s0.fetch_all_proteins()
        if df.empty:
            sys.exit("DisProt returned no proteins - check your internet connection / the DisProt API.")
        df.to_csv(PROTEINS_CSV, index=False)
    df = pd.read_csv(PROTEINS_CSV, dtype=str).fillna("")
    if FULL_LIST_CSV.exists():                       # an old run_pipeline.py test run trimmed the main file
        full = pd.read_csv(FULL_LIST_CSV, dtype=str).fillna("")
        if len(full) > len(df):
            df = full
    if args.only:
        wanted = {w.strip().upper() for w in args.only.split(",") if w.strip()}
        df = df[df["disprot_id"].str.upper().isin(wanted) | df["gene"].str.upper().isin(wanted)]
        if df.empty:
            sys.exit(f"None of {sorted(wanted)} matched a DisProt ID or gene symbol.")
    elif not args.full:
        df = df.head(args.proteins or DEFAULT_TEST_PROTEINS)
    return df


def main() -> None:
    p = argparse.ArgumentParser(description="Run the IDR pipeline one protein at a time, with full-text PDFs.")
    p.add_argument("--source", choices=["openalex", "pubmed"], default="openalex")
    p.add_argument("--full", action="store_true", help="every DisProt protein")
    p.add_argument("--proteins", type=int, help="first N proteins (default 10)")
    p.add_argument("--only", help="comma-separated DisProt IDs and/or gene symbols")
    p.add_argument("--openalex-pdfs", type=int, default=0, help="max paid OpenAlex PDF downloads this run ($0.01 each)")
    p.add_argument("--no-pdf", action="store_true")
    p.add_argument("--no-unpaywall", action="store_true")
    p.add_argument("--no-text-fallback", action="store_true")
    p.add_argument("--retry-missing", action="store_true")
    p.add_argument("--redo", action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--refresh-proteins", action="store_true")
    p.add_argument("--workers", type=int, default=WORKERS, help=f"papers fetched at the same time (default {WORKERS})")
    p.add_argument("--idr-acronyms", action="store_true",
                   help="OpenAlex: also match bare IDR/IDP acronyms (more papers, many off-topic)")
    p.add_argument("--max-papers", type=int, default=None, help="OpenAlex: keep only the top N papers per protein (default: all)")
    args = p.parse_args()

    configure_output(args.source)
    OUT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")], force=True)
    if os.environ.get("CONTACT_EMAIL") and not os.environ.get("NCBI_EMAIL"):
        os.environ["NCBI_EMAIL"] = os.environ["CONTACT_EMAIL"]
    if args.source == "pubmed" and not os.environ.get("NCBI_EMAIL"):
        sys.exit('PubMed mode needs your email. Set CONTACT_EMAIL first, e.g.  $env:CONTACT_EMAIL="you@school.edu"  '
                 '(PowerShell) or  export CONTACT_EMAIL="you@school.edu"  (macOS/Linux).')
    logging.getLogger("urllib3").setLevel(logging.ERROR)     # hide "Retrying ... connection broken" noise
    if args.idr_acronyms:
        openalex_search.USE_IDR_ACRONYMS = True
    if args.fresh:
        for f in (TEXT_CACHE, PDF_REGISTRY, STATUS_CSV, PROGRESS):
            f.unlink(missing_ok=True)
        logger.info(f"Cleared progress and caches (PDFs already in {OUT.name}/_pdf_store are kept and reused).")

    proteins = get_proteins(args)
    try:
        runner = Runner(args)
    except openalex_search.BudgetExhausted as e:
        sys.exit(str(e))
    if args.source == "openalex":
        logger.info(f"Source: OpenAlex | output: {OUT.name}/ | PDFs: "
                    f"{'off' if args.no_pdf else 'PMC Cloud + open-access links'}"
                    f"{f' + up to {args.openalex_pdfs} paid OpenAlex PDFs' if args.openalex_pdfs and not args.no_pdf else ''}")
    else:
        logger.info(f"Source: PubMed | NCBI email: {runner.email} | API key: "
                    f"{'set' if runner.s1.Entrez.api_key else 'NOT set (slower)'} | output: {OUT.name}/")
    runner.run(proteins)


if __name__ == "__main__":
    main()
