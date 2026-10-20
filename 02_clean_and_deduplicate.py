"""
Step 2: Clean the IDR/DisProt paper list and prepare the input for Step 3 (03_api_text_extractor.py).

- Safety-net dedupe by PMID (merging protein/track columns)
- Optional: remove papers already in your main database
- Split into (a) papers with a PMCID -> can go through steps 3-5 for Discussion/Conclusion text
           (b) papers without a PMCID -> abstract-only (not open access in PMC)
"""
import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).parent
PAPERS_FILE = BASE_DIR / "IDR_DisProt_Papers.csv"
MAIN_DB_FILE = BASE_DIR / "init2.csv"      # optional; skipped if the file doesn't exist (needs a 'pmid' column)

# Step 3 expects this exact filename
STEP3_INPUT = BASE_DIR / "public_research_papers_export_before_conclusion_and_discussion.csv"
NO_PMCID_OUT = BASE_DIR / "IDR_papers_no_pmcid_abstract_only.csv"

df = pd.read_csv(PAPERS_FILE, dtype={"PMID": str})

################# 1. DEDUPLICATE BY PMID ###############################
def merge_csv_strings(x):
    items = {s.strip() for v in x.dropna().astype(str) for s in v.split(",") if s.strip()}
    return ", ".join(sorted(items))

for col in ["Track", "DisProt_IDs", "Genes"]:
    if col in df.columns:
        df[col] = df.groupby("PMID")[col].transform(merge_csv_strings)
df = df.drop_duplicates(subset=["PMID"], keep="first")
print(f"{len(df)} unique papers.")

################# 2. OVERLAP WITH MAIN DATABASE ########################
if MAIN_DB_FILE.exists():
    main = pd.read_csv(MAIN_DB_FILE, dtype=str)
    clean = lambda s: s.astype(str).str.replace(r"\.0$", "", regex=True).str.strip().str.upper()
    overlap = df[clean(df["PMID"]).isin(set(clean(main["pmid"])))]
    print(f"Found {len(overlap)} papers that already exist in the main database!")
    overlap.to_csv(BASE_DIR / "overlap_papers.csv", index=False)
    df = df[~df.index.isin(overlap.index)]
else:
    print(f"(No {MAIN_DB_FILE.name} found - skipping overlap check.)")

################# 3. SPLIT BY PMCID AVAILABILITY #######################
df["PMCID"] = df["PMCID"].fillna("").astype(str).str.strip()
has_pmcid = df["PMCID"] != ""

no_pmcid = df[~has_pmcid]
no_pmcid.to_csv(NO_PMCID_OUT, index=False)
print(f"{len(no_pmcid)} papers have no PMCID -> {NO_PMCID_OUT.name} (abstract only)")

ready = df[has_pmcid].copy()
ready["PMCID"] = ready["PMCID"].apply(lambda x: x if x.upper().startswith("PMC") else f"PMC{x}")
ready = ready.rename(columns={"URL": "pubmed_url"})    # keep the word "url" free: step 4 uses it for the PDF link
ready.columns = [c.lower() for c in ready.columns]      # steps 3-5 expect lowercase 'pmcid'
ready["discussion"] = "Not Found"                       # placeholders that step 3 fills in
ready["conclusion"] = "Not Found"
ready.to_csv(STEP3_INPUT, index=False)
print(f"{len(ready)} papers with PMCIDs -> {STEP3_INPUT.name} (ready for step 3)")
