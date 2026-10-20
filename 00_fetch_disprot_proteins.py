"""
Step 0: Pull the list of human proteins from DisProt (same filters as the browse URL you shared:
release=current, show_ambiguous=true, show_obsolete=false, ncbi_taxon_id=9606).

Output: disprot_human_proteins.csv  (one row per DisProt entry)
"""
import time
import logging
from pathlib import Path

import requests
import pandas as pd

# --- CONFIGURATION ---
API_URL = "https://disprot.org/api/search"
PAGE_SIZE = 100        # the website uses 20; if the API rejects 100, drop this to 20
REQUEST_DELAY = 0.5    # seconds between pages
TIMEOUT = 60
EXPECTED_PROTEINS = 1339   # what the DisProt browse page shows for human (release=current); only used for a sanity warning

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def _as_text(x) -> str:
    """DisProt sometimes gives {'value': 'TP53'} and sometimes a plain string."""
    if isinstance(x, dict):
        return str(x.get("value", "")).strip()
    return str(x).strip() if x else ""


def parse_entry(entry: dict) -> dict:
    genes = entry.get("genes") or []
    gene_name, synonyms = "", []
    if genes:
        g0 = genes[0]
        gene_name = _as_text(g0.get("name"))
        synonyms = [_as_text(s) for s in (g0.get("synonyms") or [])]
    return {
        "disprot_id": entry.get("disprot_id"),
        "uniprot_acc": entry.get("acc"),
        "gene": gene_name,
        "gene_synonyms": "; ".join(s for s in synonyms if s),
        "protein_name": entry.get("name"),
        "organism": entry.get("organism"),
        "length": entry.get("length"),
        "disorder_content": entry.get("disorder_content"),
        "regions_counter": entry.get("regions_counter"),
    }


def fetch_all_proteins() -> pd.DataFrame:
    rows, seen = [], set()
    page = 0
    while True:
        params = {
            "sort_field": "disprot_id",
            "sort_value": "asc",
            "page_size": PAGE_SIZE,
            "page": page,
            "release": "current",
            "show_ambiguous": "true",
            "show_obsolete": "false",
            "ncbi_taxon_id": 9606,
        }
        resp = requests.get(API_URL, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", [])
        if page == 0:
            logger.info(f"API reports total size: {payload.get('size', 'unknown')}")
        if not data:
            break

        new = 0
        for entry in data:
            did = entry.get("disprot_id")
            if did in seen:
                continue
            seen.add(did)
            rows.append(parse_entry(entry))
            new += 1
        logger.info(f"Page {page}: {len(data)} entries ({new} new) | running total {len(rows)}")

        if new == 0:  # API ignored 'page' and is repeating itself -> stop instead of looping forever
            logger.warning("Page returned no new entries; stopping.")
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return pd.DataFrame(rows)


if __name__ == "__main__":
    BASE_DIR = Path(__file__).parent
    df = fetch_all_proteins()
    df.to_csv(BASE_DIR / "disprot_human_proteins.csv", index=False)
    logger.info(f"Saved {len(df)} proteins. Missing gene symbol: {(df['gene'] == '').sum()}")
    if not (0.95 * EXPECTED_PROTEINS <= len(df) <= 1.05 * EXPECTED_PROTEINS):
        logger.warning(f"Expected about {EXPECTED_PROTEINS} proteins but got {len(df)}. "
                       "DisProt releases change over time, but if this is far off the API may have paginated wrongly.")
