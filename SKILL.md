---
name: topconf-paper-collector
description: Use when collecting top-conference papers (NeurIPS/ICML/ICLR/CVPR/ACL/EMNLP/SIGCOMM/OSDI/NSDI/IEEE S&P/USENIX Security/CCS/NDSS/KDD/CHI) with high recall and reproducible outputs, especially when multi-step retrieval, multi-source discovery (OpenAlex/Crossref/Semantic Scholar/OpenReview), metadata deduplication, open-access PDF downloading, and mandatory local MinerU parsing to markdown files in a papers directory are required.
---

# TopConf Paper Collector

## Overview
Use this skill to collect top-conference papers with a recall-first workflow: query across multiple public sources, normalize and deduplicate records, download open-access PDFs, then parse each downloaded PDF with local MinerU into markdown. Keep every run reproducible and policy-safe.

## Quick Start
Run this from the skill directory (or use absolute paths):

```bash
python3 scripts/collect_topconf_papers.py --help
```

Basic metadata collection:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "graph anomaly detection" \
  --venues "IEEE S&P,USENIX Security,CCS,NDSS,KDD" \
  --years "2022-2025" \
  --max-per-source 80 \
  --out-dir ./runs/security-top4-kdd
```

With open-access PDF retrieval + local MinerU parsing:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "large language model" \
  --venues "NeurIPS,ICML,KDD,ACL,EMNLP" \
  --years "2024,2025" \
  --max-per-source 120 \
  --download-pdf \
  --mineru-cmd /home/jinyilun/anaconda3/envs/pdf/bin/mineru \
  --mineru-backend vlm-http-client \
  --mineru-api-base http://127.0.0.1:8000 \
  --min-pdf-bytes 20000 \
  --out-dir ./runs/llm-kdd-2024-2025
```

## Workflow (Multi-Step + Multi-Source + Multi-Method)
1. Define scope: query string, target venues, year filter, expected volume.
2. Run multi-source collection from OpenAlex, Crossref, Semantic Scholar, and OpenReview.
3. Normalize fields (title/authors/venue/year/IDs/URLs).
4. Deduplicate records by DOI/arXiv/title-based keys.
5. Download open-access PDF using multiple methods:
   - direct OA links from metadata
   - arXiv canonical PDF route
   - OpenReview forum-to-PDF route
   - DOI landing page PDF sniffing and common host rewrites
6. Parse each downloaded PDF with local MinerU and copy final markdown to a mandatory `papers/` directory.
7. Export machine-readable outputs and inspect failures.

## Outputs
Each run writes to `--out-dir`:

- `papers.jsonl`: canonical deduplicated records
- `papers.csv`: spreadsheet-friendly metadata
- `pdfs/`: downloaded PDFs (when `--download-pdf` is enabled)
- `papers/`: mandatory directory containing parsed paper markdown files (`*.md`)
- `failed_downloads.jsonl`: per-paper PDF failure diagnostics
- `failed_mineru_parses.jsonl`: per-paper MinerU parsing failure diagnostics

## Parameters You Will Use Most
- `--query`: semantic query for discovery (required)
- `--venues`: comma-separated venue hints (optional but recommended)
- `--years`: year list/ranges, e.g. `2024,2025` or `2022-2025`
- `--max-per-source`: cap per source before merge
- `--download-pdf`: enable open-access PDF retrieval + MinerU markdown parsing workflow
- `--min-pdf-bytes`: filter tiny non-PDF/invalid files
- `--mineru-cmd`: MinerU CLI path (defaults to `mineru`, with fallback probes)
- `--mineru-backend`: MinerU backend (default `vlm-http-client`)
- `--mineru-api-base`: local MinerU API base URL (for `*-http-client` backends)
- `--mineru-timeout`: per-paper MinerU timeout in seconds
- `--timeout`, `--retries`, `--min-interval`: network reliability and politeness controls

## Read These References When Needed
- For source coverage and endpoint caveats: `references/sources-and-endpoints.md`
- For retrieval and dedupe playbook: `references/retrieval-playbook.md`
- For compliance guardrails: `references/compliance-and-ethics.md`

## Common Mistakes
- Using vague `--query` and no venue/year constraints, causing noisy results.
- Assuming one source is complete; always keep multi-source retrieval on.
- Treating workshop/demo listings as main-track papers without checks.
- Running `--download-pdf` without a working local MinerU command/API.
- Downloading restricted content; this skill only permits public/open links.

## Non-Negotiable Rules
- Do not bypass paywalls, CAPTCHAs, auth walls, or robots restrictions.
- Do not fabricate metadata when fields are missing; keep nulls and provenance.
- Do not claim dataset completeness without checking run stats and sampled records.
- Do not skip markdown persistence: parsed markdown must be stored in `papers/`.
