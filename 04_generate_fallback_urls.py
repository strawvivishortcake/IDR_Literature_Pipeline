"""
Step 4: Flag papers the API couldn't fully read and add their PMC article page link (for humans to click).
Step 5 no longer downloads from these links (PMC blocks scripted downloads there); it uses pdf_sources.py.
"""
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

MISSING = ['Not Found', 'API Fetch Failed']


def build_fallback_urls(input_path: Path, output_path: Path) -> None:
    logger.info(f"Loading dataset from: {input_path.name}")
    try:
        df = pd.read_csv(input_path, dtype=str)
    except FileNotFoundError:
        logger.error(f"Could not find {input_path.name}. Ensure it is in the same folder as this script.")
        return

    # Valid PMCID AND (discussion or conclusion is missing)
    needs_update = (
        df['pmcid'].notna() &
        (df['pmcid'].str.strip() != '') &
        ~df['pmcid'].isin(['(NULL)', 'Not Found in PMC']) &
        (df['discussion'].isin(MISSING) | df['conclusion'].isin(MISSING) |
         df['discussion'].isna() | df['conclusion'].isna())
    )

    target_indices = df[needs_update].index
    logger.info(f"Found {len(target_indices)} papers requiring fallback PDF URLs.")

    if len(target_indices) == 0:
        logger.info("No fallback URLs needed.")
        df.to_csv(output_path, index=False)      # always write, so step 5 never reads a missing or stale file
        return

    logger.info("Generating URLs...")
    clean_pmcids = df.loc[target_indices, 'pmcid'].astype(str).str.strip()
    clean_pmcids = clean_pmcids.apply(lambda x: x if x.upper().startswith("PMC") else f"PMC{x}")
    df.loc[target_indices, "url"] = "https://pmc.ncbi.nlm.nih.gov/articles/" + clean_pmcids + "/"

    logger.info(f"Saving updated dataset to: {output_path.name}")
    df.to_csv(output_path, index=False)
    logger.info("Fallback generation complete!")


if __name__ == "__main__":
    BASE_DIR = Path(__file__).parent
    INPUT_FILE = BASE_DIR / "research_papers_updated_text.csv"
    OUTPUT_FILE = BASE_DIR / "research_papers_updated_text_2.csv"
    build_fallback_urls(INPUT_FILE, OUTPUT_FILE)
