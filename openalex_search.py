"""
OpenAlex as the paper-search source (replaces PubMed/Entrez in run_by_protein.py --source openalex).

One search per protein (plus one per extra page of 100 results) over titles + abstracts:
    ("TP53" OR "P53" OR "Cellular tumor antigen p53") AND ("intrinsically disordered" OR "IDR" OR ...)
The old "IDR_interaction" track is not a second search any more: it was always a subset of the general search, so
papers are tagged IDR_interaction when their title/abstract mentions binding / affinity / Kd / phase separation /
condensates. That halves the number of paid search calls.

Costs (OpenAlex, 2026): an API key is required; each key gets $1 of free usage per day (resets midnight UTC).
A search page (100 results) costs $0.001, so the free tier covers ~1,000 pages (~100,000 results) a day. OpenAlex's own cached-PDF download
costs $0.01 each and is OFF unless you set a budget (--openalex-pdfs N).
Docs: https://help.openalex.org/api/  |  free key: https://openalex.org/settings/api-key
"""
from __future__ import annotations

import logging
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

API = "https://api.openalex.org/works"
PER_PAGE = 100                    # OpenAlex maximum
MAX_RESULTS_PER_PROTEIN = None    # None = every result (cursor paging, no 10,000 limit); set a number to cap
EXCLUDE_REVIEWS = True            # keep only primary research (type article or preprint)
MIN_DELAY = 0.15                  # seconds between calls (free keys allow 30/s; no need to push it)
TIMEOUT = (15, 60)                # seconds: (connect, wait between bytes)

IDR_PHRASES = ['"intrinsically disordered"', '"intrinsically unstructured"', '"intrinsic disorder"',
               '"disordered region"', '"disordered regions"', '"disordered protein"', '"disordered proteins"',
               '"disordered domain"', '"disordered segment"', '"disordered tail"', '"disordered linker"',
               '"unstructured region"', '"unstructured regions"', '"intrinsically unfolded"']
# Bare acronyms are OFF by default: OpenAlex search ignores case, so IDP also matches "internally displaced persons"
# (with gene CAMP -> refugee-camp papers) and IDR matches the Indonesian rupiah. Real IDR abstracts spell it out.
IDR_ACRONYMS = ["IDR", "IDRs", "IDP", "IDPs"]
USE_IDR_ACRONYMS = False
INTERACTION_RE = re.compile(r"bind|affinit|dissociation constant|\bK[dD]\b|interact|phase[- ]separat|condensat", re.I)
SELECT = ",".join(["id", "ids", "doi", "title", "publication_date", "type", "is_retracted", "authorships",
                   "primary_location", "locations", "best_oa_location", "open_access", "has_content", "content_urls",
                   "abstract_inverted_index"])
PMC_PATTERNS = [re.compile(p, re.I) for p in (r"pubmedcentral\.nih\.gov:(\d+)", r"/pmc/articles/(?:PMC)?(\d+)",
                                              r"pmc\.ncbi\.nlm\.nih\.gov/articles/(?:PMC)?(\d+)", r"^(?:PMC)?(\d+)$")]


class BudgetExhausted(RuntimeError):
    """OpenAlex daily allowance used up (or the key is missing/invalid). The run stops cleanly and resumes later."""


# ------------------------------------------------------------------ query
def _quote(term: str) -> str:
    term = re.sub(r'["()]', " ", term).strip()
    term = re.sub(r"\s+", " ", term)
    return f'"{term}"' if term else ""


def build_query(terms: list[str]) -> str | None:
    names = [q for q in (_quote(t) for t in terms) if q]
    if not names:
        return None
    idr = IDR_PHRASES + (IDR_ACRONYMS if USE_IDR_ACRONYMS else [])
    return f"({' OR '.join(names)}) AND ({' OR '.join(idr)})"


# ------------------------------------------------------------------ parsing a work
def reconstruct_abstract(inv: dict | None) -> str:
    if not inv:
        return "No Abstract"
    words = {}
    for word, positions in inv.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words)) if words else "No Abstract"


def _tail(url) -> str:
    return str(url or "").rstrip("/").rsplit("/", 1)[-1]


def find_pmcid(work: dict) -> str:
    candidates = [(work.get("ids") or {}).get("pmcid") or ""]
    for loc in work.get("locations") or []:
        candidates += [loc.get("id") or "", loc.get("landing_page_url") or "", loc.get("pdf_url") or ""]
    for c in candidates:
        for pat in PMC_PATTERNS:
            m = pat.search(str(c))
            if m:
                return f"PMC{m.group(1)}"
    return ""


def oa_pdf_urls(work: dict) -> list[str]:
    """Free, legal PDF links OpenAlex knows (best location first), excluding PMC's (scripts are blocked there)."""
    urls, seen = [], set()
    locs = [work.get("best_oa_location")] + list(work.get("locations") or [])
    for loc in locs:
        if not loc or not loc.get("is_oa"):
            continue
        u = loc.get("pdf_url")
        if u and u not in seen and "ncbi.nlm.nih.gov" not in u and "europepmc.org" not in u:
            seen.add(u)
            urls.append(u)
    return urls


def parse_work(work: dict) -> dict:
    ids = work.get("ids") or {}
    title = work.get("title") or ""
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    source = ((work.get("primary_location") or {}).get("source") or {})
    best = work.get("best_oa_location") or {}
    has_content = work.get("has_content") or {}
    pmid = _tail(ids.get("pmid"))
    return {
        "paper_id": _tail(work.get("id")),                       # W123... (OpenAlex ID)
        "PMID": pmid if pmid.isdigit() else "",
        "PMCID": find_pmcid(work),
        "DOI": re.sub(r"^https?://(dx\.)?doi\.org/", "", work.get("doi") or ids.get("doi") or "", flags=re.I),
        "Date": work.get("publication_date") or "",
        "Title": title,
        "Authors": ", ".join(((a.get("author") or {}).get("display_name") or "") for a in (work.get("authorships") or [])),
        "Journal": source.get("display_name") or "",
        "Article Type": work.get("type") or "",
        "Abstract": abstract,
        "URL": work.get("id") or "",
        "oa_status": (work.get("open_access") or {}).get("oa_status") or "",
        "license": best.get("license") or "",
        "is_retracted": str(bool(work.get("is_retracted"))),
        "oa_pdf_urls": oa_pdf_urls(work),
        "openalex_pdf_available": bool(has_content.get("pdf")),
        "Interaction_In_Abstract": bool(INTERACTION_RE.search(f"{title} {abstract}")),
    }


# ------------------------------------------------------------------ client
class OpenAlexClient:
    def __init__(self, api_key: str, email: str = ""):
        if not api_key:
            raise BudgetExhausted("No OpenAlex API key. Get a free one at https://openalex.org/settings/api-key and set "
                                  "OPENALEX_API_KEY (PowerShell: $env:OPENALEX_API_KEY=\"...\").")
        self.api_key, self.email = api_key, email
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"],
                      respect_retry_after_header=False, backoff_max=20)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers["User-Agent"] = f"IDR-literature-pipeline (mailto:{email})" if email else "IDR-literature-pipeline"
        self.remaining: float | None = None       # today's allowance left / total, from X-RateLimit-* headers
        self.limit: float | None = None
        self.n_calls = 0
        self._last = 0.0
        self._lock = threading.Lock()           # searches run in a background thread

    def _note_usage(self, r: requests.Response) -> None:
        for attr, header in (("remaining", "X-RateLimit-Remaining"), ("limit", "X-RateLimit-Limit")):
            try:
                if r.headers.get(header) is not None:
                    setattr(self, attr, float(r.headers[header]))
            except ValueError:
                pass

    def budget_used_up(self) -> bool:
        if self.remaining is None:
            return False
        return self.remaining <= 0 or (bool(self.limit) and self.remaining < 0.002 * self.limit)

    def get(self, url: str, params: dict | None = None, stream: bool = False) -> requests.Response:
        params = {**(params or {}), "api_key": self.api_key}
        for attempt in range(6):
            with self._lock:
                wait = MIN_DELAY - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=TIMEOUT, stream=stream)
            except requests.exceptions.RequestException as e:
                logger.warning(f"OpenAlex network error ({e}); retrying")
                time.sleep(5 * (attempt + 1))
                continue
            self.n_calls += 1
            self._note_usage(r)
            if r.status_code in (401, 403) and "content.openalex.org" not in url:
                raise BudgetExhausted(f"OpenAlex rejected the API key (HTTP {r.status_code}). Check OPENALEX_API_KEY.")
            if r.status_code == 429:
                if self.budget_used_up():
                    raise BudgetExhausted(self._budget_message())
                time.sleep(2 * (attempt + 1))     # per-second limit: free to retry after a moment
                continue
            return r
        raise BudgetExhausted(self._budget_message() + " (OpenAlex kept answering 429 Too Many Requests.)")

    def _budget_message(self) -> str:
        return ("OpenAlex's daily allowance for this API key is used up. It resets at midnight UTC (8 pm New York time; 7 pm after Nov 1); "
                "run the same command again then and it resumes where it stopped. To go faster, add prepaid credit "
                "at https://openalex.org/settings/usage.")

    def search_protein(self, terms: list[str], max_results: int | None = None) -> tuple[list[dict], int, bool]:
        """
        Every matching work (cursor paging), or the top `max_results` by relevance.
        Returns (parsed works, total hits OpenAlex reported, capped?), or None on a hard error. Raises BudgetExhausted.
        """
        cap = max_results if max_results is not None else MAX_RESULTS_PER_PROTEIN
        query = build_query(terms)
        if not query:
            return [], 0, False
        params = {"search.title_and_abstract": query, "per_page": PER_PAGE, "select": SELECT}
        if EXCLUDE_REVIEWS:
            params["filter"] = "type:article|preprint"
        works, total, cursor, n_page = [], 0, "*", 0
        while cursor and (cap is None or len(works) < cap):
            n_page += 1
            if n_page > 1 and n_page % 5 == 0:
                logger.info(f"    OpenAlex: {len(works)}/{total} results downloaded")
            r = self.get(API, {**params, "cursor": cursor})
            if r.status_code == 400 and len(query) > 1500:
                logger.warning("Query too long for OpenAlex; retrying with the gene symbol and full name only")
                return self.search_protein(terms[:1] + terms[-1:], max_results) if len(terms) > 2 else ([], 0, False)
            if r.status_code >= 400:
                logger.error(f"OpenAlex search failed (HTTP {r.status_code}): {r.text[:200]}")
                return None
            data = r.json()
            meta = data.get("meta") or {}
            total = int(meta.get("count") or 0)
            batch = data.get("results") or []
            works += [parse_work(w) for w in batch]
            cursor = meta.get("next_cursor") if batch else None
        if cap is not None:
            works = works[:cap]
        seen, unique = set(), []
        for w in works:                       # cursor pages can overlap at the edges
            if w["paper_id"] not in seen:
                seen.add(w["paper_id"])
                unique.append(w)
        return unique, total, cap is not None and total > cap

    def download_cached_pdf(self, work_id: str) -> tuple[bytes | None, str]:
        """OpenAlex's cached copy ($0.01 each). Returns (bytes, 'ok' | 'not_available' | 'error')."""
        try:
            r = self.get(f"https://content.openalex.org/works/{work_id}.pdf")
        except BudgetExhausted:
            return None, "budget"
        if r.status_code in (402, 403, 404):
            return None, "not_available"
        if r.status_code >= 400:
            return None, "error"
        if not r.content.startswith(b"%PDF"):
            return None, "not_available"
        return r.content, "ok"
