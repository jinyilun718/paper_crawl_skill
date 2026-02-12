# Compliance and Ethics

## Core Rules
- Use only public metadata APIs and public/open paper links.
- Respect robots.txt, API terms, and published rate limits.
- Keep attribution and provenance in every output record.
- Stop collection on explicit policy denial from any source.

## Allowed vs Disallowed

**Allowed**
- Public API requests with polite rate limiting.
- Downloading open-access PDFs from explicitly public links.
- Parsing downloaded PDFs with local MinerU and saving markdown under `papers/`.
- Recording missing fields as null/empty when unavailable.

**Disallowed**
- Paywall bypass or credential sharing.
- CAPTCHA circumvention or anti-bot evasion.
- Scraping restricted full text without permission.
- Fabricating metadata to “fill gaps.”

## Operational Guardrails
- Keep `--min-interval` conservative (e.g., `>= 1.0` second per host).
- Use retries/backoff (`--retries`) instead of high concurrency.
- Keep logs and artifacts for auditability.

## Provenance Requirements
Ensure each record includes:
- source lineage (`sources`)
- source identifiers (`source_ids`)
- canonical URL and/or persistent IDs (DOI/arXiv/OpenReview)

## Command Snippets (`scripts/collect_topconf_papers.py`)
Policy-first run:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "network security" \
  --venues "IEEE S&P,USENIX Security,CCS,NDSS,KDD" \
  --years "2022-2025" \
  --max-per-source 60 \
  --download-pdf \
  --mineru-cmd /home/jinyilun/anaconda3/envs/pdf/bin/mineru \
  --mineru-backend vlm-http-client \
  --mineru-api-base http://127.0.0.1:8000 \
  --timeout 25 \
  --retries 4 \
  --min-interval 1.5 \
  --out-dir ./runs/security-top4-kdd-policy
```
