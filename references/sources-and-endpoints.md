# Sources and Endpoints

## Multi-Source Coverage
Use all four public sources in each collection run to maximize recall and reduce source-specific blind spots.

- OpenAlex: broad scholarly graph with open-access hints and IDs.
- Crossref: DOI-centric publisher metadata and links.
- Semantic Scholar: enriched metadata and paper URL backfill.
- OpenReview: venue-native records for conferences with OpenReview workflows.

## Endpoint Patterns
Use these canonical API endpoints (already embedded in `scripts/collect_topconf_papers.py`).

| Source | Endpoint | Notes |
|---|---|---|
| OpenAlex | `https://api.openalex.org/works` | Supports filtering and paging; OA links may be present. |
| Crossref | `https://api.crossref.org/works` | DOI-rich metadata; some records have sparse venue text. |
| Semantic Scholar | `https://api.semanticscholar.org/graph/v1/paper/search` | Public endpoint may rate-limit; set conservative retries. |
| OpenReview | `https://api2.openreview.net/notes/search` (fallback `api.openreview.net`) | Useful for ICLR and other OpenReview-native venues. |

## Venue Mapping Hints
Normalize venue aliases before querying and post-filtering.

| Canonical venue | Typical aliases |
|---|---|
| `neurips` | NIPS, Neural Information Processing Systems |
| `icml` | International Conference on Machine Learning |
| `iclr` | International Conference on Learning Representations |
| `cvpr` | IEEE/CVF Conference on Computer Vision and Pattern Recognition |
| `acl` | Annual Meeting of the Association for Computational Linguistics |
| `emnlp` | Conference on Empirical Methods in Natural Language Processing |
| `sigcomm` | ACM SIGCOMM |
| `osdi` | USENIX OSDI |
| `nsdi` | USENIX NSDI |
| `ieee s&p` | IEEE Symposium on Security and Privacy, IEEE S&P, S&P, Oakland, Oakland Conference |
| `usenix security` | USENIX Security Symposium |
| `ccs` | ACM CCS |
| `ndss` | Network and Distributed System Security Symposium, ISOC NDSS |
| `kdd` | ACM SIGKDD, SIGKDD Conference on Knowledge Discovery and Data Mining |
| `chi` | ACM CHI |

## Command Snippets (`scripts/collect_topconf_papers.py`)
Inspect flags first:

```bash
python3 scripts/collect_topconf_papers.py --help
```

Metadata-first run:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "adversarial robustness" \
  --venues "IEEE S&P,USENIX Security,CCS,NDSS,KDD" \
  --years "2021-2025" \
  --max-per-source 100 \
  --out-dir ./runs/sec4-kdd-robustness
```

Conservative network settings for unstable APIs:

```bash
python3 scripts/collect_topconf_papers.py \
  --query "alignment" \
  --venues "ACL,EMNLP" \
  --years "2024,2025" \
  --max-per-source 80 \
  --timeout 25 \
  --retries 5 \
  --min-interval 1.5 \
  --out-dir ./runs/alignment-nlp
```

PDF + MinerU markdown pipeline (local API):

```bash
python3 scripts/collect_topconf_papers.py \
  --query "vision-language model" \
  --venues "CVPR,ICLR" \
  --years "2024,2025" \
  --max-per-source 60 \
  --download-pdf \
  --mineru-cmd /home/jinyilun/anaconda3/envs/pdf/bin/mineru \
  --mineru-backend vlm-http-client \
  --mineru-api-base http://127.0.0.1:8000 \
  --out-dir ./runs/vlm-mineru
```
