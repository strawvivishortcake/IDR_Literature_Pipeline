"""
Offline tests for the OpenAlex search source (openalex_search.py + run_by_protein.py --source openalex).

    pytest -v tests/test_openalex.py
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import openalex_search as oas  # noqa: E402
import pdf_sources  # noqa: E402

PDF = b"%PDF-1.4\n" + b"fake pdf body " * 50


def work(wid, title, abstract, pmid=None, pmc_loc=None, pdf_url=None, has_pdf=False, typ="article", retracted=False):
    words = abstract.split()
    inv = {}
    for i, w in enumerate(words):
        inv.setdefault(w, []).append(i)
    locs = [{"id": f"doi:10.1/{wid}", "is_oa": bool(pdf_url), "pdf_url": pdf_url, "license": "cc-by",
             "landing_page_url": f"https://doi.org/10.1/{wid}"}]
    if pmc_loc:
        locs.append({"id": f"pmh:oai:pubmedcentral.nih.gov:{pmc_loc}", "is_oa": True, "pdf_url": None,
                     "landing_page_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_loc}"})
    ids = {"openalex": f"https://openalex.org/{wid}", "doi": f"https://doi.org/10.1/{wid}"}
    if pmid:
        ids["pmid"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"
    return {"id": f"https://openalex.org/{wid}", "ids": ids, "doi": f"https://doi.org/10.1/{wid}", "title": title,
            "publication_date": "2021-05-04", "type": typ, "is_retracted": retracted,
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}, {"author": {"display_name": "Alan Turing"}}],
            "primary_location": {"source": {"display_name": "J Test"}}, "locations": locs,
            "best_oa_location": locs[0] if pdf_url else None, "open_access": {"oa_status": "gold" if pdf_url else "closed"},
            "has_content": {"pdf": has_pdf, "grobid_xml": False}, "abstract_inverted_index": inv}


class Resp:
    def __init__(self, status=200, data=None, headers=None, content=b""):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.content = content or json.dumps(data or {}).encode()
        self.text = self.content.decode("latin-1")

    def json(self):
        return self._data

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(oas.time, "sleep", lambda s: None)
    monkeypatch.setattr(pdf_sources.time, "sleep", lambda s: None)


# ------------------------------------------------------------------ query + parsing
def test_query_quotes_names_and_ands_idr_terms():
    q = oas.build_query(["TP53", "P53", 'Cellular "tumor" antigen (p53)'])
    assert q.startswith('("TP53" OR "P53" OR "Cellular tumor antigen p53") AND (')
    assert '"intrinsically disordered"' in q and "IDP" not in q          # acronyms off: IDP = displaced persons
    oas.USE_IDR_ACRONYMS = True
    try:
        assert " OR IDP OR " in oas.build_query(["CAMP"])
    finally:
        oas.USE_IDR_ACRONYMS = False
    assert oas.build_query([]) is None


def test_parse_work_gets_ids_abstract_and_pmcid_from_locations():
    w = oas.parse_work(work("W1", "FUS IDR binds RNA", "The FUS IDR binds RNA and forms condensates",
                            pmid="123", pmc_loc="7434221", pdf_url="https://pub.example/a.pdf", has_pdf=True))
    assert w["paper_id"] == "W1" and w["PMID"] == "123" and w["PMCID"] == "PMC7434221" and w["DOI"] == "10.1/W1"
    assert w["Abstract"] == "The FUS IDR binds RNA and forms condensates"
    assert w["Authors"] == "Ada Lovelace, Alan Turing" and w["Journal"] == "J Test"
    assert w["oa_pdf_urls"] == ["https://pub.example/a.pdf"] and w["openalex_pdf_available"]
    assert w["Interaction_In_Abstract"]


def test_pmc_and_europepmc_pdf_links_are_skipped():
    w = work("W2", "t", "a", pdf_url="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1/pdf/x.pdf")
    assert oas.oa_pdf_urls(w) == []


# ------------------------------------------------------------------ client
class FakeHTTP:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.headers = {}

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)

    def mount(self, *a):
        pass


def client(responses):
    c = oas.OpenAlexClient("KEY", "a@b.edu")
    c.session = FakeHTTP(responses)
    return c


def test_missing_key_is_a_clear_error():
    with pytest.raises(oas.BudgetExhausted, match="openalex.org/settings/api-key"):
        oas.OpenAlexClient("", "a@b.edu")


def test_search_follows_cursor_to_the_end_with_no_cap():
    page1 = {"meta": {"count": 250, "next_cursor": "C2"}, "results": [work(f"W{i}", "t", "a") for i in range(100)]}
    page2 = {"meta": {"count": 250, "next_cursor": "C3"}, "results": [work(f"W{i}", "t", "a") for i in range(100, 200)]}
    page3 = {"meta": {"count": 250, "next_cursor": "C4"}, "results": [work(f"W{i}", "t", "a") for i in range(199, 250)]}
    page4 = {"meta": {"count": 250, "next_cursor": None}, "results": []}
    c = client([Resp(data=page1, headers={"X-RateLimit-Remaining": "9990", "X-RateLimit-Limit": "10000"}),
                Resp(data=page2), Resp(data=page3), Resp(data=page4)])
    works, total, capped = c.search_protein(["FUS"])
    assert len(works) == 250 and total == 250 and not capped          # W199 overlap de-duplicated
    url, params = c.session.calls[0]
    assert params["api_key"] == "KEY" and params["filter"] == "type:article|preprint"
    assert params["search.title_and_abstract"].startswith('("FUS") AND') and params["per_page"] == 100
    assert [p["cursor"] for _, p in c.session.calls] == ["*", "C2", "C3", "C4"]
    assert c.remaining == 9990


def test_optional_cap_still_works():
    pages = [Resp(data={"meta": {"count": 5000, "next_cursor": f"C{p}"},
                        "results": [work(f"W{p}_{i}", "t", "a") for i in range(100)]}) for p in range(3)]
    c = client(pages)
    works, total, capped = c.search_protein(["TP53"], max_results=200)
    assert len(works) == 200 and capped and len(c.session.calls) == 2


def test_per_second_429_is_retried():
    ok = Resp(data={"meta": {"count": 0}, "results": []})
    c = client([Resp(429, headers={"X-RateLimit-Remaining": "5000", "X-RateLimit-Limit": "10000"}), ok])
    assert c.search_protein(["FUS"]) == ([], 0, False)


def test_daily_budget_429_stops_with_resume_message():
    c = client([Resp(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "10000"})])
    with pytest.raises(oas.BudgetExhausted, match="midnight UTC"):
        c.search_protein(["FUS"])


def test_bad_key_stops():
    c = client([Resp(401)])
    with pytest.raises(oas.BudgetExhausted, match="OPENALEX_API_KEY"):
        c.search_protein(["FUS"])


# ------------------------------------------------------------------ PDF chain with OpenAlex links
class PdfSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return v
        return Resp(404, content=b"nope")


def test_oa_link_used_when_no_pmc_and_paid_pdf_not_touched():
    s = PdfSession({"pub.example": Resp(200, content=PDF)})
    paid = []
    res = pdf_sources.get_pdf(s, "", "10.1/x", "a@b.edu", use_unpaywall=False,
                              oa_pdf_urls=["https://pub.example/a.pdf"], cached_pdf=lambda: paid.append(1) or (PDF, "ok"))
    assert res.status == "ok" and res.source == "openalex_oa_link" and paid == []


def test_paid_cached_pdf_is_last_resort():
    s = PdfSession({"pub.example": Resp(200, content=b"<html>bot check</html>")})
    res = pdf_sources.get_pdf(s, "", "10.1/x", "a@b.edu", use_unpaywall=False,
                              oa_pdf_urls=["https://pub.example/a.pdf"], cached_pdf=lambda: (PDF, "ok"))
    assert res.status == "ok" and res.source == "openalex_cached"


def test_paid_budget_exhausted_is_retried_later():
    res = pdf_sources.get_pdf(PdfSession({}), "", "", "a@b.edu", use_unpaywall=False, cached_pdf=lambda: (None, "budget"))
    assert res.status == "error"


# ------------------------------------------------------------------ end to end (--source openalex)
WORKS = [work("W10", "TP53 intrinsically disordered region binds DNA", "TP53 IDR binds DNA", pmid="1", pmc_loc="100"),
         work("W20", "TP53 disordered region condensates", "TP53 IDR phase separation", pdf_url="https://pub.example/b.pdf"),
         work("W30", "TP53 IDR structure", "TP53 IDR NMR", has_pdf=True)]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for f in REPO.glob("*.py"):
        shutil.copy(f, tmp_path / f.name)
    pd.DataFrame([{"disprot_id": "DP00086", "uniprot_acc": "P04637", "gene": "TP53", "gene_synonyms": "P53",
                   "protein_name": "Cellular tumor antigen p53"},
                  {"disprot_id": "DP00002", "uniprot_acc": "Q", "gene": "FUS", "gene_synonyms": "",
                   "protein_name": "RNA-binding protein FUS"}]).to_csv(tmp_path / "disprot_human_proteins.csv", index=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
    for m in ("run_by_protein", "pdf_sources", "openalex_search"):
        sys.modules.pop(m, None)
    rbp = importlib.import_module("run_by_protein")
    monkeypatch.setattr(rbp.time, "sleep", lambda s: None)
    return tmp_path, rbp


def setup_fakes(rbp, monkeypatch, budget_after=None):
    calls = {"search": [], "pdf": [], "paid": 0}

    def fake_search(self, terms, max_results=None):
        calls["search"].append(terms[0])
        if budget_after is not None and len(calls["search"]) > budget_after:
            raise rbp.openalex_search.BudgetExhausted("OpenAlex's daily allowance ... midnight UTC")
        return [rbp.openalex_search.parse_work(w) for w in WORKS], 3, False

    def fake_get_pdf(session, pmcid, doi, email, use_unpaywall=True, want_text_fallback=True, oa_pdf_urls=(), cached_pdf=None):
        calls["pdf"].append((pmcid, tuple(oa_pdf_urls), use_unpaywall))
        if pmcid == "PMC100":
            return pdf_sources.PdfResult("ok", source="pmc_cloud", pdf=PDF, pmc_version="PMC100.1", is_retracted=False)
        if oa_pdf_urls:
            return pdf_sources.PdfResult("ok", source="openalex_oa_link", pdf=PDF)
        if cached_pdf:
            pdf, st = cached_pdf()
            calls["paid"] += 1
            if st == "ok":
                return pdf_sources.PdfResult("ok", source="openalex_cached", pdf=pdf)
        return pdf_sources.PdfResult("not_available")

    monkeypatch.setattr(rbp.openalex_search.OpenAlexClient, "search_protein", fake_search)
    monkeypatch.setattr(rbp.openalex_search.OpenAlexClient, "download_cached_pdf", lambda self, wid: (PDF, "ok"))
    monkeypatch.setattr(rbp.pdf_sources, "get_pdf", fake_get_pdf)
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml",
                        lambda session, pmcid: (b"<article><body><sec><title>Discussion</title><p>XML disc.</p></sec>"
                                                b"</body></article>", "ok") if pmcid == "PMC100" else (None, "not_available"))
    return calls


def run(rbp, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_by_protein.py", *argv])
    rbp.main()


def test_openalex_end_to_end(repo, monkeypatch):
    tmp, rbp = repo
    calls = setup_fakes(rbp, monkeypatch)
    run(rbp, monkeypatch, "--only", "TP53")

    out = tmp / "by_protein_openalex"
    papers = pd.read_csv(out / "proteins" / "DP00086_TP53" / "papers.csv", dtype=str).set_index("paper_id")
    assert set(papers.index) == {"W10", "W20", "W30"}
    assert papers.loc["W10", "PMCID"] == "PMC100" and papers.loc["W10", "pdf_source"] == "pmc_cloud"
    assert papers.loc["W10", "discussion"] == "XML disc." and papers.loc["W10", "text_source"].startswith("pmc_xml")
    assert papers.loc["W20", "pdf_source"] == "openalex_oa_link"
    assert papers.loc["W20", "Track"] == "IDR_general, IDR_interaction"
    assert papers.loc["W30", "pdf_status"] == "not_available"          # paid PDFs are off by default
    assert calls["paid"] == 0 and all(not unpay for _, _, unpay in calls["pdf"])
    assert (out / "proteins" / "DP00086_TP53" / "pdfs" / "W10_PMC100.pdf").exists()
    assert pd.read_csv(out / "protein_status.csv").iloc[0]["status"] == "done"
    assert len(pd.read_csv(out / "papers_master.csv")) == 3


def test_paid_pdfs_only_with_budget_and_capped(repo, monkeypatch):
    tmp, rbp = repo
    calls = setup_fakes(rbp, monkeypatch)
    run(rbp, monkeypatch, "--only", "TP53", "--openalex-pdfs", "5")
    papers = pd.read_csv(tmp / "by_protein_openalex" / "proteins" / "DP00086_TP53" / "papers.csv", dtype=str).set_index("paper_id")
    assert papers.loc["W30", "pdf_source"] == "openalex_cached" and calls["paid"] == 1


def test_budget_exhaustion_stops_cleanly_and_resumes(repo, monkeypatch):
    tmp, rbp = repo
    calls = setup_fakes(rbp, monkeypatch, budget_after=1)
    run(rbp, monkeypatch, "--proteins", "2")
    done = (tmp / "by_protein_openalex" / "protein_progress.txt").read_text().split()
    assert done == ["DP00086"]                                         # FUS not marked done
    calls = setup_fakes(rbp, monkeypatch)
    run(rbp, monkeypatch, "--proteins", "2")
    assert calls["search"] == ["FUS"]                                   # resumes with the next protein only


def test_watchdog_writes_where_a_hang_is(repo, monkeypatch):
    tmp, rbp = repo
    setup_fakes(rbp, monkeypatch)
    monkeypatch.setattr(rbp, "WATCHDOG_SEC", 1)
    import time as _t

    def hanging_xml(session, pmcid):
        end = _t.monotonic() + 1.6          # busy-wait: time.sleep is patched out by the fixture
        while _t.monotonic() < end:
            pass
        return None, "not_available"
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml", hanging_xml)
    run(rbp, monkeypatch, "--only", "TP53", "--no-pdf")
    report = (tmp / "by_protein_openalex" / "stuck_report.txt").read_text()
    assert "hanging_xml" in report and "get_text" in report


def test_papers_are_fetched_in_parallel(repo, monkeypatch):
    tmp, rbp = repo
    setup_fakes(rbp, monkeypatch)
    import threading
    import time as _t
    active, peak, lock = [0], [0], threading.Lock()

    def slow_xml(session, pmcid):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        end = _t.monotonic() + 0.3
        while _t.monotonic() < end:
            pass
        with lock:
            active[0] -= 1
        return None, "not_available"
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml", slow_xml)
    monkeypatch.setattr(rbp.openalex_search, "parse_work", rbp.openalex_search.parse_work)
    global WORKS
    old = WORKS
    WORKS = [work(f"W{i}", "TP53 IDR", "TP53 IDR", pmc_loc=str(500 + i)) for i in range(8)]
    try:
        run(rbp, monkeypatch, "--only", "TP53", "--no-pdf", "--workers", "4")
    finally:
        WORKS = old
    assert peak[0] >= 2                                                   # really concurrent
    papers = pd.read_csv(tmp / "by_protein_openalex" / "proteins" / "DP00086_TP53" / "papers.csv")
    assert len(papers) == 8
    text = pd.read_csv(tmp / "by_protein_openalex" / "text_cache.csv")
    assert len(text) == 8 and text["pmcid"].is_unique                     # no lost or garbled cache rows


def test_per_host_limit_caps_parallel_downloads_to_one_site(monkeypatch):
    import threading
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(pdf_sources, "PER_HOST_INTERVAL", 0.0)
    active, peak, lock = {}, {}, threading.Lock()

    def fake_inner(session, url, md5="", headers=None):
        host = url.split("/")[2]
        with lock:
            active[host] = active.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), active[host])
        end = _t.monotonic() + 0.1
        while _t.monotonic() < end:
            pass
        with lock:
            active[host] -= 1
        return PDF, "ok"
    monkeypatch.setattr(pdf_sources, "_download_pdf_bytes_inner", fake_inner)
    urls = [f"https://slowpub.example/{i}.pdf" for i in range(8)] + \
           [f"https://pmc-oa-opendata.s3.amazonaws.com/{i}.pdf" for i in range(8)]
    with ThreadPoolExecutor(12) as pool:
        list(pool.map(lambda u: pdf_sources._download_pdf_bytes(None, u), urls))
    assert peak["slowpub.example"] <= pdf_sources.PER_HOST_CONCURRENCY      # publisher protected
    assert peak["pmc-oa-opendata.s3.amazonaws.com"] > 2                     # AWS bucket not throttled


def test_next_protein_is_searched_while_current_one_downloads(repo, monkeypatch):
    tmp, rbp = repo
    calls = setup_fakes(rbp, monkeypatch)
    order = []
    real_process = rbp.Runner.process

    def spy_process(self, row, found="search"):
        order.append(("process", row["gene"], list(calls["search"])))
        return real_process(self, row, found)
    monkeypatch.setattr(rbp.Runner, "process", spy_process)
    run(rbp, monkeypatch, "--proteins", "2")
    assert order[0][0:2] == ("process", "TP53")
    assert order[1][2] == ["TP53", "FUS"]                                    # FUS search already done


def test_csv_locked_by_excel_waits_instead_of_crashing(repo, monkeypatch):
    tmp, rbp = repo
    attempts = []

    def flaky_write():
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "Permission denied")     # what Windows raises while Excel has the file open
        return "written"
    assert rbp.write_retry(tmp / "pdf_registry.csv", flaky_write) == "written" and len(attempts) == 3


def test_unreachable_oa_sites_count_as_no_pdf_not_endless_retry():
    s = PdfSession({"biorxiv.org": Resp(503, content=b"")})
    res = pdf_sources.fetch_from_urls(s, ["https://www.biorxiv.org/x.full.pdf"], "a@b.edu")
    assert res.status == "not_available"
