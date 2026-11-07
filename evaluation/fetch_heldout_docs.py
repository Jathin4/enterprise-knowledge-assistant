"""Download the real public documents used by the held-out test. They are US government works
(public domain) and stay out of Git because they are large.

    python evaluation/fetch_heldout_docs.py <target folder>
"""
import sys
from pathlib import Path

import httpx

DOCS = {
    "NIST.SP.800-63b.pdf": "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63b.pdf",
    "NIST.SP.800-61r2.pdf": "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-61r2.pdf",
}

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
for name, url in DOCS.items():
    path = target / name
    if not path.exists():
        response = httpx.get(url, follow_redirects=True, timeout=300)
        response.raise_for_status()
        if not response.content.startswith(b"%PDF-"):
            sys.exit(f"{url} did not return a PDF")
        path.write_bytes(response.content)
    print(f"{name}: {path.stat().st_size} bytes")
