"""
Step 3: Fast text extraction via the NCBI BioC API.
Fills in the 'discussion' and 'conclusion' columns for every open-access paper it can read.

Built for thousands of papers: a few worker threads share one rate limiter (about 3 requests/sec overall),
progress is checkpointed, and a re-run skips papers already processed. Papers that failed because of a
network/server error are retried on the next run; papers that simply aren't open access are not.
"""
from __future__ import annotations

import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- CONFIGURATION ---
BASE_URL = "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/{pmcid}/unicode"
REQUESTS_PER_SECOND = 1.0   # overall cap across all threads. NCBI answers 429/503 when pushed harder.
MAX_WORKERS = 1
RATE_LIMIT_WAIT = 60        # seconds to back off after a 429 when NCBI doesn't say how long
TIMEOUT = 45                # Seconds before a request times out
CHECKPOINT_EVERY = 100      # Save progress every N papers so a crash doesn't lose hours of work

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


class RateLimiter:
    """Hands out request slots at most `per_second` times per second, across all threads."""
    def __init__(self, per_second: float):
        self.interval, self.lock, self.next_slot = 1.0 / per_second, threading.Lock(), 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + self.interval
        if delay:
            time.sleep(delay)


_local = threading.local()


def create_requests_session() -> requests.Session:
    """Creates a robust web session with automatic retries for temporary network hiccups."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=5, status_forcelist=[500, 502, 503, 504],   # 5s, 10s, 20s between tries
                  respect_retry_after_header=False, backoff_max=30)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def thread_session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = create_requests_session()
    return _local.session


def retry_after_seconds(response) -> float:
    try:
        return min(float(response.headers.get("Retry-After", RATE_LIMIT_WAIT)), 600)
    except (TypeError, ValueError):
        return RATE_LIMIT_WAIT


def fetch_bioc_json(session: requests.Session, pmcid: str):
    """
    Returns (data, status): 'ok', 'not_oa' (not in the Open Access subset), 'rate_limited' (NCBI said slow down;
    the wait it asked for is in fetch_bioc_json.last_wait) or 'error' (network/server).
    """
    url = BASE_URL.format(pmcid=pmcid.strip())
    try:
        response = session.get(url, timeout=TIMEOUT)
        if response.status_code == 429:
            fetch_bioc_json.last_wait = retry_after_seconds(response)
            logger.warning(f"{pmcid}: NCBI rate limit (429); backing off {fetch_bioc_json.last_wait:.0f}s")
            return None, "rate_limited"
        response.raise_for_status()
        # NCBI answers 200 with plain text when a paper isn't in the Open Access subset
        if "application/json" not in response.headers.get("Content-Type", ""):
            logger.warning(f"{pmcid}: Not in machine-readable Open Access subset.")
            return None, "not_oa"
        return response.json(), "ok"
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching {pmcid}: {e}")
    except ValueError:
        logger.error(f"Invalid JSON returned for {pmcid}")
    return None, "error"


fetch_bioc_json.last_wait = RATE_LIMIT_WAIT


def extract_section_text(data, section_type: str) -> str:
    """
    Navigates the NCBI BioC JSON tree to extract all paragraphs for a given section.
    section_type examples: 'DISCUSS', 'CONCL'
    """
    paragraphs = []
    documents = data if isinstance(data, list) else [data]
    for doc in documents:
        passages = list(doc.get("passages", []))
        for inner_doc in doc.get("documents", []):
            passages.extend(inner_doc.get("passages", []))
        for passage in passages:
            infons = passage.get("infons", {})
            if infons.get("section_type") == section_type and infons.get("type") == "paragraph":
                text = passage.get("text", "").strip()
                if text:
                    paragraphs.append(text)
    return "\n\n".join(paragraphs) if paragraphs else "Not Found"


def normalize_pmcid(raw) -> str:
    raw = str(raw).strip()
    return raw if raw.upper().startswith("PMC") else f"PMC{raw}"


def enrich_research_papers(input_path: Path, output_path: Path) -> None:
    """Main pipeline: Loads CSV, finds missing data, fetches it, and saves."""
    logger.info(f"Loading dataset from: {input_path.name}")
    try:
        df = pd.read_csv(input_path, dtype=str)
    except FileNotFoundError:
        logger.error(f"Could not find the input file: {input_path.name}. Ensure it is in the same folder as this script.")
        return

    for col in ("discussion", "conclusion"):
        if col not in df.columns:
            df[col] = "Not Found"

    valid = (df['pmcid'].notna() & (df['pmcid'].str.strip() != '') & ~df['pmcid'].isin(['(NULL)', 'Not Found in PMC']))
    df.loc[valid, "pmcid"] = df.loc[valid, "pmcid"].apply(normalize_pmcid)

    # --- resume: copy results for papers already processed in an earlier run ---
    progress_path = output_path.with_name("api_progress.txt")
    done = set(progress_path.read_text().split()) if progress_path.exists() else set()
    if done and output_path.exists():
        prev = pd.read_csv(output_path, dtype=str).drop_duplicates("pmcid").set_index("pmcid")
        reuse = df["pmcid"].isin(done) & df["pmcid"].isin(prev.index)
        for col in ("discussion", "conclusion"):
            df.loc[reuse, col] = df.loc[reuse, "pmcid"].map(prev[col])
        logger.info(f"Resuming: reusing results for {int(reuse.sum())} papers already processed.")

    needs_update = valid & ~df["pmcid"].isin(done) & (
        (df['discussion'] == 'Not Found') | (df['conclusion'] == 'Not Found') | df['discussion'].isna() | df['conclusion'].isna())
    targets = df.loc[needs_update, "pmcid"].drop_duplicates().tolist()
    logger.info(f"Found {len(targets)} papers requiring text extraction (~{len(targets) / REQUESTS_PER_SECOND / 60:.0f} min).")
    if not targets:
        logger.info("No updates needed. Exiting.")
        df.to_csv(output_path, index=False)
        return

    index_of = df.groupby("pmcid").groups
    limiter = RateLimiter(REQUESTS_PER_SECOND)

    def work(pmcid: str):
        limiter.wait()
        data, status = fetch_bioc_json(thread_session(), pmcid)
        if status == "rate_limited":
            time.sleep(fetch_bioc_json.last_wait)      # wait as asked, then try this paper once more
            limiter.wait()
            data, status = fetch_bioc_json(thread_session(), pmcid)
            if status == "rate_limited":
                status = "error"
        if status == "ok":
            return pmcid, status, extract_section_text(data, "DISCUSS"), extract_section_text(data, "CONCL")
        return pmcid, status, "API Fetch Failed", "API Fetch Failed"

    def save():
        df.to_csv(output_path, index=False)
        progress_path.write_text("\n".join(sorted(done)))

    started, counts = time.time(), {"ok": 0, "not_oa": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(work, p) for p in targets]
        for n, future in enumerate(as_completed(futures), 1):
            pmcid, status, disc, concl = future.result()
            df.loc[index_of[pmcid], "discussion"] = disc
            df.loc[index_of[pmcid], "conclusion"] = concl
            counts[status] += 1
            if status != "error":            # network errors are NOT marked done, so a re-run retries them
                done.add(pmcid)
            if n % CHECKPOINT_EVERY == 0:
                save()
                eta = (time.time() - started) / n * (len(targets) - n) / 60
                logger.info(f"[{n}/{len(targets)}] text extracted: {counts['ok']} | not open access: {counts['not_oa']} | "
                            f"errors: {counts['error']} | ~{eta:.0f} min left")

    logger.info(f"Saving updated dataset to: {output_path.name}")
    save()
    logger.info(f"Pipeline complete! ok={counts['ok']} not_open_access={counts['not_oa']} errors={counts['error']}"
                + (" (re-run this step to retry the errors)" if counts["error"] else ""))


if __name__ == "__main__":
    BASE_DIR = Path(__file__).parent
    INPUT_FILE = BASE_DIR / "public_research_papers_export_before_conclusion_and_discussion.csv"
    OUTPUT_FILE = BASE_DIR / "research_papers_updated_text.csv"
    enrich_research_papers(INPUT_FILE, OUTPUT_FILE)

