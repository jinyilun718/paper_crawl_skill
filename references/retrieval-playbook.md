# Retrieval Playbook

## Objective
Collect high-recall, deduplicated, and auditable top-conference paper datasets with open-access PDFs and local MinerU markdown parsing.

## Multi-Step, Multi-Method Workflow
1. Define run scope: query, venues, years, and expected size.
2. Collect from all sources in one run (OpenAlex/Crossref/Semantic Scholar/OpenReview).
3. Normalize paper fields and IDs.
4. Deduplicate by key priority.
5. Run quality checks on output distribution.
6. Download PDFs using multiple URL generation/rewrite methods.
7. Parse each downloaded PDF with local MinerU and persist markdown into `papers/`.
8. Export artifacts and review failures.

## Dedupe Strategy
Apply this matching priority in order:

1. `doi`
2. source-native IDs (e.g., OpenReview note id)
3. `arxiv_id`
4. normalized title hash (fallback)

After merge, keep one canonical record and preserve:
- `sources`: every source that contributed to the final record
- `source_ids`: per-source identifiers used for traceability

## Quality Checks
Perform these checks after each run:

- Ensure core fields exist: `title`, `year`, at least one URL/ID.
- Compare counts by year and venue against expectation.
- Spot-check at least 20 records for obvious false positives.
- Review `failed_downloads.jsonl` and `failed_mineru_parses.jsonl`.
- Verify parsed markdown outputs exist in `papers/`.

## Command Snippets (`scripts/collect_topconf_papers.py`)
Collection-only run:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "privacy-preserving learning" \
  --venues "IEEE S&P,USENIX Security,CCS,NDSS,KDD" \
  --years "2023-2025" \
  --max-per-source 120 \
  --out-dir ./runs/sec4-kdd-privacy
```

Collection + PDF + MinerU parsing run:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "retrieval augmented generation" \
  --venues "NeurIPS,ICLR,KDD,ACL,EMNLP" \
  --years "2024,2025" \
  --max-per-source 120 \
  --download-pdf \
  --mineru-cmd /home/jinyilun/anaconda3/envs/pdf/bin/mineru \
  --mineru-backend vlm-http-client \
  --mineru-api-base http://127.0.0.1:8000 \
  --min-pdf-bytes 15000 \
  --out-dir ./runs/rag-kdd
```
