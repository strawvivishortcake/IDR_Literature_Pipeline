"""
Step 1: For every protein in disprot_human_proteins.csv, search PubMed for papers that mention
that protein AND intrinsically disordered regions (IDRs).

Phase 1  one PubMed search per protein per track (resumable; failed searches are retried, never silently dropped)
Phase 2  fetch each unique PMID ONCE (cached on disk, so an interrupted run resumes)
Phase 3  join papers to the proteins that found them

Outputs
  idr_protein_paper_links.csv   one row per (protein, paper, track)
  IDR_DisProt_Papers.csv        one row per paper (proteins/tracks merged)  -> input for step 2
  protein_summary.csv           one row per protein: how many papers, capped?, status
  idr_search_log.csv            raw result counts per search (shows which searches hit the cap)
  idr_failed_proteins.csv       only exists if some proteins still failed after a retry pass
"""
from __future__ import annotations

import os
import re
import time
import logging
import http.client
from pathlib import Path
from urllib.error import HTTPError

import pandas as pd
from Bio import Entrez

# --- CONFIGURATION ---
# Your contact email (NCBI requires one, so it can reach you if your requests cause problems). Set it in the
# environment - never in code:   CONTACT_EMAIL="you@university.edu"   (NCBI_EMAIL also works)
#   NCBI_API_KEY="..."   (optional: raises the PubMed limit from 3 to 10 requests/sec)
Entrez.email = os.environ.get("CONTACT_EMAIL") or os.environ.get("NCBI_EMAIL") or ""
Entrez.api_key = os.environ.get("NCBI_API_KEY") or None
DELAY = 0.11 if Entrez.api_key else 0.34

MAX_PMIDS_PER_SEARCH = 1000     # per protein, per track (results are sorted by relevance; capped searches are reported)
FETCH_BATCH = 200
EXCLUDE_REVIEWS = True          # matches your earlier scripts; set False to keep reviews
DATE_MIN, DATE_MAX = None, None # e.g. "1990", "2026"; None = no limit
USE_PROTEIN_NAME = True         # also match the full protein name (only if it's specific enough)
MIN_NAME_LEN = 10               # skips generic short names like "Kinase"
USE_GENE_SYNONYMS = True        # also search DisProt's alternative gene names (e.g. P53 for TP53)
MAX_SYNONYMS = 5
CHECKPOINT_EVERY = 25           # proteins

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# --- QUERY BUILDING ---
IDR_TERMS = (
    '("intrinsically disordered"[tiab] OR "intrinsically unstructured"[tiab] OR "intrinsic disorder"[tiab] '
    'OR "disordered region*"[tiab] OR "disordered protein*"[tiab] OR "unstructured region*"[tiab] '
    'OR "IDR"[tiab] OR "IDRs"[tiab] OR "IDP"[tiab] OR "IDPs"[tiab])'
)
INTERACTION_TERMS = (
    '("binding"[tiab] OR "affinity"[tiab] OR "dissociation constant"[tiab] OR "Kd"[tiab] '
    'OR "interact*"[tiab] OR "phase separation"[tiab] OR "condensate*"[tiab])'
)
EXCLUDE = ' NOT ("Review"[PT] OR "Comment"[PT] OR "Editorial"[PT])' if EXCLUDE_REVIEWS else ""

TRACKS = {
    "IDR_general": lambda protein: f"{protein} AND {IDR_TERMS}",
    "IDR_interaction": lambda protein: f"{protein} AND {IDR_TERMS} AND {INTERACTION_TERMS}",
}

# Flags papers whose title/abstract mentions binding / affinity / phase separation (informational only)
INTERACTION_RE = re.compile(r"bind|affinit|dissociation constant|\bK[dD]\b|interact|phase[- ]separat|condensat", re.I)


def clean_term(t) -> str:
    t = str(t or "").replace('"', "").strip()
    return "" if t.lower() == "nan" else t


def search_terms(row) -> list[str]:
    """Gene symbol, (optionally) synonyms, (optionally) the full protein name - de-duplicated."""
    terms = []
    gene = clean_term(row.get("gene"))
    if gene:
        terms.append(gene)
    if USE_GENE_SYNONYMS:
        syns = [clean_term(x) for x in str(row.get("gene_synonyms") or "").split(";")]
        syns = [x for x in syns if len(x) >= 2 and not x.isdigit()]
        terms += syns[:MAX_SYNONYMS]
    name = clean_term(row.get("protein_name"))
    if USE_PROTEIN_NAME and len(name) >= MIN_NAME_LEN:
        terms.append(name)
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def protein_clause(row) -> str | None:
    terms = search_terms(row)
    return "(" + " OR ".join(f'"{t}"[tiab]' for t in terms) + ")" if terms else None



# --- ENTREZ HELPERS ---
def entrez_retry(func, retries=3, **kwargs):
    for attempt in range(retries):
        try:
            handle = func(**kwargs)
            result = Entrez.read(handle)
            handle.close()
            return result
        except (http.client.IncompleteRead, HTTPError, ValueError, RuntimeError) as e:
            logger.warning(f"Entrez error ({e}); retry {attempt + 1}/{retries}")
            time.sleep(3 * (attempt + 1))
    return None


def parse_pub_date(article) -> str:
    try:
        pub_date = article['MedlineCitation']['Article']['Journal']['JournalIssue']['PubDate']
        if 'Year' in pub_date:
            return f"{pub_date.get('Year', '0000')}-{pub_date.get('Month', '01')}-{pub_date.get('Day', '01')}"
        return str(pub_date.get('MedlineDate', 'Not Available'))
    except Exception:
        return "Not Available"


def get_article_ids(article) -> tuple[str, str]:
    """Returns (pmcid, doi) from the PubMed record (needed by steps 3-5 of the pipeline)."""
    pmcid, doi = "", ""
    try:
        for aid in article['PubmedData']['ArticleIdList']:
            id_type = aid.attributes.get('IdType')
            if id_type == 'pmc':
                pmcid = str(aid)
            elif id_type == 'doi':
                doi = str(aid)
    except Exception:
        pass
    return pmcid, doi


# --- PHASE 1: SEARCH ---
def search_one_protein(row: dict, clause: str):
    """Returns (hits, log_rows) or None if ANY track failed (so a half-searched protein is never marked done)."""
    did, gene = row["disprot_id"], str(row.get("gene") or "")
    ambiguous = 0 < len(gene) <= 3
    hits, log_rows = [], []
    for track, build in TRACKS.items():
        params = dict(db="pubmed", term=build(clause) + EXCLUDE, retmax=MAX_PMIDS_PER_SEARCH)
        if DATE_MIN or DATE_MAX:
            params.update(mindate=DATE_MIN or "1900", maxdate=DATE_MAX or "3000", datetype="pdat")
        record = entrez_retry(Entrez.esearch, **params)
        time.sleep(DELAY)
        if not record:
            logger.error(f"{did} ({gene or row.get('protein_name')}) {track}: search failed")
            return None
        total = int(record["Count"])
        capped = total > MAX_PMIDS_PER_SEARCH
        if capped:
            logger.warning(f"{did} ({gene}) {track}: {total} hits, capped at {MAX_PMIDS_PER_SEARCH} (top results by relevance kept).")
        log_rows.append({"disprot_id": did, "track": track, "n_total": total, "n_retrieved": len(record["IdList"]), "capped": capped})
        for pmid in record["IdList"]:
            hits.append({"pmid": str(pmid), "disprot_id": did, "gene": gene, "uniprot_acc": row.get("uniprot_acc"),
                         "track": track, "ambiguous_symbol": ambiguous})
    return hits, log_rows


def run_searches(proteins: pd.DataFrame, base: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    hits_path, log_path, progress_path = base / "idr_search_hits.csv", base / "idr_search_log.csv", base / "idr_search_progress.txt"
    done = set(progress_path.read_text().split()) if progress_path.exists() else set()
    hits = pd.read_csv(hits_path, dtype=str).to_dict("records") if hits_path.exists() else []
    log_rows = pd.read_csv(log_path, dtype=str).to_dict("records") if log_path.exists() else []
    logger.info(f"Resuming: {len(done)} proteins already searched, {len(hits)} hits on file.")

    def checkpoint():
        pd.DataFrame(hits).to_csv(hits_path, index=False)
        pd.DataFrame(log_rows).to_csv(log_path, index=False)
        progress_path.write_text("\n".join(sorted(done)))

    failed, started, n_new = [], time.time(), 0
    todo = [r for r in proteins.to_dict("records") if r["disprot_id"] not in done]

    def handle(row, clause):
        result = search_one_protein(row, clause)
        if result is None:
            return False
        hits.extend(result[0]); log_rows.extend(result[1]); done.add(row["disprot_id"])
        return True

    for i, row in enumerate(todo, 1):
        clause = protein_clause(row)
        if not clause:
            logger.warning(f"{row['disprot_id']}: no gene symbol or usable name; skipping.")
            done.add(row["disprot_id"])
            continue
        if not handle(row, clause):
            failed.append((row, clause))
        n_new += 1
        if i % CHECKPOINT_EVERY == 0:
            checkpoint()
            rate = (time.time() - started) / n_new
            logger.info(f"[{i}/{len(todo)}] searched | {len(hits)} hits | ~{rate * (len(todo) - i) / 60:.0f} min left")

    if failed:                                    # second chance for anything that hit network trouble
        logger.warning(f"{len(failed)} proteins failed; retrying after a pause...")
        time.sleep(10)
        failed = [(r, c) for r, c in failed if not handle(r, c)]
    checkpoint()
    failed_path = base / "idr_failed_proteins.csv"
    if failed:
        pd.DataFrame([r for r, _ in failed])[["disprot_id", "gene", "protein_name"]].to_csv(failed_path, index=False)
        logger.error(f"{len(failed)} proteins still failing (see idr_failed_proteins.csv). Re-run this step to try them again.")
    elif failed_path.exists():
        failed_path.unlink()
    return pd.DataFrame(hits), pd.DataFrame(log_rows)


# --- PHASE 2: FETCH EACH UNIQUE PAPER ONCE (cached) ---
PAPER_COLS = ["PMID", "PMCID", "DOI", "Date", "Title", "Authors", "Journal", "Article Type", "Abstract", "URL"]


def parse_pubmed_article(article) -> dict:
    """One PubMed efetch record -> one row with PAPER_COLS (shared with run_by_protein.py)."""
    art = article['MedlineCitation']['Article']
    pmid = str(article['MedlineCitation']['PMID'])
    abstract_data = art.get('Abstract', {}).get('AbstractText', [])
    abstract = ' '.join(str(a) for a in abstract_data) if abstract_data else "No Abstract"
    authors = ', '.join(f"{a.get('LastName', '')} {a.get('ForeName', '')}".strip() for a in art.get('AuthorList', []))
    pmcid, doi = get_article_ids(article)
    return {'PMID': pmid, 'PMCID': pmcid, 'DOI': doi, 'Date': parse_pub_date(article),
            'Title': str(art['ArticleTitle']), 'Authors': authors, 'Journal': art['Journal']['Title'],
            'Article Type': ', '.join(str(pt) for pt in art.get('PublicationTypeList', [])),
            'Abstract': abstract, 'URL': f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"}


def fetch_papers(pmids: list[str], cache_path: Path) -> pd.DataFrame:
    cached = pd.read_csv(cache_path, dtype=str) if cache_path.exists() else pd.DataFrame(columns=PAPER_COLS)
    have = set(cached["PMID"])
    todo = [p for p in pmids if p not in have]
    logger.info(f"{len(pmids)} unique papers: {len(pmids) - len(todo)} cached, {len(todo)} to download.")

    for start in range(0, len(todo), FETCH_BATCH):
        batch = todo[start:start + FETCH_BATCH]
        logger.info(f"Fetching papers {start + 1}-{start + len(batch)} of {len(todo)}")
        records = entrez_retry(Entrez.efetch, db="pubmed", id=",".join(batch), retmode="xml")
        time.sleep(DELAY)
        if not records:
            logger.error(f"Batch starting at {start} failed; those papers will be retried on the next run.")
            continue
        rows = [parse_pubmed_article(article) for article in records.get('PubmedArticle', [])]
        if rows:
            pd.DataFrame(rows, columns=PAPER_COLS).to_csv(cache_path, mode="a", header=not cache_path.exists(), index=False)

    papers = pd.read_csv(cache_path, dtype=str) if cache_path.exists() else pd.DataFrame(columns=PAPER_COLS)
    papers = papers[papers["PMID"].isin(pmids)].drop_duplicates("PMID")
    if len(papers) < len(pmids):
        logger.warning(f"{len(pmids) - len(papers)} papers could not be downloaded; re-run this step to retry them.")
    return papers


# --- PHASE 3: JOIN PAPERS TO PROTEINS ---
def build_links(hits: pd.DataFrame, papers: pd.DataFrame, proteins: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per (protein, paper, track) for every paper that was downloaded. `proteins` is unused (kept for old callers)."""
    text_by_pmid = {r.PMID: f"{r.Title} {'' if r.Abstract == 'No Abstract' else r.Abstract}" for r in papers.itertuples()}
    rows = []
    for h in hits.astype({"pmid": str}).to_dict("records"):
        text = text_by_pmid.get(h["pmid"])
        if text is None:
            continue
        rows.append({**h, "interaction_match": bool(INTERACTION_RE.search(text))})
    return pd.DataFrame(rows)


def join_unique(x):
    return ", ".join(sorted({str(v) for v in x if str(v) not in ("", "nan")}))


def build_paper_table(links: pd.DataFrame, papers: pd.DataFrame) -> pd.DataFrame:
    agg = links.groupby("pmid").agg(
        DisProt_IDs=("disprot_id", join_unique), Genes=("gene", join_unique), Track=("track", join_unique),
        N_Proteins=("disprot_id", "nunique"),
        Only_Ambiguous_Symbols=("ambiguous_symbol", lambda x: (x.astype(str) == "True").all()),
        Interaction_In_Abstract=("interaction_match", "any")).reset_index().rename(columns={"pmid": "PMID"})
    out = papers.merge(agg, on="PMID", how="inner")
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    return out.sort_values(by="Date", ascending=True)


def build_protein_summary(proteins: pd.DataFrame, links: pd.DataFrame, search_log: pd.DataFrame, done: set) -> pd.DataFrame:
    rows = []
    log = search_log.copy()
    if not log.empty:
        log["capped"] = log["capped"].astype(str) == "True"
    for p in proteins.to_dict("records"):
        did = p["disprot_id"]
        mine = links[links["disprot_id"] == did] if not links.empty else links
        if not protein_clause(p):
            status = "skipped_no_search_term"
        elif did in done:
            status = "searched"
        else:
            status = "failed_or_pending"
        rows.append({"disprot_id": did, "gene": p.get("gene"), "uniprot_acc": p.get("uniprot_acc"),
                     "protein_name": p.get("protein_name"), "status": status,
                     "n_papers": mine["pmid"].nunique() if len(mine) else 0,
                     "n_papers_interaction": mine.loc[mine["track"] == "IDR_interaction", "pmid"].nunique() if len(mine) else 0,
                     "capped": bool(log.loc[log["disprot_id"] == did, "capped"].any()) if not log.empty else False})
    return pd.DataFrame(rows)


def main() -> None:
    if "@" not in Entrez.email or "." not in Entrez.email.split("@")[-1]:
        logger.warning(f"NCBI email {Entrez.email!r} doesn't look like a full address (needs a domain like .edu/.org). "
                       "NCBI uses it to contact you if your requests cause problems.")
    base = Path(__file__).parent
    proteins = pd.read_csv(base / "disprot_human_proteins.csv", dtype=str).fillna("")
    logger.info(f"Loaded {len(proteins)} DisProt proteins.")

    hits, search_log = run_searches(proteins, base)
    done = set((base / "idr_search_progress.txt").read_text().split())
    if hits.empty:
        build_protein_summary(proteins, hits, search_log, done).to_csv(base / "protein_summary.csv", index=False)
        raise SystemExit("No hits found.")

    unique_pmids = sorted(hits["pmid"].astype(str).unique())
    logger.info(f"{len(hits)} protein-paper hits -> {len(unique_pmids)} unique papers.")
    papers = fetch_papers(unique_pmids, base / "idr_papers_cache.csv")

    links = build_links(hits, papers, proteins)
    links.to_csv(base / "idr_protein_paper_links.csv", index=False)
    paper_table = build_paper_table(links, papers)
    paper_table.to_csv(base / "IDR_DisProt_Papers.csv", index=False)
    summary = build_protein_summary(proteins, links, search_log, done)
    summary.to_csv(base / "protein_summary.csv", index=False)

    logger.info(f"Done! {len(paper_table)} papers saved to IDR_DisProt_Papers.csv")
    logger.info(f"Proteins with >=1 paper: {int((summary['n_papers'] > 0).sum())} of {len(summary)}")


if __name__ == "__main__":
    main()
