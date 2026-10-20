"""
Offline tests for pdf_sources.py and run_by_protein.py (no internet: every network call is faked).

    pytest -v tests/test_by_protein.py
"""
from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import pdf_sources  # noqa: E402

PDF = b"%PDF-1.4\n" + b"fake pdf body " * 50
MD5 = hashlib.md5(PDF).hexdigest()


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}
        self.encoding = None

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """Routes URLs to canned responses; records every call."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, **kw):
        key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
        self.calls.append(key)
        for pattern, resp in self.routes.items():
            if pattern in key:
                return resp() if callable(resp) else resp
        return FakeResponse(404)


def listing(*prefixes):
    items = "".join(f"<CommonPrefixes><Prefix>{p}/</Prefix></CommonPrefixes>" for p in prefixes)
    return FakeResponse(200, f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}</ListBucketResult>'.encode())


def meta(prefix, pdf=True, manuscript=False, retracted=False):
    d = {"pmcid": prefix.split(".")[0], "version": prefix.split(".")[1], "is_manuscript": manuscript,
         "is_retracted": retracted, "license_code": "CC BY",
         "text_url": f"s3://pmc-oa-opendata/{prefix}/{prefix}.txt?md5=x"}
    if pdf:
        d["pdf_url"] = f"s3://pmc-oa-opendata/{prefix}/{prefix}.pdf?md5={MD5}"
    return FakeResponse(200, json.dumps(d).encode())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pdf_sources.time, "sleep", lambda s: None)


# ------------------------------------------------------------------ pdf_sources
def test_s3_url_is_rebased_to_https_and_md5_kept():
    url, md5 = pdf_sources.bucket_https_url("s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf?md5=abc")
    assert url == "https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.pdf"
    assert md5 == "abc"


def test_version_choice_prefers_published_pdf_over_manuscript():
    metas = [{"version": "1", "is_manuscript": "yes", "pdf_url": "a"},
             {"version": "2", "is_manuscript": False, "pdf_url": "b"},
             {"version": "3", "is_manuscript": False}]
    assert pdf_sources.choose_version(metas)["pdf_url"] == "b"


def test_pmc_pdf_downloaded_and_checksum_verified():
    s = FakeSession({"prefix=PMC123.": listing("PMC123.1"), "metadata/PMC123.1.json": meta("PMC123.1"),
                     "PMC123.1/PMC123.1.pdf": FakeResponse(200, PDF)})
    res = pdf_sources.fetch_from_pmc(s, "123")
    assert res.status == "ok" and res.pdf == PDF and res.pmc_version == "PMC123.1"
    assert not any("md5=" in c for c in s.calls if c.endswith(".pdf"))   # query string stripped before download


def test_checksum_mismatch_is_retryable_error():
    s = FakeSession({"prefix=PMC123.": listing("PMC123.1"), "metadata/PMC123.1.json": meta("PMC123.1"),
                     "PMC123.1/PMC123.1.pdf": FakeResponse(200, b"%PDF-1.4 corrupted")})
    assert pdf_sources.fetch_from_pmc(s, "PMC123").status == "error"


def test_no_pdf_license_falls_back_to_plain_text():
    s = FakeSession({"prefix=PMC9.": listing("PMC9.1"), "metadata/PMC9.1.json": meta("PMC9.1", pdf=False),
                     "PMC9.1/PMC9.1.txt": FakeResponse(200, b"Full text of the article")})
    res = pdf_sources.fetch_from_pmc(s, "PMC9")
    assert res.status == "not_available" and res.fulltext == "Full text of the article"


def test_prefix_has_trailing_dot_so_pmc12_does_not_match_pmc123():
    s = FakeSession({"prefix=PMC12.": listing()})
    assert pdf_sources.fetch_from_pmc(s, "PMC12").status == "not_available"
    assert any("prefix=PMC12." in c for c in s.calls)


def test_html_bot_page_is_not_saved_as_pdf():
    s = FakeSession({"api.unpaywall.org": FakeResponse(200, json.dumps(
                        {"is_oa": True, "best_oa_location": {"url_for_pdf": "https://publisher.example/x.pdf"}}).encode()),
                     "publisher.example": FakeResponse(200, b"<html>Just a moment...</html>")})
    assert pdf_sources.fetch_from_unpaywall(s, "10.1/abc", "a@b.edu").status == "not_available"


def test_unpaywall_used_when_paper_has_no_pmcid():
    s = FakeSession({"api.unpaywall.org": FakeResponse(200, json.dumps(
                        {"is_oa": True, "best_oa_location": {"url_for_pdf": "https://repo.example/x.pdf"}}).encode()),
                     "repo.example": FakeResponse(200, PDF)})
    res = pdf_sources.get_pdf(s, "", "https://doi.org/10.1/abc", "a@b.edu")
    assert res.status == "ok" and res.source == "unpaywall"


# ------------------------------------------------------------------ run_by_protein (end to end)
class Aid(str):
    def __new__(cls, value, id_type):
        obj = super().__new__(cls, value)
        obj.attributes = {"IdType": id_type}
        return obj


def article(pmid, title, abstract, pmcid="", doi=""):
    ids = [Aid(pmcid, "pmc")] if pmcid else []
    ids += [Aid(doi, "doi")] if doi else []
    return {"MedlineCitation": {"PMID": pmid, "Article": {
                "ArticleTitle": title, "Abstract": {"AbstractText": [abstract]}, "AuthorList": [{"LastName": "Doe", "ForeName": "J"}],
                "Journal": {"Title": "J Test", "JournalIssue": {"PubDate": {"Year": "2020"}}}, "PublicationTypeList": ["Journal Article"]}},
            "PubmedData": {"ArticleIdList": ids}}


ARTICLES = {
    "1": article("1", "TP53 intrinsically disordered region binds DNA", "TP53 IDR study.", pmcid="PMC100", doi="10.1/a"),
    "2": article("2", "Disordered protein regions", "We set the IDR aside.", pmcid="PMC200"),          # no confidence filter: still processed
    "3": article("3", "TP53 disordered region condensates", "TP53 IDR phase separation.", doi="10.1/c"),  # no PMCID
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for f in REPO.glob("*.py"):
        shutil.copy(f, tmp_path / f.name)
    pd.DataFrame([{"disprot_id": "DP00086", "uniprot_acc": "P04637", "gene": "TP53", "gene_synonyms": "P53",
                   "protein_name": "Cellular tumor antigen p53"},
                  {"disprot_id": "DP99999", "uniprot_acc": "X", "gene": "", "gene_synonyms": "", "protein_name": "x"}]
                 ).to_csv(tmp_path / "disprot_human_proteins.csv", index=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTACT_EMAIL", "tester@example.edu")
    for m in ("run_by_protein", "pdf_sources", "openalex_search"):
        sys.modules.pop(m, None)
    rbp = importlib.import_module("run_by_protein")
    monkeypatch.setattr(rbp.time, "sleep", lambda s: None)
    return tmp_path, rbp


def make_runner(rbp, monkeypatch, *argv):
    if "--source" not in argv:
        argv = ("--source", "pubmed", *argv)
    monkeypatch.setattr(sys, "argv", ["run_by_protein.py", *argv])
    calls = {"search": 0, "pdf": 0}

    def fake_search(row, clause):
        calls["search"] += 1
        hits = [{"pmid": p, "disprot_id": row["disprot_id"], "gene": row["gene"], "uniprot_acc": "",
                 "track": t, "ambiguous_symbol": False} for p in ARTICLES for t in ("IDR_general",)]
        hits.append({**hits[0], "track": "IDR_interaction"})
        return hits, [{"disprot_id": row["disprot_id"], "track": "IDR_general", "n_total": 3, "n_retrieved": 3, "capped": False}]

    def fake_entrez(func, **kw):
        return {"PubmedArticle": [ARTICLES[p] for p in kw["id"].split(",")]}

    def fake_bioc(session, pmcid):
        if pmcid == "PMC100":
            return [{"passages": [{"infons": {"section_type": "DISCUSS", "type": "paragraph"}, "text": "BioC discussion."}]}], "ok"
        return None, "not_oa"

    def fake_get_pdf(session, pmcid, doi, email, use_unpaywall=True, want_text_fallback=True, **kw):
        calls["pdf"] += 1
        if pmcid == "PMC100":
            return pdf_sources.PdfResult("ok", source="pmc_cloud", pdf=PDF, pmc_version="PMC100.1", is_retracted=False)
        return pdf_sources.PdfResult("not_available", notes=["no legal open-access copy known"])

    orig_init = rbp.Runner.__init__

    def init(self, args):
        orig_init(self, args)
        self.s1.search_one_protein = fake_search
        self.s1.entrez_retry = fake_entrez
        self.s3.fetch_bioc_json = fake_bioc
    monkeypatch.setattr(rbp.Runner, "__init__", init)
    monkeypatch.setattr(rbp.pdf_sources, "get_pdf", fake_get_pdf)
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml",
                        lambda session, pmcid: (JATS, "ok") if pmcid == "PMC100" else (None, "not_available"))
    return calls


def test_end_to_end_per_protein_folders_pdfs_and_resume(repo, monkeypatch):
    tmp, rbp = repo
    calls = make_runner(rbp, monkeypatch)
    rbp.main()

    folder = tmp / "by_protein" / "proteins" / "DP00086_TP53"
    papers = pd.read_csv(folder / "papers.csv", dtype=str)
    assert set(papers["PMID"]) == {"1", "2", "3"}
    row1 = papers.set_index("PMID").loc["1"]
    assert row1["pdf_status"] == "pdf" and row1["pdf_file"] == "pdfs/1_PMC100.pdf"
    assert (folder / "pdfs" / "1_PMC100.pdf").read_bytes() == PDF
    assert (folder / "pdfs" / "1_PMC100.pdf").stat().st_ino == (tmp / "by_protein" / "_pdf_store" / "1.pdf").stat().st_ino
    assert row1["discussion"].startswith("The IDR binds DNA.") and row1["text_source"] == "pmc_xml"
    assert "IDR_interaction" in row1["Track"]
    assert papers.set_index("PMID").loc["2", "pdf_status"] == "not_available"            # processed like every other paper
    assert "Confidence" not in papers.columns
    assert papers.set_index("PMID").loc["3", "pdf_status"] == "not_available"

    status = pd.read_csv(tmp / "by_protein" / "protein_status.csv", dtype=str).set_index("disprot_id")
    assert status.loc["DP00086", "status"] == "done" and status.loc["DP99999", "status"] == "skipped_no_search_term"
    master = pd.read_csv(tmp / "by_protein" / "papers_master.csv", dtype=str)
    assert len(master) == 3 and master.set_index("PMID").loc["1", "DisProt_IDs"] == "DP00086"

    # second run: everything done -> no new searches or downloads
    n_search, n_pdf = calls["search"], calls["pdf"]
    calls2 = make_runner(rbp, monkeypatch)
    rbp.main()
    assert calls2["search"] == 0 and calls2["pdf"] == 0
    assert (n_search, n_pdf) == (1, 3)


def test_redo_reuses_cached_pdfs_and_text(repo, monkeypatch):
    tmp, rbp = repo
    make_runner(rbp, monkeypatch, "--only", "TP53")
    rbp.main()
    calls = make_runner(rbp, monkeypatch, "--only", "TP53", "--redo")
    rbp.main()
    assert calls["search"] == 1 and calls["pdf"] == 0      # searched again, but nothing re-downloaded


# ------------------------------------------------------------------ step 5 (classic pipeline)
def test_step5_saves_pdf_for_every_paper_and_only_fills_missing_text(tmp_path, monkeypatch):
    for m in ("pdf_sources",):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("step5", REPO / "05_pdf_fallback_scraper.py")
    s5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s5)
    monkeypatch.setattr(s5, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(s5, "extract_sections_from_pdf", lambda path: ("PDF discussion", "PDF conclusion"))
    monkeypatch.setattr(s5.pdf_sources, "get_pdf", lambda session, pmcid, doi, email, use_unpaywall=True:
                        pdf_sources.PdfResult("ok", source="pmc_cloud", pdf=PDF) if pmcid == "PMC1"
                        else pdf_sources.PdfResult("not_available", fulltext="plain text" if pmcid == "PMC2" else None))
    pd.DataFrame([{"pmid": "11", "pmcid": "PMC1", "doi": "", "discussion": "API text", "conclusion": "Not Found"},
                  {"pmid": "22", "pmcid": "PMC2", "doi": "", "discussion": "Not Found", "conclusion": "Not Found"}]
                 ).to_csv(tmp_path / "in.csv", index=False)
    s5.scrape_fallback_pdfs(tmp_path / "in.csv", tmp_path / "out.csv")
    out = pd.read_csv(tmp_path / "out.csv", dtype=str).set_index("pmid")
    assert (tmp_path / "pdfs" / "11_PMC1.pdf").read_bytes() == PDF
    assert out.loc["11", "discussion"] == "API text" and out.loc["11", "conclusion"] == "PDF conclusion"
    assert out.loc["22", "pdf_status"] == "text_only" and (tmp_path / "pdfs" / "22_PMC2_fulltext.txt").exists()


def test_step4_writes_output_even_when_nothing_needs_a_pdf(tmp_path):
    spec = importlib.util.spec_from_file_location("step4", REPO / "04_generate_fallback_urls.py")
    s4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s4)
    pd.DataFrame([{"pmcid": "PMC1", "discussion": "x", "conclusion": "y"}]).to_csv(tmp_path / "in.csv", index=False)
    s4.build_fallback_urls(tmp_path / "in.csv", tmp_path / "out.csv")
    assert (tmp_path / "out.csv").exists()


# ------------------------------------------------------------------ BioC outage -> PMC XML fallback
JATS = b"""<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front/><body>
  <sec><title>Introduction</title><p>Intro text.</p></sec>
  <sec sec-type="results|discussion"><title>Results and Discussion</title>
    <p>The <italic>IDR</italic> binds DNA.</p>
    <fig><caption><p>Figure caption, not text.</p></caption></fig>
    <sec><title>Phase separation</title><p>Nested paragraph.</p></sec>
  </sec>
  <sec><title>4. Conclusions</title><p>It matters.</p></sec>
</body><back><ref-list><p>ref</p></ref-list></back></article>"""


def test_sections_from_jats():
    disc, concl = pdf_sources.sections_from_jats(JATS)
    assert disc == "The IDR binds DNA.\n\nNested paragraph."
    assert concl == "It matters."
    assert pdf_sources.sections_from_jats(b"not xml") == ("Not Found", "Not Found")


def _runner(rbp, monkeypatch):
    make_runner(rbp, monkeypatch, "--only", "TP53", "--no-pdf")
    return rbp.Runner(rbp.argparse.Namespace(no_unpaywall=False, no_text_fallback=False, retry_missing=False,
                                             no_pdf=True, redo=False))


def test_pmc_xml_is_primary_and_bioc_is_not_called(repo, monkeypatch):
    tmp, rbp = repo
    runner = _runner(rbp, monkeypatch)
    bioc_calls = []
    runner.s3.fetch_bioc_json = lambda session, pmcid: bioc_calls.append(pmcid) or (None, "error")
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml", lambda session, pmcid: (JATS, "ok"))
    for i in range(10):
        disc, concl, status = runner.get_text(f"PMC{i}")
        assert status == "ok_pmc_xml" and concl == "It matters."
    assert bioc_calls == []                                   # NCBI's BioC server is never touched
    assert runner.text.get("PMC0")["text_status"] == "ok_pmc_xml"


def test_not_in_pmc_bucket_means_not_oa_without_asking_bioc(repo, monkeypatch):
    tmp, rbp = repo
    runner = _runner(rbp, monkeypatch)
    bioc_calls = []
    runner.s3.fetch_bioc_json = lambda session, pmcid: bioc_calls.append(pmcid) or (None, "error")
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml", lambda session, pmcid: (None, "not_available"))
    assert runner.get_text("PMC7")[2] == "not_oa" and bioc_calls == []


def test_bioc_fallback_stops_immediately_on_429(repo, monkeypatch):
    tmp, rbp = repo
    runner = _runner(rbp, monkeypatch)
    bioc_calls = []
    runner.s3.fetch_bioc_json = lambda session, pmcid: bioc_calls.append(pmcid) or (None, "rate_limited")
    monkeypatch.setattr(rbp.pdf_sources, "fetch_pmc_xml", lambda session, pmcid: (None, "error"))  # bucket unreachable
    for i in range(10):
        assert runner.get_text(f"PMC{i}")[2] == "error"       # left uncached -> retried next run
    assert len(bioc_calls) == 1                               # one 429 and BioC is paused
    assert runner.text.get("PMC0") is None


def test_step3_waits_and_retries_once_after_429(monkeypatch):
    spec = importlib.util.spec_from_file_location("step3", REPO / "03_api_text_extractor.py")
    s3 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s3)
    s = FakeSession({"BioC_json": FakeResponse(429, b"", {"Retry-After": "7"})})
    assert s3.fetch_bioc_json(s, "PMC1") == (None, "rate_limited")
    assert s3.fetch_bioc_json.last_wait == 7
