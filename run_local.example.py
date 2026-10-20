"""
TEMPLATE for a personal launcher, so you don't have to set your keys in the terminal every time.

    1. Copy this file to  run_local.py   (that name is in .gitignore, so it is never uploaded to GitHub)
    2. Fill in your own values below
    3. Run it exactly like run_by_protein.py:
           python run_local.py --only TP53,FUS
           python run_local.py --full

Never put real keys in this example file - it IS uploaded to GitHub.
"""
import os
import runpy
import sys
from pathlib import Path

os.environ["OPENALEX_API_KEY"] = "PASTE-YOUR-OPENALEX-KEY-HERE"   # free: https://openalex.org/settings/api-key
os.environ["CONTACT_EMAIL"] = "you@your-university.edu"         # sent to OpenAlex/NCBI so they can contact you
# os.environ["NCBI_API_KEY"] = "..."                            # optional, only for --source pubmed

if "PASTE-YOUR" in os.environ["OPENALEX_API_KEY"]:
    sys.exit("Edit run_local.py and paste your OpenAlex API key first.")

sys.argv[0] = str(Path(__file__).with_name("run_by_protein.py"))
runpy.run_path(sys.argv[0], run_name="__main__")
