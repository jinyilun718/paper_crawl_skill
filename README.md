# TopConf Paper Collector（`paper_crawl_skill`）

本仓库用于**高召回率抓取顶会论文元数据**，并可选执行：

1. 多源检索（OpenAlex / Crossref / Semantic Scholar / OpenReview）
2. 去重与标准化导出（`papers.jsonl` / `papers.csv`）
3. 开放访问 PDF 下载（仅公开链接）
4. 本地 MinerU 解析 PDF 到 Markdown（强制落盘到 `papers/`）

---

## 1) 项目概览

### 仓库结构（与你当前目录一致）

```text
/mnt/564a01af-e72c-4aa5-a69f-bd303b6d2a79/ai_project/fundmental/paper_crawl_skill
├── README.md
├── SKILL.md
├── scripts/
│   ├── collect_topconf_papers.py
│   └── package_release.sh
├── references/
│   ├── compliance-and-ethics.md
│   ├── retrieval-playbook.md
│   └── sources-and-endpoints.md
└── agents/
    └── openai.yaml
```

### 核心能力

- 检索来源：OpenAlex、Crossref、Semantic Scholar、OpenReview
- 目标会议（可组合）：NeurIPS / ICML / ICLR / CVPR / ACL / EMNLP / SIGCOMM / OSDI / NSDI / IEEE S&P / USENIX Security / CCS / NDSS / KDD / CHI
- 去重策略：DOI → 源 ID → arXiv ID → 归一化标题 hash
- PDF 流程：仅尝试公开可访问 PDF，不绕过付费/验证码
- 解析流程：调用本地 MinerU CLI，输出 Markdown 到 `papers/`

---

## 2) 前置条件（Prerequisites）

### 系统与工具

- Linux / macOS（推荐）
- `python3`（建议 3.10+；若与 MinerU 共环境，建议 3.11）
- `bash`
- 网络可访问公开学术 API

### 打包发布额外依赖（用于 `scripts/package_release.sh`）

- `tar`
- `zip`
- `sha256sum`
- `wc`
- `awk`

### MinerU 相关

- 已安装 MinerU CLI（`mineru`）
- 如果使用 `*-http-client` 后端，需要先启动本地 API 服务并保证 URL 可达

---

## 3) 完整部署流程（Python 环境、依赖、配置）

> 说明：`scripts/collect_topconf_papers.py` 本身只用到 Python 标准库；因此**采集器本体无额外 pip 依赖**。MinerU 属于可选链路（下载+解析 PDF 时必需）。

### 3.1 进入仓库

```bash
export REPO_ROOT=/mnt/564a01af-e72c-4aa5-a69f-bd303b6d2a79/ai_project/fundmental/paper_crawl_skill
cd "$REPO_ROOT"
```

### 3.2 创建并激活 Python 环境（采集器）

```bash
python3 -m venv .venv
source .venv/bin/activate
python -V
python -m pip install -U pip
```

### 3.3 基础配置（建议）

```bash
export RUNS_DIR="$REPO_ROOT/runs"
mkdir -p "$RUNS_DIR"

# MinerU 相关默认值（如未安装可先保留，metadata-only 模式仍可跑）
export MINERU_CMD="${MINERU_CMD:-mineru}"
export MINERU_API_BASE="${MINERU_API_BASE:-http://127.0.0.1:8000}"
```

### 3.4 验证采集器命令可用

```bash
python3 scripts/collect_topconf_papers.py --help
```

预期看到：

- `usage: collect_topconf_papers.py ...`
- `--mineru-backend {pipeline,vlm-http-client,hybrid-http-client,vlm-auto-engine,hybrid-auto-engine}`
- 默认 `--mineru-api-base http://127.0.0.1:8000`

---

## 4) 完整使用流程（命令 + 预期输出）

### 4.1 仅采集元数据（不下载 PDF）

```bash
cd "$REPO_ROOT"
source .venv/bin/activate

python3 scripts/collect_topconf_papers.py \
  --query "adversarial robustness" \
  --venues "IEEE S&P,USENIX Security,CCS,NDSS,KDD" \
  --years "2022-2025" \
  --max-per-source 80 \
  --out-dir "$RUNS_DIR/sec4-kdd-meta" \
  --timeout 20 \
  --retries 4 \
  --min-interval 1.0 \
  --log-level INFO
```

预期日志（示例）：

```text
INFO Collecting from OpenAlex ...
INFO OpenAlex returned <N> filtered papers
INFO Collecting from Crossref ...
INFO Semantic Scholar returned <N> filtered papers
INFO OpenReview returned <N> filtered papers
INFO Collected <RAW_N> raw papers before dedup
INFO Retained <DEDUP_N> papers after dedup
INFO Wrote metadata: .../papers.jsonl
INFO Wrote metadata: .../papers.csv
INFO Wrote markdown papers directory: .../papers
```

### 4.2 检查输出目录

```bash
ls -lah "$RUNS_DIR/sec4-kdd-meta"
head -n 2 "$RUNS_DIR/sec4-kdd-meta/papers.jsonl"
head -n 5 "$RUNS_DIR/sec4-kdd-meta/papers.csv"
```

预期产物：

- `papers.jsonl`
- `papers.csv`
- `papers/`（metadata-only 模式下通常为空目录）

### 4.3 启用 PDF 下载 + MinerU 解析

```bash
python3 scripts/collect_topconf_papers.py \
  --query "large language model" \
  --venues "NeurIPS,ICML,KDD,ACL,EMNLP" \
  --years "2024,2025" \
  --max-per-source 120 \
  --download-pdf \
  --min-pdf-bytes 15000 \
  --mineru-cmd "$MINERU_CMD" \
  --mineru-backend vlm-http-client \
  --mineru-api-base "$MINERU_API_BASE" \
  --mineru-timeout 900 \
  --out-dir "$RUNS_DIR/llm-e2e" \
  --timeout 25 \
  --retries 4 \
  --min-interval 1.0 \
  --log-level INFO
```

预期额外日志（示例）：

```text
INFO [1/<TOTAL>] Downloading PDF for: <TITLE>
INFO PDF download finished: success=<S> failed=<F>
INFO MinerU parse finished: success=<S2> failed=<F2>
INFO Wrote failures: .../failed_downloads.jsonl
INFO Wrote failures: .../failed_mineru_parses.jsonl
```

---

## 5) MinerU 安装 + 部署（完整）

下面给出两种安装方式（Conda / pip），并包含后端选择、本地 API 部署与验证命令。

### 5.1 安装方式 A：Conda（推荐）

```bash
conda create -n mineru python=3.11 -y
conda activate mineru
python -m pip install -U pip

# 全功能安装（推荐，含 VLM/Hybrid 相关能力）
pip install -U "mineru[all]"
```

快速验证：

```bash
command -v mineru
mineru --help | head -n 20
```

### 5.2 安装方式 B：pip（venv）

```bash
cd "$REPO_ROOT"
python3 -m venv .venv-mineru
source .venv-mineru/bin/activate
python -m pip install -U pip

# 方案 1：按需安装
pip install -U "mineru[core]"
pip install -U "mineru[modelscope]"

# 方案 2：直接全量
pip install -U "mineru[all]"
```

### 5.3 后端选择（与本仓库参数一致）

本采集器支持以下 `--mineru-backend`：

- `pipeline`
- `vlm-http-client`
- `hybrid-http-client`
- `vlm-auto-engine`
- `hybrid-auto-engine`

选择建议：

- 仅本地基础解析：先试 `pipeline`
- 想把推理交给本地 HTTP 服务：用 `vlm-http-client` 或 `hybrid-http-client`
- 自动选择引擎：`vlm-auto-engine` / `hybrid-auto-engine`

### 5.4 本地 API 端点部署

#### 选项 A：MinerU API 服务（与仓库默认 URL `http://127.0.0.1:8000` 对齐）

```bash
source .venv-mineru/bin/activate 2>/dev/null || conda activate mineru
mineru-api --host 127.0.0.1 --port 8000
```

新开一个终端验证：

```bash
curl -sS http://127.0.0.1:8000/docs | head -n 5
```

#### 选项 B：OpenAI 兼容服务（可用于 `*-http-client`）

```bash
source .venv-mineru/bin/activate 2>/dev/null || conda activate mineru
mineru-openai-server --host 127.0.0.1 --port 30000
```

验证：

```bash
curl -sS http://127.0.0.1:30000/v1/models
```

如果你用 30000 端口，采集命令应改为：

```bash
--mineru-api-base http://127.0.0.1:30000
```

### 5.5 MinerU 本地 Smoke Test（建议先跑）

```bash
# 准备一个本地 PDF 样例
cp "$RUNS_DIR/llm-e2e/pdfs"/*.pdf /tmp/mineru_sample.pdf 2>/dev/null || true

mineru \
  --path /tmp/mineru_sample.pdf \
  --output /tmp/mineru_smoke \
  --backend pipeline

find /tmp/mineru_smoke -name '*.md' | head -n 3
```

---

## 6) 端到端示例（采集 + PDF 下载 + MinerU 解析）

### 6.1 启动 MinerU 服务（终端 A）

```bash
source .venv-mineru/bin/activate 2>/dev/null || conda activate mineru
mineru-api --host 127.0.0.1 --port 8000
```

### 6.2 执行采集器（终端 B）

```bash
export REPO_ROOT=/mnt/564a01af-e72c-4aa5-a69f-bd303b6d2a79/ai_project/fundmental/paper_crawl_skill
cd "$REPO_ROOT"
source .venv/bin/activate

RUN_DIR="$REPO_ROOT/runs/e2e-$(date +%Y%m%d-%H%M%S)"

python3 scripts/collect_topconf_papers.py \
  --query "retrieval augmented generation" \
  --venues "NeurIPS,ICLR,KDD,ACL,EMNLP" \
  --years "2024,2025" \
  --max-per-source 120 \
  --download-pdf \
  --min-pdf-bytes 15000 \
  --mineru-cmd mineru \
  --mineru-backend vlm-http-client \
  --mineru-api-base http://127.0.0.1:8000 \
  --mineru-timeout 900 \
  --timeout 25 \
  --retries 4 \
  --min-interval 1.0 \
  --out-dir "$RUN_DIR" \
  --log-level INFO
```

### 6.3 验证结果

```bash
echo "RUN_DIR=$RUN_DIR"
ls -lah "$RUN_DIR"

wc -l "$RUN_DIR/papers.jsonl"
find "$RUN_DIR/pdfs" -name '*.pdf' | wc -l
find "$RUN_DIR/papers" -name '*.md' | wc -l

test -f "$RUN_DIR/failed_downloads.jsonl" && wc -l "$RUN_DIR/failed_downloads.jsonl" || echo "failed_downloads.jsonl: 0"
test -f "$RUN_DIR/failed_mineru_parses.jsonl" && wc -l "$RUN_DIR/failed_mineru_parses.jsonl" || echo "failed_mineru_parses.jsonl: 0"

head -n 1 "$RUN_DIR/papers.jsonl"
find "$RUN_DIR/papers" -name '*.md' | head -n 3
```

成功标准：

- `papers.jsonl` / `papers.csv` 存在且非空
- `pdfs/` 下有下载结果（数量允许小于总数）
- `papers/` 下出现 Markdown 文件
- 失败清单文件可用于复盘

---

## 7) 故障排查清单（Troubleshooting Checklist）

### 参数与输入

- [ ] 报错 `Invalid arguments: years out of range`：检查 `--years` 是否在有效区间（例如 `2024,2025` 或 `2022-2025`）
- [ ] 报错 `--timeout must be > 0` / `--retries must be > 0`：修正为正数
- [ ] 结果过少：放宽 `--venues` 或扩大 `--years`，并提高 `--max-per-source`

### 网络与数据源

- [ ] 出现 429/5xx：增大 `--min-interval`（如 `1.5`），并适当提高 `--retries`
- [ ] 某一来源失败：脚本会 warning 并继续其他来源；检查网络策略后重试
- [ ] 长时间无输出：先用 metadata-only 模式验证，再开启 `--download-pdf`

### MinerU 链路

- [ ] 报错 `MinerU command not found`：确认 `command -v mineru` 或显式传 `--mineru-cmd /绝对路径/mineru`
- [ ] `mineru_nonzero_exit`：先独立执行 `mineru --path <pdf> --output <dir> --backend <backend>` 查看 CLI 错误
- [ ] `mineru_timeout`：调大 `--mineru-timeout`（例如 1200）
- [ ] `mineru_no_markdown`：尝试切换后端（`pipeline` ↔ `hybrid-http-client`）并检查 API 可用性
- [ ] `*-http-client` 后端失败：确认 `--mineru-api-base` 与服务端口一致（8000 或 30000）

### 输出与审计

- [ ] 检查 `failed_downloads.jsonl` 与 `failed_mineru_parses.jsonl`
- [ ] 抽样核查 `papers.jsonl` 的 `sources`、`source_ids`、`doi/arxiv_id`
- [ ] 复现时保留同一命令行与日志，便于审计

---

## 8) 打包与发布（`scripts/package_release.sh`）

该脚本会在 `dist/` 下生成：

- `.tar.gz`
- `.zip`
- 对应 `.sha256` 校验文件

并自动排除 `.git`、`dist`、缓存目录等。

### 8.1 默认版本号（推荐）

```bash
cd "$REPO_ROOT"
bash scripts/package_release.sh
```

默认版本规则：

- 有 git 仓库：`YYYYMMDD-<short_commit>`
- 无 git：`YYYYMMDD-HHMMSS`

### 8.2 指定版本号

```bash
cd "$REPO_ROOT"
bash scripts/package_release.sh 20260212-r1
```

预期输出（示例）：

```text
Release package complete
Version: 20260212-r1
Output directory: .../dist
 - paper_crawl_skill-20260212-r1.tar.gz (...) sha256=...
 - paper_crawl_skill-20260212-r1.zip (...) sha256=...
 - paper_crawl_skill-20260212-r1.tar.gz.sha256
 - paper_crawl_skill-20260212-r1.zip.sha256
```

---

## 9) 安全与合规说明（Security / Compliance）

请严格遵守 `references/compliance-and-ethics.md`：

1. **仅使用公开 API 与公开论文链接**，不得绕过付费墙、验证码、认证墙。
2. **遵守 robots.txt、API ToS、限速策略**，建议 `--min-interval >= 1.0`。
3. **保留溯源字段**：`sources`、`source_ids`、`doi`/`arxiv_id`/URL。
4. **缺失字段保持空值**，不得伪造元数据。
5. **保留失败日志与产物**，保证可审计与可复现。
6. **仅处理开放访问 PDF**，遇到策略拒绝应停止对应来源采集。

---

## 附：常用一键命令

### A. Metadata-only 快速跑

```bash
cd /mnt/564a01af-e72c-4aa5-a69f-bd303b6d2a79/ai_project/fundmental/paper_crawl_skill
source .venv/bin/activate
python3 scripts/collect_topconf_papers.py --query "graph anomaly detection" --venues "IEEE S&P,USENIX Security,CCS,NDSS,KDD" --years "2022-2025" --max-per-source 80 --out-dir ./runs/quick-meta
```

### B. PDF + MinerU 快速跑

```bash
cd /mnt/564a01af-e72c-4aa5-a69f-bd303b6d2a79/ai_project/fundmental/paper_crawl_skill
source .venv/bin/activate
python3 scripts/collect_topconf_papers.py --query "large language model" --venues "NeurIPS,ICML,KDD,ACL,EMNLP" --years "2024,2025" --max-per-source 120 --download-pdf --min-pdf-bytes 20000 --mineru-cmd mineru --mineru-backend vlm-http-client --mineru-api-base http://127.0.0.1:8000 --out-dir ./runs/quick-e2e
```

