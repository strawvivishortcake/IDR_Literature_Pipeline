"""
One-command runner for the whole IDR pipeline (steps 0-5).

    python run_pipeline.py                  # TEST run: first 10 DisProt proteins (about a minute)
    python run_pipeline.py --full           # ALL DisProt human proteins (hours; safe to Ctrl-C and re-run)
    python run_pipeline.py --proteins 50

Useful options for the full run:
    --from-step N     start at step N (0-5) using files already on disk, e.g. --from-step 3 to resume text extraction
    --no-pdf          skip step 5 (the slow PDF fallback); the step-4 file becomes the final master file
    --fresh           forget saved progress (search/paper cache, text-extraction progress) and start over

Credentials come from the environment (NCBI_EMAIL, NCBI_API_KEY); this file contains no secrets.
Everything printed is also saved to pipeline_run.log.
A test run followed by a full run reuses the searches already done.
"""
from __future__ import annotations

import argparse
import os
import runpy
import shutil
import sys
import time
from pathlib import Path

STEPS = ["00_fetch_disprot_proteins.py", "01_fetch_idr_pubmed.py", "02_clean_and_deduplicate.py",
         "03_api_text_extractor.py", "04_generate_fallback_urls.py", "05_pdf_fallback_scraper.py"]
DEFAULT_TEST_PROTEINS = 10
STATE_FILES = ["idr_search_progress.txt", "idr_search_hits.csv", "idr_search_log.csv", "idr_papers_cache.csv",
               "idr_failed_proteins.csv", "api_progress.txt", "pdf_progress.txt"]
NO_TEXT = ["Not Found", "API Fetch Failed", "PDF Download Failed", "Not Found in PDF Text", "Requires OCR (Image Only)"]


def check_dependencies() -> None:
    missing = []
    for module, package in [("pandas", "pandas"), ("requests", "requests"), ("Bio", "biopython")]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401
        except ImportError:
            missing.append("PyMuPDF")
    if missing:
        sys.exit(f"Missing packages: {', '.join(missing)}\nInstall with:  python -m pip install -r requirements.txt")


def run_step(base: Path, name: str) -> None:
    print(f"\n{'=' * 78}\nSTEP {name}\n{'=' * 78}", flush=True)
    start = time.time()
    runpy.run_path(str(base / name), run_name="__main__")
    print(f"--- {name} finished in {(time.time() - start) / 60:.1f} min", flush=True)


def summarize(base: Path) -> None:
    import pandas as pd

    def count(fname):
        f = base / fname
        return len(pd.read_csv(f)) if f.exists() else "n/a"

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    for fname, label in [("disprot_human_proteins.csv", "proteins searched"),
                         ("IDR_DisProt_Papers.csv", "unique papers found"),
                         ("IDR_papers_no_pmcid_abstract_only.csv", "  of which no PMCID (abstract only)"),
                         ("research_papers_final_master.csv", "papers with full-text extraction attempted")]:
        print(f"  {label:<55} {count(fname)}")

    final = base / "research_papers_final_master.csv"
    if final.exists():
        df = pd.read_csv(final, dtype=str)
        got = df["discussion"].notna() & ~df["discussion"].isin(NO_TEXT)
        print(f"  {'discussion text obtained':<55} {int(got.sum())} of {len(df)}")
        print("\n  Discussion column status:")
        for value, n in df["discussion"].where(~got, "(text extracted)").value_counts().items():
            print(f"    {n:>6}  {value}")

    ps = base / "protein_summary.csv"
    if ps.exists():
        s = pd.read_csv(ps)
        print(f"\n  PROTEIN COVERAGE ({len(s)} proteins)")
        print(f"    with no papers at all          : {int((s['n_papers'] == 0).sum())}")
        print(f"    search hit the result cap      : {int(s['capped'].sum())}")
        print(f"    failed / pending (re-run step 1): {int((s['status'] == 'failed_or_pending').sum())}")
        print("\n    Top 10 proteins by papers found:")
        for r in s.sort_values("n_papers", ascending=False).head(10).itertuples():
            label = r.gene if isinstance(r.gene, str) and r.gene else str(r.protein_name)[:14]
            print(f"      {label:<14} {r.disprot_id:<9} {r.n_papers:>5} papers")
    print(f"\nOutput files are in: {base}")


def main(test_proteins: int | None = DEFAULT_TEST_PROTEINS, from_step: int = 0, no_pdf: bool = False,
         fresh: bool = False) -> None:
    import pandas as pd
    check_dependencies()
    base = Path(__file__).parent
    if os.environ.get("CONTACT_EMAIL") and not os.environ.get("NCBI_EMAIL"):
        os.environ["NCBI_EMAIL"] = os.environ["CONTACT_EMAIL"]
    if not os.environ.get("NCBI_EMAIL"):
        sys.exit("Set your contact email first (NCBI requires one): CONTACT_EMAIL=you@school.edu")
    print(f"NCBI email: {os.environ['NCBI_EMAIL']} | API key: {'set' if os.environ.get('NCBI_API_KEY') else 'NOT set (slower)'}")
    print(f"Mode: {'FULL run' if test_proteins is None else f'TEST run ({test_proteins} proteins)'} | "
          f"start at step {from_step} | PDF step: {'skipped' if no_pdf else 'on'}")
    if test_proteins is None:
        print("Full run: step 1 (PubMed searches) is usually minutes to an hour; step 3 (full text) scales with the number of\n"
              "papers and can take hours. Keep the computer awake and plugged in. Ctrl-C is safe: run the same command again to resume.")

    if fresh:
        for f in STATE_FILES:
            (base / f).unlink(missing_ok=True)
        print("Cleared saved progress.")

    for i, step in enumerate(STEPS):
        if i < from_step:
            continue
        if i == 5 and no_pdf:
            shutil.copy(base / "research_papers_updated_text_2.csv", base / "research_papers_final_master.csv")
            print("\nSkipping step 5 (--no-pdf): copied the step-4 file to research_papers_final_master.csv")
            continue
        try:
            run_step(base, step)
        except SystemExit as e:
            print(f"\nStopped in {step}: {e}")
            return
        if i == 0:
            proteins_csv = base / "disprot_human_proteins.csv"
            df = pd.read_csv(proteins_csv, dtype=str)
            print(f"DisProt returned {len(df)} human proteins.")
            if len(df) == 0:
                sys.exit("Step 0 returned no proteins - check your internet connection / the DisProt API.")
            if test_proteins is not None:
                df.to_csv(base / "disprot_full.csv", index=False)      # full list kept for a later --full run
                df.head(test_proteins).to_csv(proteins_csv, index=False)
                print(f"Test mode: keeping the first {min(test_proteins, len(df))} proteins.")
    summarize(base)


class Tee:
    """Send everything printed to the console AND a log file."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)

    def flush(self):
        for s in self.streams:
            s.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the IDR literature pipeline (steps 0-5).")
    parser.add_argument("--full", action="store_true", help="run every DisProt protein instead of a 10-protein test")
    parser.add_argument("--proteins", type=int, help="test run with this many proteins")
    parser.add_argument("--from-step", type=int, default=0, choices=range(6), help="start at this step (0-5)")
    parser.add_argument("--no-pdf", action="store_true", help="skip the slow PDF fallback (step 5)")
    parser.add_argument("--fresh", action="store_true", help="discard saved progress and start over")
    args = parser.parse_args()

    log = open(Path(__file__).with_name("pipeline_run.log"), "a", encoding="utf-8")
    sys.stdout, sys.stderr = Tee(sys.__stdout__, log), Tee(sys.__stderr__, log)
    print(f"\n##### run started {time.strftime('%Y-%m-%d %H:%M:%S')} #####")
    main(None if args.full else (args.proteins or DEFAULT_TEST_PROTEINS), args.from_step, args.no_pdf,
         args.fresh)
