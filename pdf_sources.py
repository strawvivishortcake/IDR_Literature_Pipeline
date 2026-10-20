"""
Legal, automatable sources for full-text PDFs.

1. PMC Cloud Service (AWS bucket pmc-oa-opendata) - the official route for automated PMC downloads since
   NCBI retired the OA Web Service and the old FTP/PDF files in August 2026. Covers the PMC Open Access
   Subset and author manuscripts. Not every article there has a PDF: some licenses only allow XML/text,
   in which case the plain-text full text can be saved instead.
2. Unpaywall (by DOI) - optional fallback for papers with no PMC PDF (no PMCID, or PMC has text only).
   Points to legal open-access copies on publisher or repository sites. Some of those sites block scripts,
   so this is best-effort.

Why not the old URLs: https://pmc.ncbi.nlm.nih.gov/articles/PMCxxxx/pdf now returns a "Preparing to download"
HTML page to scripts, and PMC's terms only allow automated retrieval through its Cloud Service, OAI-PMH,
E-utilities and BioC. Europe PMC's ?pdf=render pages sit behind a bot check, so they are not used.

Every function returns a status instead of raising:
  "ok"            got the file
  "not_available" checked, the source has no PDF for this paper (don't ask again)
  "error"         network/server problem (worth retrying on the next run)
"""
from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from contextlib import contextmanager
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BUCKET_URL = "https://pmc-oa-opendata.s3.amazonaws.com"
UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}"
TIMEOUT = (15, 45)                        # seconds: (connect, wait between bytes)
DOWNLOAD_DEADLINE = 120                    # give up on any single file after this many seconds in total
MAX_PDF_BYTES = 150 * 1024 * 1024        # skip anything absurdly large (supplementary archives mislabelled as PDF)
PMC_DELAY = 0.0                           # the PMC bucket is AWS S3, built for bulk downloads: no pause needed
UNPAYWALL_DELAY = 0.0                     # pacing for other hosts is done per host (see _host_slot)
PER_HOST_CONCURRENCY = 2                  # at most this many downloads at once from any one publisher/repository
PER_HOST_INTERVAL = 0.5                   # and at least this many seconds between their starts
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


@dataclass
class PdfResult:
    status: str                       # "ok" | "not_available" | "error"
    source: str = ""                  # "pmc_cloud" | "unpaywall"
    pdf: bytes | None = None
    fulltext: str | None = None       # PMC plain-text full text (when the bucket has text but no PDF)
    pmc_version: str = ""             # e.g. "PMC1234567.2"
    is_manuscript: bool | None = None
    is_retracted: bool | None = None
    license: str = ""
    notes: list[str] = field(default_factory=list)


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "HEAD"],
                  respect_retry_after_header=False, backoff_max=10)   # never sleep for a server's "retry in 1 hour"
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"true", "yes", "y", "1"}


def normalize_pmcid(raw) -> str:
    raw = str(raw or "").strip()
    if not raw or raw.lower() == "nan":
        return ""
    return raw.upper() if raw.upper().startswith("PMC") else f"PMC{raw}"


def bucket_https_url(s3_url: str) -> tuple[str, str]:
    """
    The bucket's JSON gives URLs like  s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf?md5=abc...
    Returns (https_url_without_query, md5_or_empty). Plain https URLs are accepted too.
    """
    parts = urlsplit(s3_url)
    md5 = parse_qs(parts.query).get("md5", [""])[0]
    if parts.scheme == "s3":
        return f"https://{parts.netloc}.s3.amazonaws.com/{parts.path.lstrip('/')}", md5
    return f"{parts.scheme}://{parts.netloc}{parts.path}", md5


def _get(session: requests.Session, url: str, **kw) -> requests.Response | None:
    try:
        r = session.get(url, timeout=TIMEOUT, **kw)
        return r
    except requests.exceptions.RequestException as e:
        if "amazonaws.com" in url:
            logger.warning(f"Network error for {url}: {e}")
        else:                                   # dead / misconfigured publisher sites are common; one short line
            logger.info(f"  skipped unreachable site {urlsplit(url).netloc} ({type(e).__name__})")
        return None


_host_sems: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(PER_HOST_CONCURRENCY))
_host_next: dict[str, float] = {}
_host_lock = threading.Lock()


@contextmanager
def _host_slot(url: str):
    """Be polite to publisher/repository sites when many downloads run in parallel. AWS S3 is exempt."""
    host = urlsplit(url).netloc.lower()
    if host.endswith("amazonaws.com"):
        yield
        return
    with _host_sems[host]:
        with _host_lock:
            start = max(time.monotonic(), _host_next.get(host, 0.0))
            _host_next[host] = start + PER_HOST_INTERVAL
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        yield


def _download_pdf_bytes(session: requests.Session, url: str, md5: str = "", headers: dict | None = None):
    """Returns (bytes|None, status). Rejects HTML/bot-check pages and checksum mismatches."""
    with _host_slot(url):
        return _download_pdf_bytes_inner(session, url, md5, headers)


def _download_pdf_bytes_inner(session: requests.Session, url: str, md5: str = "", headers: dict | None = None):
    r = _get(session, url, headers=headers or {}, stream=True)
    if r is None:
        return None, "error"
    if r.status_code in (403, 404, 410):
        return None, "not_available"
    if r.status_code >= 400:
        return None, "error"
    size = int(r.headers.get("Content-Length") or 0)
    if size > MAX_PDF_BYTES:
        logger.warning(f"Skipping {url}: {size / 1e6:.0f} MB is over the size limit")
        return None, "not_available"
    chunks, total, started = [], 0, time.monotonic()
    try:
        for chunk in r.iter_content(chunk_size=256 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total == len(chunk) and not chunk.lstrip()[:5].startswith(b"%PDF"):
                r.close()
                return None, "not_available"       # first bytes aren't a PDF: web page / bot check; stop reading
            if total > MAX_PDF_BYTES:
                r.close()
                logger.warning(f"Skipping {url}: over {MAX_PDF_BYTES // 1_000_000} MB")
                return None, "not_available"
            if time.monotonic() - started > DOWNLOAD_DEADLINE:
                r.close()
                logger.warning(f"Gave up on {url}: still downloading after {DOWNLOAD_DEADLINE}s")
                return None, "error"
    except requests.exceptions.RequestException as e:
        logger.warning(f"Download interrupted for {url}: {e}")
        return None, "error"
    content = b"".join(chunks).lstrip()
    if not content.startswith(b"%PDF"):
        return None, "not_available"               # HTML landing page / bot check, not a PDF
    if md5 and hashlib.md5(content).hexdigest() != md5:
        logger.warning(f"Checksum mismatch for {url}; will retry next run")
        return None, "error"
    return content, "ok"


# ---------------------------------------------------------------- PMC Cloud Service
def list_pmc_versions(session: requests.Session, pmcid: str) -> tuple[list[str], str]:
    """Article versions in the bucket, e.g. ['PMC123.1', 'PMC123.2']. The trailing '.' stops PMC12 matching PMC123."""
    r = _get(session, f"{BUCKET_URL}/", params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"})
    time.sleep(PMC_DELAY)
    if r is None or r.status_code >= 500:
        return [], "error"
    if r.status_code >= 400:
        return [], "error"
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return [], "error"
    prefixes = [el.text.rstrip("/") for el in root.iter(f"{_S3_NS}Prefix")
                if el.text and el.text.startswith(f"{pmcid}.") and el.text.endswith("/")]
    return sorted(set(prefixes), key=lambda p: int(p.rsplit(".", 1)[1]) if p.rsplit(".", 1)[1].isdigit() else 0), "ok"


def get_pmc_metadata(session: requests.Session, version: str) -> tuple[dict | None, str]:
    r = _get(session, f"{BUCKET_URL}/metadata/{version}.json")
    time.sleep(PMC_DELAY)
    if r is None or r.status_code >= 500:
        return None, "error"
    if r.status_code in (403, 404):
        return None, "not_available"
    try:
        return r.json(), "ok"
    except ValueError:
        return None, "error"


def choose_version(metas: list[dict]) -> dict | None:
    """
    Prefer the published version over the author manuscript, then a version that has a PDF,
    then the higher version number (PMC: higher isn't always newer, but it's the best tie-breaker).
    """
    if not metas:
        return None

    def key(m):
        v = str(m.get("version", "0"))
        return (bool(m.get("pdf_url")), not _truthy(m.get("is_manuscript")), int(v) if v.isdigit() else 0)
    return max(metas, key=key)


_best_version_cache: dict[str, tuple[dict | None, str, str]] = {}


def best_pmc_version(session: requests.Session, pmcid: str) -> tuple[dict | None, str, str]:
    """(metadata of the preferred article version, status, note). Cached, so text + PDF lookups list the bucket once."""
    pmcid = normalize_pmcid(pmcid)
    if not pmcid:
        return None, "not_available", "no PMCID"
    if pmcid in _best_version_cache:
        return _best_version_cache[pmcid]
    versions, status = list_pmc_versions(session, pmcid)
    if status == "error":
        return None, "error", "bucket listing failed"           # not cached -> retried
    if not versions:
        out = (None, "not_available", "not in PMC Open Access / author manuscript dataset")
        _best_version_cache[pmcid] = out
        return out
    metas = []
    for v in versions:
        meta, st = get_pmc_metadata(session, v)
        if st == "error":
            return None, "error", f"metadata for {v} failed"
        if meta:
            meta.setdefault("version", v.rsplit(".", 1)[1])
            meta["_prefix"] = v
            metas.append(meta)
    best = choose_version(metas)
    out = (best, "ok", "") if best else (None, "not_available", "no readable metadata")
    _best_version_cache[pmcid] = out
    return out


def fetch_from_pmc(session: requests.Session, pmcid: str, want_text_fallback: bool = True) -> PdfResult:
    best, status, note = best_pmc_version(session, pmcid)
    if best is None:
        return PdfResult(status, notes=[note])

    res = PdfResult("not_available", source="pmc_cloud", pmc_version=best["_prefix"],
                    is_manuscript=_truthy(best.get("is_manuscript")), is_retracted=_truthy(best.get("is_retracted")),
                    license=str(best.get("license_code") or ""))
    if res.is_retracted:
        res.notes.append("RETRACTED")

    if best.get("pdf_url"):
        url, md5 = bucket_https_url(best["pdf_url"])
        pdf, st = _download_pdf_bytes(session, url, md5)
        time.sleep(PMC_DELAY)
        if st == "ok":
            res.status, res.pdf = "ok", pdf
            return res
        if st == "error":
            res.status = "error"
            res.notes.append("PDF download failed")
            return res
        res.notes.append("PDF listed but not downloadable")
    else:
        res.notes.append("PMC has XML/text only for this license (no PDF)")

    if want_text_fallback and best.get("text_url"):
        url, _ = bucket_https_url(best["text_url"])
        r = _get(session, url)
        time.sleep(PMC_DELAY)
        if r is not None and r.status_code == 200:
            r.encoding = "utf-8"
            res.fulltext = r.text
            res.notes.append("saved PMC plain-text full text instead")
    return res


# ---------------------------------------------------------------- Discussion / Conclusion from PMC XML
_SKIP_TAGS = {"fig", "table-wrap", "fn-group", "ref-list", "table", "disp-formula", "supplementary-material"}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _paragraphs(elem) -> list[str]:
    out = []
    for child in elem:
        name = _local(child.tag)
        if name in _SKIP_TAGS:
            continue
        if name == "p":
            text = re.sub(r"\s+", " ", "".join(child.itertext())).strip()
            if text:
                out.append(text)
        else:
            out += _paragraphs(child)
    return out


def _section_kind(sec) -> str:
    title = next((("".join(c.itertext())) for c in sec if _local(c.tag) == "title"), "")
    label = f"{sec.get('sec-type', '')} {title}".lower()
    if "discussion" in label:
        return "discussion"
    if "conclu" in label:
        return "conclusion"
    return ""


def sections_from_jats(xml_bytes: bytes) -> tuple[str, str]:
    """
    (discussion, conclusion) from a PMC JATS article XML - the same text BioC serves, read from the PMC Cloud bucket.
    "Results and Discussion" counts as Discussion; nested subsections are included.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return "Not Found", "Not Found"
    body = next((e for e in root.iter() if _local(e.tag) == "body"), None)
    found = {"discussion": [], "conclusion": []}

    def walk(elem):
        for child in elem:
            if _local(child.tag) != "sec":
                continue
            kind = _section_kind(child)
            if kind:
                found[kind] += _paragraphs(child)
            else:
                walk(child)
    if body is not None:
        walk(body)
    return ("\n\n".join(found["discussion"]) or "Not Found", "\n\n".join(found["conclusion"]) or "Not Found")


def fetch_pmc_xml(session: requests.Session, pmcid: str) -> tuple[bytes | None, str]:
    """The article's JATS XML from the PMC Cloud bucket: (xml, 'ok' | 'not_available' | 'error')."""
    best, status, _ = best_pmc_version(session, pmcid)
    if best is None:
        return None, status
    if best.get("xml_url"):
        url, _ = bucket_https_url(best["xml_url"])
    else:
        prefix = best["_prefix"]
        url = f"{BUCKET_URL}/{prefix}/{prefix}.xml"
    r = _get(session, url)
    time.sleep(PMC_DELAY)
    if r is None or r.status_code >= 500:
        return None, "error"
    if r.status_code >= 400:
        return None, "not_available"
    return r.content, "ok"


# ---------------------------------------------------------------- Unpaywall
def fetch_from_unpaywall(session: requests.Session, doi: str, email: str) -> PdfResult:
    doi = str(doi or "").strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    if not doi or doi.lower() == "nan":
        return PdfResult("not_available", notes=["no DOI"])
    r = _get(session, UNPAYWALL_URL.format(doi=quote(doi, safe="/")), params={"email": email})
    time.sleep(UNPAYWALL_DELAY)
    if r is None or r.status_code >= 500 or r.status_code == 429:
        return PdfResult("error", source="unpaywall", notes=["Unpaywall lookup failed"])
    if r.status_code == 404:
        return PdfResult("not_available", source="unpaywall", notes=["DOI unknown to Unpaywall"])
    if r.status_code >= 400:
        return PdfResult("error", source="unpaywall", notes=[f"Unpaywall HTTP {r.status_code}"])
    try:
        data = r.json()
    except ValueError:
        return PdfResult("error", source="unpaywall", notes=["Unpaywall returned bad JSON"])
    if not data.get("is_oa"):
        return PdfResult("not_available", source="unpaywall", notes=["no legal open-access copy known"])

    locations = [data.get("best_oa_location")] + list(data.get("oa_locations") or [])
    urls, seen = [], set()
    for loc in locations:
        u = (loc or {}).get("url_for_pdf")
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        return PdfResult("not_available", source="unpaywall", notes=["open access, but only as a web page (no PDF link)"])

    headers = {"User-Agent": f"IDR-literature-pipeline (mailto:{email})"}
    for u in urls[:3]:
        pdf, st = _download_pdf_bytes(session, u, headers=headers)
        time.sleep(UNPAYWALL_DELAY)
        if st == "ok":
            return PdfResult("ok", source="unpaywall", pdf=pdf, license=str((data.get("best_oa_location") or {}).get("license") or ""))
    return PdfResult("not_available", source="unpaywall", notes=["open-access PDF links blocked scripted download"])


# ---------------------------------------------------------------- direct open-access links (from OpenAlex)
def fetch_from_urls(session: requests.Session, urls: list[str], email: str, source: str = "oa_link") -> PdfResult:
    """Try known open-access PDF links (publisher / repository copies). Some hosts block scripts: 'not_available'."""
    headers = {"User-Agent": f"IDR-literature-pipeline (mailto:{email})"}
    statuses = []
    for u in list(urls)[:4]:
        pdf, st = _download_pdf_bytes(session, u, headers=headers)
        time.sleep(UNPAYWALL_DELAY)
        if st == "ok":
            return PdfResult("ok", source=source, pdf=pdf)
        statuses.append(st)
    if not statuses:
        return PdfResult("not_available", source=source)
    if all(st == "error" for st in statuses):
        # e.g. bioRxiv answers scripts with 403/503 every time; retrying each run would never finish the protein.
        # Recorded as no PDF; `--retry-missing` tries these papers again later.
        return PdfResult("not_available", source=source, notes=["open-access PDF sites unreachable or blocking scripts"])
    return PdfResult("not_available", source=source, notes=["open-access PDF links blocked scripted download"])


# ---------------------------------------------------------------- combined
def get_pdf(session: requests.Session, pmcid: str, doi: str, email: str,
            use_unpaywall: bool = True, want_text_fallback: bool = True,
            oa_pdf_urls: list[str] | None = None, cached_pdf=None) -> PdfResult:
    """
    In order, stopping at the first PDF:
      1. PMC Cloud Service (official PMC bucket)          free
      2. oa_pdf_urls - open-access links from OpenAlex     free
      3. Unpaywall by DOI (if use_unpaywall)               free
      4. cached_pdf() - OpenAlex's cached copy, if given   paid ($0.01); a callable returning (bytes|None, status)
    'error' is only returned when nothing worked AND a retry could help.
    """
    pmc = fetch_from_pmc(session, pmcid, want_text_fallback) if normalize_pmcid(pmcid) else PdfResult("not_available", notes=["no PMCID"])
    if pmc.status == "ok":
        return pmc
    retryable = pmc.status == "error"

    def finish(found: PdfResult) -> PdfResult:
        found.fulltext = pmc.fulltext
        found.pmc_version, found.is_retracted = pmc.pmc_version, pmc.is_retracted
        found.notes = pmc.notes + found.notes
        return found

    if oa_pdf_urls:
        direct = fetch_from_urls(session, oa_pdf_urls, email, source="openalex_oa_link")
        if direct.status == "ok":
            return finish(direct)
        pmc.notes += direct.notes
        retryable |= direct.status == "error"
    if use_unpaywall:
        up = fetch_from_unpaywall(session, doi, email)
        if up.status == "ok":
            return finish(up)
        pmc.notes += up.notes
        retryable |= up.status == "error"
    if cached_pdf is not None:
        pdf, st = cached_pdf()
        if st == "ok":
            return finish(PdfResult("ok", source="openalex_cached", pdf=pdf))
        if st in ("budget", "error"):
            pmc.notes.append("OpenAlex PDF budget used up" if st == "budget" else "OpenAlex PDF download failed")
            retryable = True                    # try again on a later run
    if retryable:
        pmc.status = "error"
    return pmc
