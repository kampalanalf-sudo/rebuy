# Manuals

Two PDFs, generated from `scripts/make_manuals.py`:

| File | Who it is for |
|---|---|
| `Rebuy-Shop-Manual.pdf` | Shop owners, managers and staff. 10 sections, including the clinic/hospital flow. |
| `Rebuy-Admin-Manual.pdf` | Platform operators. 11 sections: cockpit, onboarding, credentials, queue, runbook, hard limits. |

Rebuild after changing the product:

```bash
python3 scripts/make_manuals.py
```

Needs `pip install reportlab`. Content is written against the real pages, routes
and limits in the code — when you add a screen or change a plan price, update the
generator rather than editing the PDF.
