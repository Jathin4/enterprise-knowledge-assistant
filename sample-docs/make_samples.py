"""Create synthetic sample documents for a fictional company, Brightline Analytics.

    python make_samples.py      (writes into this folder; the PDF is printed by Microsoft Edge)

Used for the Stage 2 milestone and, later, the Stage 3 benchmark questions.
"""
import subprocess
import tempfile
from pathlib import Path

from docx import Document

HERE = Path(__file__).parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

# ---------- Engineering: API authentication guide (DOCX with headings + a table) ----------

def make_docx():
    d = Document()
    d.add_heading("Brightline Analytics: API Authentication Guide", 0)
    d.add_paragraph("Version 2.3. Owner: Platform Engineering. This guide explains how services and "
                    "partners authenticate requests to the Brightline Data API.")
    d.add_heading("1. Overview", 1)
    d.add_paragraph("Every request to the Data API must be authenticated. Two methods are supported: "
                    "OAuth 2.0 client credentials for partner integrations, and HMAC request signing for "
                    "internal services. Unauthenticated requests are rejected with HTTP 401.")
    d.add_heading("2. Request signing with HMAC", 1)
    d.add_heading("2.1 Building the signature", 2)
    d.add_paragraph("Internal services sign each request with HMAC-SHA256 using their service secret. "
                    "The string to sign is the HTTP method, the request path, the Unix timestamp and the "
                    "SHA-256 hash of the body, joined with newline characters.")
    d.add_paragraph("The resulting signature is sent in the X-Signature header, and the timestamp in the "
                    "X-Timestamp header. Requests whose timestamp is more than 300 seconds old are rejected "
                    "to prevent replay attacks.")
    d.add_heading("2.2 Secret rotation", 2)
    d.add_paragraph("Service secrets are stored in the central vault and rotated every 90 days. During "
                    "rotation the API accepts both the old and the new secret for 24 hours.")
    d.add_heading("3. OAuth 2.0 for partners", 1)
    d.add_paragraph("Partners obtain an access token from the token endpoint using the client credentials "
                    "grant. Access tokens expire after 3600 seconds and must not be shared between partners.")
    d.add_heading("4. Rate limits", 1)
    d.add_paragraph("Limits depend on the client tier:")
    t = d.add_table(rows=1, cols=3)
    for cell, text in zip(t.rows[0].cells, ("Tier", "Requests per minute", "Burst")):
        cell.text = text
    for row in (("Internal", "6000", "1000"), ("Partner Standard", "600", "100"), ("Partner Premium", "2400", "400")):
        for cell, text in zip(t.add_row().cells, row):
            cell.text = text
    d.add_paragraph("Clients that exceed their limit receive HTTP 429 with a Retry-After header.")
    d.save(HERE / "api_authentication_guide.docx")


# ---------- Engineering: information security policy (multi-page PDF) ----------

POLICY_SECTIONS = [
    ("1. Purpose", "This policy sets the minimum security requirements for all Brightline Analytics systems "
     "and staff. It applies to employees, contractors and any service that processes company or customer data."),
    ("2. Access control", "Access is granted on the principle of least privilege. Managers review the access "
     "rights of their team every quarter. Accounts of people who leave the company are disabled within 24 hours."),
    ("3. Authentication", "Multi-factor authentication (MFA) is mandatory for all administrator accounts and for "
     "remote access. Passwords must be at least 14 characters long. Shared accounts are not permitted."),
    ("4. API security", "All APIs must authenticate every request. Internal services must use signed requests, "
     "and secrets must be rotated at least every 90 days. API keys must never be stored in source code."),
    ("5. Data classification", "Data is classified as Public, Internal, Confidential or Restricted. Customer data "
     "is Confidential by default. Restricted data must be encrypted at rest and in transit."),
    ("6. Logging and monitoring", "Security-relevant events, including logins, permission changes and data "
     "exports, must be logged. Logs are kept for at least 12 months and reviewed weekly by the security team."),
    ("7. Incident response", "Suspected security incidents must be reported to security@brightline.example "
     "within one hour of discovery. The security team classifies the incident and leads the response."),
    ("8. Exceptions", "Exceptions to this policy require written approval from the Chief Information Security "
     "Officer and are valid for a maximum of six months."),
]


def make_pdf():
    pages = [POLICY_SECTIONS[i:i + 3] for i in range(0, len(POLICY_SECTIONS), 3)]
    body = "".join(
        '<div class="page"><p class="hdr">Brightline Analytics · Internal</p>'
        + ("<h1>Information Security Policy</h1><p>Version 4.0 · Approved by the CISO</p>" if n == 0 else "")
        + "".join(f"<h2>{h}</h2><p>{t}</p>" for h, t in sec)
        + f'<p class="ftr">Page {n + 1} of {len(pages)}</p></div>'
        for n, sec in enumerate(pages))
    html = ("<html><head><meta charset='utf-8'><style>body{font-family:Arial;font-size:13pt}"
            ".page{page-break-after:always}.hdr,.ftr{color:#666;font-size:10pt}</style></head><body>"
            + body + "</body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "policy.html"
        src.write_text(html, encoding="utf-8")
        out = HERE / "information_security_policy.pdf"
        subprocess.run([str(EDGE), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", src.as_uri()], check=True, timeout=120)


# ---------- Finance: expense policy (plain text) ----------

EXPENSES = """BRIGHTLINE ANALYTICS - TRAVEL AND EXPENSE POLICY
Owner: Finance. Effective 1 January 2026.

1. Approval
All expense reports must be approved by the employee's line manager within 5 working days of submission. Expenses above 2,000 EUR also require approval from the Finance Director.

2. Travel
Economy class must be used for flights under 6 hours. Business class may be used for flights of 6 hours or longer with prior approval. Hotel costs are reimbursed up to 180 EUR per night in most cities and up to 260 EUR per night in London, New York and Tokyo.

3. Meals
Meals while travelling are reimbursed up to 60 EUR per day. Alcohol is not reimbursed.

4. Receipts
An itemised receipt is required for every expense above 25 EUR. Claims must be submitted within 30 days of the expense; later claims are only paid with Finance Director approval.

5. Corporate cards
Corporate cards may only be used for business expenses. Personal use must be repaid within 10 working days.
"""


if __name__ == "__main__":
    make_docx()
    (HERE / "expense_policy.txt").write_text(EXPENSES, encoding="utf-8")
    make_pdf()
    for f in sorted(HERE.iterdir()):
        if f.suffix in {".docx", ".pdf", ".txt"}:
            print(f"{f.name:<36} {f.stat().st_size:>8} bytes")
