"""
Step 5: Full-text PDFs. Downloads the PDF of EVERY paper from step 4 (saved in pdfs/ as <PMID>_<PMCID>.pdf)
through pdf_sources.py: the official PMC Cloud Service first, then Unpaywall by DOI. When PMC only offers
text for a paper's license, the plain-text full text is saved instead (<PMID>_<PMCID>_fulltext.txt).
For papers still missing a Discussion / Conclusion after step 3, the sections are extracted from the PDF
with PyMuPDF. Text the API (step 3) already got is never overwritten.

Set IDR_NO_UNPAYWALL=1 to use PMC only. For per-protein batches, use run_by_protein.py instead.
"""
from __future__ import annotations

import os
import re
import time
import logging
from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:
    import fitz
import pandas as pd

import pdf_sources

# --- CONFIGURATION ---
PDF_DIR = Path(__file__).parent / "pdfs"
CHECKPOINT_EVERY = 25
MISSING = {'Not Found', 'API Fetch Failed', 'PDF Download Failed'}  # values that mean "try the PDF"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Headings may be numbered ("4. Discussion", "5.1 Conclusions") and must sit alone on their line.
_NUM = r'(?:\d+(?:\.\d+)*\.?\s*)?'
_END_OF_DISCUSSION = r'(?:Conclusions?|References|Acknowledge?ments?|Materials\s+and\s+Methods|Methods|Funding|Author\s+Contributions)'
_END_OF_CONCLUSION = r'(?:References|Acknowledge?ments?|Funding|Author\s+Contributions|Supplementary[^\n]*)'
DISCUSSION_RE = re.compile(rf'\n{_NUM}Discussion\s*\n(.*?)(?=\n{_NUM}{_END_OF_DISCUSSION}\s*\n|\Z)', re.IGNORECASE | re.DOTALL)
CONCLUSION_RE = re.compile(rf'\n{_NUM}Conclusions?\s*\n(.*?)(?=\n{_NUM}{_END_OF_CONCLUSION}\s*\n|\Z)', re.IGNORECASE | re.DOTALL)


def _longest(regex: re.Pattern, text: str) -> str:
    """Longest match wins, so a stray 'Discussion' line in a running header can't beat the real section."""
    matches = [m.group(1).strip() for m in regex.finditer(text)]
    return max(matches, key=len) if matches else "Not Found in PDF Text"


def extract_sections_from_text(full_text: str) -> tuple[str, str]:
    if len(full_text.strip()) < 500:   # almost no text layer -> scanned image
        return "Requires OCR (Image Only)", "Requires OCR (Image Only)"
    return _longest(DISCUSSION_RE, full_text), _longest(CONCLUSION_RE, full_text)


def extract_sections_from_pdf(pdf_path: str) -> tuple[str, str]:
    doc = fitz.open(pdf_path)
    full_text = ""
    for page in doc:
        full_text += page.get_text("text", sort=True) + "\n"   # sort=True keeps column reading order
    doc.close()
    return extract_sections_from_text("\n" + full_text)


def _is_missing(value) -> bool:
    return pd.isna(value) or value in MISSING


def scrape_fallback_pdfs(input_path: Path, output_path: Path) -> None:
    logger.info(f"Loading dataset from: {input_path.name}")
    try:
        df = pd.read_csv(input_path, dtype=str)
    except FileNotFoundError:
        logger.error(f"Could not find {input_path.name}. Ensure it is in the same folder.")
        return
    for col in ("pdf_status", "pdf_source", "pdf_file", "fulltext_file", "pmc_version", "is_retracted", "pdf_notes"):
        if col not in df.columns:
            df[col] = ""
    df["pmcid"] = df["pmcid"].apply(pdf_sources.normalize_pmcid)
    if "doi" not in df.columns:
        df["doi"] = ""

    # --- resume: reuse results for papers already handled in an earlier run ---
    progress_path = output_path.with_name("pdf_progress.txt")
    done = set(progress_path.read_text().split()) if progress_path.exists() else set()
    if done and output_path.exists():
        prev = pd.read_csv(output_path, dtype=str).drop_duplicates("pmcid").set_index("pmcid")
        reuse = df["pmcid"].isin(done) & df["pmcid"].isin(prev.index)
        for col in ("discussion", "conclusion", "pdf_status", "pdf_source", "pdf_file", "fulltext_file",
                    "pmc_version", "is_retracted", "pdf_notes"):
            if col in prev.columns:
                df.loc[reuse, col] = df.loc[reuse, "pmcid"].map(prev[col])
        logger.info(f"Resuming: reusing results for {int(reuse.sum())} papers already processed.")

    target_indices = df[(df["pmcid"] != "") & ~df["pmcid"].isin(done)].drop_duplicates("pmcid").index
    logger.info(f"Found {len(target_indices)} papers to fetch PDFs for.")
    if len(target_indices) == 0:
        df.to_csv(output_path, index=False)
        return

    def save():
        df.to_csv(output_path, index=False)
        progress_path.write_text("\n".join(sorted(done)))

    PDF_DIR.mkdir(exist_ok=True)
    session = pdf_sources.make_session()
    email = os.environ.get("CONTACT_EMAIL") or os.environ.get("NCBI_EMAIL") or ""
    use_unpaywall = os.environ.get("IDR_NO_UNPAYWALL", "0") != "1" and bool(email)   # Unpaywall requires an email
    started, counts = time.time(), {"pdf": 0, "text_only": 0, "not_available": 0, "error": 0}
    for count, idx in enumerate(target_indices, 1):
        pmcid, doi = df.loc[idx, "pmcid"], str(df.loc[idx, "doi"] or "")
        rows = df.index[df["pmcid"] == pmcid]
        stem = f"{df.loc[idx, 'pmid']}_{pmcid}"
        res = pdf_sources.get_pdf(session, pmcid, doi, email, use_unpaywall=use_unpaywall)
        info = {"pdf_source": res.source, "pmc_version": res.pmc_version, "pdf_notes": "; ".join(res.notes),
                "is_retracted": "" if res.is_retracted is None else str(res.is_retracted)}
        if res.pdf:
            pdf_path = PDF_DIR / f"{stem}.pdf"
            pdf_path.write_bytes(res.pdf)
            info.update(pdf_status="pdf", pdf_file=f"pdfs/{pdf_path.name}")
            # Only fill what is still missing; never clobber API text
            if _is_missing(df.loc[idx, "discussion"]) or _is_missing(df.loc[idx, "conclusion"]):
                try:
                    discussion, conclusion = extract_sections_from_pdf(str(pdf_path))
                except Exception as e:
                    logger.warning(f"{pmcid}: could not read PDF ({e})")
                    discussion = conclusion = "Not Found in PDF Text"
                for col, val in (("discussion", discussion), ("conclusion", conclusion)):
                    keep = df.loc[rows, col].apply(_is_missing)
                    df.loc[rows[keep.values], col] = val
        elif res.fulltext:
            txt_path = PDF_DIR / f"{stem}_fulltext.txt"
            txt_path.write_text(res.fulltext, encoding="utf-8")
            info.update(pdf_status="text_only", fulltext_file=f"pdfs/{txt_path.name}")
        else:
            info["pdf_status"] = "error" if res.status == "error" else "not_available"
        for col, val in info.items():
            df.loc[rows, col] = val
        counts[info["pdf_status"]] += 1
        if info["pdf_status"] != "error":        # network errors are retried on the next run
            done.add(pmcid)

        if count % CHECKPOINT_EVERY == 0:
            save()
            eta = (time.time() - started) / count * (len(target_indices) - count) / 60
            logger.info(f"[{count}/{len(target_indices)}] PDFs {counts['pdf']} | text only {counts['text_only']} | "
                        f"none {counts['not_available']} | errors {counts['error']} | ~{eta:.0f} min left")

    logger.info(f"Saving final dataset to: {output_path.name}")
    save()
    logger.info(f"PDF step complete! {counts}" + (" (re-run this step to retry the errors)" if counts["error"] else ""))


if __name__ == "__main__":
    BASE_DIR = Path(__file__).parent
    INPUT_FILE = BASE_DIR / "research_papers_updated_text_2.csv"
    OUTPUT_FILE = BASE_DIR / "research_papers_final_master.csv"
    scrape_fallback_pdfs(INPUT_FILE, OUTPUT_FILE)
