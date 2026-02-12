#!/usr/bin/env python3
"""Collect top-conference papers, download open PDFs, and parse them with MinerU."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib import error, parse, request


OPENALEX_URL = "https://api.openalex.org/works"
CROSSREF_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENREVIEW_ENDPOINTS = (
    "https://api2.openreview.net/notes/search",
    "https://api.openreview.net/notes/search",
)

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

MINERU_BACKEND_CHOICES = (
    "pipeline",
    "vlm-http-client",
    "hybrid-http-client",
    "vlm-auto-engine",
    "hybrid-auto-engine",
)

MINERU_DEFAULT_CMD_CANDIDATES = (
    "mineru",
    "/home/jinyilun/anaconda3/envs/pdf/bin/mineru",
)


class CollectError(RuntimeError):
    """Raised when a source response cannot be parsed or used."""


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Compose default-value help with multi-line examples."""


class HttpResponse:
    """Simple HTTP response container."""

    __slots__ = ("status", "url", "headers", "body")

    def __init__(self, status: int, url: str, headers: Dict[str, str], body: bytes):
        self.status = status
        self.url = url
        self.headers = headers
        self.body = body


class PoliteHttpClient:
    """HTTP helper with per-host throttling, retries, and timeout."""

    def __init__(
        self,
        *,
        timeout: float,
        retries: int,
        min_interval: float,
        user_agent: str,
    ):
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.user_agent = user_agent
        self._last_by_host: Dict[str, float] = {}

    def _throttle(self, url: str) -> None:
        host = parse.urlparse(url).netloc.lower()
        now = time.monotonic()
        previous = self._last_by_host.get(host)
        if previous is not None:
            wait_for = self.min_interval - (now - previous)
            if wait_for > 0:
                time.sleep(wait_for)
        self._last_by_host[host] = time.monotonic()

    def _retry_sleep(self, attempt: int, response_headers: Optional[Dict[str, str]]) -> float:
        if response_headers:
            retry_after = response_headers.get("Retry-After")
            if retry_after:
                try:
                    retry_seconds = float(retry_after)
                    return max(self.min_interval, retry_seconds)
                except ValueError:
                    pass
        backoff = 0.9 * (2 ** (attempt - 1))
        jitter = random.uniform(0, 0.35)
        return max(self.min_interval, backoff + jitter)

    def _build_url(self, base_url: str, params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return base_url
        pairs: List[Tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    pairs.append((key, str(item)))
            else:
                pairs.append((key, str(value)))
        query_text = parse.urlencode(pairs, doseq=True)
        if not query_text:
            return base_url
        glue = "&" if "?" in base_url else "?"
        return f"{base_url}{glue}{query_text}"

    def request(
        self,
        base_url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResponse:
        url = self._build_url(base_url, params)
        merged_headers = {"User-Agent": self.user_agent}
        if headers:
            merged_headers.update(headers)

        for attempt in range(1, self.retries + 1):
            self._throttle(url)
            req = request.Request(url, headers=merged_headers)
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    response_headers = {k: v for k, v in resp.headers.items()}
                    return HttpResponse(
                        status=int(resp.getcode() or 200),
                        url=resp.geturl(),
                        headers=response_headers,
                        body=body,
                    )
            except error.HTTPError as exc:
                status_code = int(exc.code)
                response_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
                if status_code in RETRYABLE_STATUS_CODES and attempt < self.retries:
                    sleep_for = self._retry_sleep(attempt, response_headers)
                    logging.warning(
                        "HTTP %s for %s (attempt %d/%d), retrying in %.2fs",
                        status_code,
                        url,
                        attempt,
                        self.retries,
                        sleep_for,
                    )
                    time.sleep(sleep_for)
                    continue
                raise
            except (error.URLError, socket.timeout, TimeoutError) as exc:
                if attempt < self.retries:
                    sleep_for = self._retry_sleep(attempt, None)
                    logging.warning(
                        "Network error for %s (%s, attempt %d/%d), retrying in %.2fs",
                        url,
                        exc,
                        attempt,
                        self.retries,
                        sleep_for,
                    )
                    time.sleep(sleep_for)
                    continue
                raise

        raise CollectError(f"Request retries exhausted: {url}")

    def get_json(
        self,
        base_url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        merged_headers = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        response = self.request(base_url, params=params, headers=merged_headers)
        payload_text = response.body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise CollectError(f"Invalid JSON from {base_url}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CollectError(f"Unexpected JSON root type from {base_url}: {type(parsed)!r}")
        return parsed


def parse_csv_list(text: str) -> List[str]:
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_years(text: str) -> Optional[Set[int]]:
    if not text:
        return None
    years: Set[int] = set()
    for block in parse_csv_list(text):
        if "-" in block:
            begin_text, end_text = block.split("-", 1)
            begin_year = int(begin_text)
            end_year = int(end_text)
            if begin_year > end_year:
                raise ValueError(f"invalid year range: {block}")
            for year in range(begin_year, end_year + 1):
                years.add(year)
        else:
            years.add(int(block))

    invalid = sorted(year for year in years if year < 1900 or year > 2100)
    if invalid:
        raise ValueError(f"years out of range: {invalid}")
    return years


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clipped_text(text: Optional[str], limit: int = 2000) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def decode_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def normalize_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", unescape(text or ""))
    normalized = normalized.encode("ascii", "ignore").decode("ascii", errors="ignore")
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return normalize_whitespace(normalized)


def normalize_venue(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", unescape(text or ""))
    normalized = normalized.encode("ascii", "ignore").decode("ascii", errors="ignore")
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return normalize_whitespace(normalized)


CANONICAL_VENUE_MARKER_PREFIX = "__canonical_venue__:"

CANONICAL_VENUE_ALIAS_GROUPS: Dict[str, Tuple[str, ...]] = {
    "ieee-sp": (
        "ieee s p",
        "ieee sp",
        "s p",
        "sp",
        "oakland",
        "ieee security and privacy",
        "ieee symposium on security and privacy",
        "symposium on security and privacy",
    ),
    "usenix-security": (
        "usenix security",
        "usenix security symposium",
        "usenix sec",
    ),
    "acm-ccs": (
        "ccs",
        "acm ccs",
        "acm conference on computer and communications security",
        "conference on computer and communications security",
        "computer and communications security",
    ),
    "ndss": (
        "ndss",
        "ndss symposium",
        "network and distributed system security symposium",
        "network and distributed system security",
    ),
    "kdd": (
        "kdd",
        "acm kdd",
        "sigkdd",
        "sigkdd conference on knowledge discovery and data mining",
        "conference on knowledge discovery and data mining",
        "knowledge discovery and data mining",
    ),
}

CANONICAL_VENUE_STRONG_TERMS: Dict[str, Tuple[str, ...]] = {
    "ieee-sp": (
        "ieee symposium on security and privacy",
        "ieee security and privacy",
        "symposium on security and privacy",
        "oakland",
    ),
    "usenix-security": (
        "usenix security symposium",
        "usenix security",
    ),
    "acm-ccs": (
        "acm conference on computer and communications security",
        "conference on computer and communications security",
        "computer and communications security",
        "acm ccs",
    ),
    "ndss": (
        "network and distributed system security symposium",
        "network and distributed system security",
        "ndss",
    ),
    "kdd": (
        "acm sigkdd conference on knowledge discovery and data mining",
        "conference on knowledge discovery and data mining",
        "knowledge discovery and data mining",
        "sigkdd",
        "kdd",
    ),
}

CANONICAL_VENUE_TOKEN_RULES: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "ieee-sp": (
        ("oakland",),
        ("ieee", "security", "privacy"),
        ("symposium", "security", "privacy"),
        ("ieee", "s", "p"),
        ("ieee", "sp"),
    ),
    "usenix-security": (
        ("usenix", "security"),
        ("usenix", "sec"),
    ),
    "acm-ccs": (
        ("acm", "ccs"),
        ("ccs",),
        ("computer", "communications", "security"),
    ),
    "ndss": (
        ("ndss",),
        ("network", "distributed", "system", "security"),
    ),
    "kdd": (
        ("kdd",),
        ("sigkdd",),
        ("knowledge", "discovery", "data", "mining"),
    ),
}


def _build_canonical_venue_alias_map() -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for canonical_name, aliases in CANONICAL_VENUE_ALIAS_GROUPS.items():
        for alias in aliases:
            normalized = normalize_venue(alias)
            if normalized:
                alias_map[normalized] = canonical_name
    return alias_map


CANONICAL_VENUE_ALIAS_MAP = _build_canonical_venue_alias_map()


def canonical_venue_marker(canonical_name: str) -> str:
    return f"{CANONICAL_VENUE_MARKER_PREFIX}{canonical_name}"


def parse_canonical_venue_marker(term: str) -> Optional[str]:
    if term.startswith(CANONICAL_VENUE_MARKER_PREFIX):
        return term[len(CANONICAL_VENUE_MARKER_PREFIX) :]
    return None


def expand_canonical_venue_alias(term: str) -> List[str]:
    canonical_name = CANONICAL_VENUE_ALIAS_MAP.get(term)
    if not canonical_name:
        return [term]

    expanded: List[str] = [canonical_venue_marker(canonical_name)]
    for strong_term in CANONICAL_VENUE_STRONG_TERMS.get(canonical_name, ()):
        if strong_term not in expanded:
            expanded.append(strong_term)
    return expanded


def normalize_requested_venue_terms(raw_terms: Sequence[str]) -> List[str]:
    output: List[str] = []
    seen: Set[str] = set()
    for raw_term in raw_terms:
        normalized = normalize_venue(raw_term)
        if not normalized:
            continue
        for expanded_term in expand_canonical_venue_alias(normalized):
            if expanded_term in seen:
                continue
            seen.add(expanded_term)
            output.append(expanded_term)
    return output


def venue_matches_canonical_alias(venue_tokens: Set[str], canonical_name: str) -> bool:
    token_rules = CANONICAL_VENUE_TOKEN_RULES.get(canonical_name, ())
    for token_group in token_rules:
        if all(token in venue_tokens for token in token_group):
            return True
    return False


def venue_term_matches(venue: str, venue_tokens: Set[str], term: str) -> bool:
    canonical_name = parse_canonical_venue_marker(term)
    if canonical_name is not None:
        return venue_matches_canonical_alias(venue_tokens, canonical_name)
    return term in venue or venue in term


DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)


def normalize_doi(raw_doi: Optional[str]) -> Optional[str]:
    if not raw_doi:
        return None
    doi = raw_doi.strip()
    doi = DOI_PREFIX_RE.sub("", doi)
    if doi.lower().startswith("doi:"):
        doi = doi[4:]
    doi = doi.strip().lower()
    if not doi or not doi.startswith("10."):
        return None
    return doi


ARXIV_PATTERNS = (
    re.compile(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", re.IGNORECASE),
    re.compile(r"\barxiv:\s*([^\s]+)", re.IGNORECASE),
    re.compile(r"\b([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)\b", re.IGNORECASE),
    re.compile(r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)\b", re.IGNORECASE),
)


def normalize_arxiv_id(raw_arxiv: Optional[str]) -> Optional[str]:
    if not raw_arxiv:
        return None
    candidate = raw_arxiv.strip()
    candidate = parse.unquote(candidate)
    candidate = candidate.replace(".pdf", "")
    candidate = candidate.split("?")[0]
    candidate = candidate.split("#")[0]
    candidate = candidate.strip()
    if not candidate:
        return None

    matched = None
    for pattern in ARXIV_PATTERNS:
        found = pattern.search(candidate)
        if found:
            matched = found.group(1)
            break

    if matched is None:
        matched = candidate

    matched = matched.strip().lower()
    matched = re.sub(r"v\d+$", "", matched)
    if not matched:
        return None
    return matched


def extract_arxiv_id(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return normalize_arxiv_id(text)


def coerce_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if 1900 <= year <= 2100:
        return year
    return None


def clean_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    parsed = parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return None
    return cleaned


def is_probably_pdf_url(url: str) -> bool:
    lowered = url.lower()
    if lowered.endswith(".pdf"):
        return True
    if ".pdf?" in lowered:
        return True
    if "download=1" in lowered and "pdf" in lowered:
        return True
    return False


def strip_html_tags(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return normalize_whitespace(unescape(cleaned))


def normalize_authors(authors: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    output: List[str] = []
    for author in authors:
        normalized = normalize_whitespace(str(author))
        if not normalized:
            continue
        lowered = normalized.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(normalized)
    return output


def append_unique(items: List[str], values: Iterable[str]) -> None:
    seen = set(items)
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        items.append(value)
        seen.add(value)


def paper_passes_filters(
    paper: Dict[str, Any],
    *,
    venue_terms: Sequence[str],
    years: Optional[Set[int]],
) -> bool:
    if years is not None:
        paper_year = coerce_year(paper.get("year"))
        if paper_year is None or paper_year not in years:
            return False

    if venue_terms:
        venue = normalize_venue(str(paper.get("venue") or ""))
        if not venue:
            return False
        venue_tokens = set(venue.split())
        matched = False
        for term in venue_terms:
            if venue_term_matches(venue, venue_tokens, term):
                matched = True
                break
        if not matched:
            return False

    return True


def parse_openalex_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = item.get("display_name") or item.get("title")
    if not title:
        return None

    authorships = item.get("authorships") or []
    authors: List[str] = []
    for entry in authorships:
        author_name = ((entry or {}).get("author") or {}).get("display_name")
        if author_name:
            authors.append(str(author_name))

    primary_location = item.get("primary_location") or {}
    primary_source = primary_location.get("source") or {}

    venue = primary_source.get("display_name") or ""
    year = coerce_year(item.get("publication_year"))
    doi = normalize_doi(item.get("doi"))

    ids = item.get("ids") or {}
    arxiv_id = normalize_arxiv_id(ids.get("arxiv"))

    landing_url = clean_url(primary_location.get("landing_page_url"))
    if arxiv_id is None:
        arxiv_id = extract_arxiv_id(landing_url)

    pdf_urls: List[str] = []
    for candidate in (
        (item.get("open_access") or {}).get("oa_url"),
        primary_location.get("pdf_url"),
    ):
        cleaned = clean_url(candidate)
        if cleaned:
            pdf_urls.append(cleaned)

    for location in item.get("locations") or []:
        cleaned = clean_url((location or {}).get("pdf_url"))
        if cleaned:
            pdf_urls.append(cleaned)

    return {
        "title": str(title),
        "year": year,
        "venue": str(venue),
        "authors": normalize_authors(authors),
        "abstract": strip_html_tags(item.get("abstract")),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "url": clean_url(landing_url or item.get("id")),
        "pdf_urls": pdf_urls,
        "source": "openalex",
        "source_id": item.get("id"),
    }


def parse_crossref_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title_list = item.get("title") or []
    title = title_list[0] if title_list else None
    if not title:
        return None

    authors: List[str] = []
    for author in item.get("author") or []:
        given = (author or {}).get("given") or ""
        family = (author or {}).get("family") or ""
        full_name = normalize_whitespace(f"{given} {family}")
        if full_name:
            authors.append(full_name)

    year = None
    for date_field in ("published-print", "published-online", "issued", "created"):
        date_parts = ((item.get(date_field) or {}).get("date-parts") or [])
        if date_parts and date_parts[0]:
            year = coerce_year(date_parts[0][0])
            if year is not None:
                break

    venue_list = item.get("container-title") or []
    venue = venue_list[0] if venue_list else ""

    pdf_urls: List[str] = []
    for link in item.get("link") or []:
        link_url = clean_url((link or {}).get("URL"))
        if not link_url:
            continue
        content_type = str((link or {}).get("content-type") or "").lower()
        if "pdf" in content_type or is_probably_pdf_url(link_url):
            pdf_urls.append(link_url)

    arxiv_id = None
    for alternative in item.get("alternative-id") or []:
        arxiv_id = extract_arxiv_id(str(alternative))
        if arxiv_id:
            break

    return {
        "title": str(title),
        "year": year,
        "venue": str(venue),
        "authors": normalize_authors(authors),
        "abstract": strip_html_tags(item.get("abstract")),
        "doi": normalize_doi(item.get("DOI")),
        "arxiv_id": arxiv_id,
        "url": clean_url(item.get("URL")),
        "pdf_urls": pdf_urls,
        "source": "crossref",
        "source_id": item.get("DOI"),
    }


def parse_semantic_scholar_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = item.get("title")
    if not title:
        return None

    external_ids = item.get("externalIds") or {}
    venue = item.get("venue") or ((item.get("publicationVenue") or {}).get("name")) or ""

    authors = [str(author.get("name")) for author in (item.get("authors") or []) if author.get("name")]

    pdf_urls: List[str] = []
    open_access_pdf = item.get("openAccessPdf") or {}
    oa_url = clean_url(open_access_pdf.get("url"))
    if oa_url:
        pdf_urls.append(oa_url)

    url = clean_url(item.get("url"))
    arxiv_id = normalize_arxiv_id(external_ids.get("ArXiv")) or extract_arxiv_id(url)

    return {
        "title": str(title),
        "year": coerce_year(item.get("year")),
        "venue": str(venue),
        "authors": normalize_authors(authors),
        "abstract": strip_html_tags(item.get("abstract")),
        "doi": normalize_doi(external_ids.get("DOI")),
        "arxiv_id": arxiv_id,
        "url": url,
        "pdf_urls": pdf_urls,
        "source": "semantic_scholar",
        "source_id": item.get("paperId"),
    }


def openreview_content_value(content: Dict[str, Any], key: str) -> Any:
    value = content.get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def parse_openreview_item(item: Dict[str, Any], endpoint: str) -> Optional[Dict[str, Any]]:
    content = item.get("content") or {}
    title = openreview_content_value(content, "title")
    if isinstance(title, list):
        title = title[0] if title else None
    if not title:
        return None

    authors_value = openreview_content_value(content, "authors")
    authors = [str(author) for author in (authors_value or []) if author]

    venue_value = openreview_content_value(content, "venue") or ""

    year_value = openreview_content_value(content, "year")
    year = coerce_year(year_value)
    if year is None:
        cdate = item.get("cdate")
        if isinstance(cdate, (int, float)) and cdate > 0:
            year = datetime.utcfromtimestamp(cdate / 1000.0).year

    abstract = openreview_content_value(content, "abstract")
    doi = normalize_doi(openreview_content_value(content, "doi"))

    arxiv_raw = openreview_content_value(content, "arxiv")
    arxiv_id = normalize_arxiv_id(arxiv_raw)

    note_id = str(item.get("id") or "").strip()
    forum_url = f"https://openreview.net/forum?id={note_id}" if note_id else None

    pdf_urls: List[str] = []
    explicit_pdf = clean_url(item.get("pdf"))
    if explicit_pdf:
        pdf_urls.append(explicit_pdf)
    elif note_id:
        pdf_urls.append(f"https://openreview.net/pdf?id={note_id}")

    return {
        "title": str(title),
        "year": year,
        "venue": str(venue_value),
        "authors": normalize_authors(authors),
        "abstract": strip_html_tags(str(abstract) if abstract is not None else None),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "url": clean_url(forum_url),
        "pdf_urls": pdf_urls,
        "source": "openreview",
        "source_id": note_id or endpoint,
    }


def fetch_openalex(
    client: PoliteHttpClient,
    *,
    query: str,
    max_per_source: int,
    venue_terms: Sequence[str],
    years: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    year_batches = sorted(years) if years else [None]

    for year in year_batches:
        page = 1
        while len(papers) < max_per_source:
            remaining = max_per_source - len(papers)
            per_page = min(200, remaining)
            params: Dict[str, Any] = {"search": query, "per-page": per_page, "page": page}
            filters: List[str] = []
            if year is not None:
                filters.append(f"from_publication_date:{year}-01-01")
                filters.append(f"to_publication_date:{year}-12-31")
            if filters:
                params["filter"] = ",".join(filters)

            payload = client.get_json(OPENALEX_URL, params=params)
            results = payload.get("results") or []
            if not isinstance(results, list) or not results:
                break

            for item in results:
                if not isinstance(item, dict):
                    continue
                paper = parse_openalex_item(item)
                if not paper:
                    continue
                if not paper_passes_filters(paper, venue_terms=venue_terms, years=years):
                    continue
                papers.append(paper)
                if len(papers) >= max_per_source:
                    break

            if len(results) < per_page:
                break
            page += 1

        if len(papers) >= max_per_source:
            break

    return papers


def fetch_crossref(
    client: PoliteHttpClient,
    *,
    query: str,
    max_per_source: int,
    venue_terms: Sequence[str],
    years: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    year_batches = sorted(years) if years else [None]
    mailto = os.getenv("CROSSREF_MAILTO", "")

    for year in year_batches:
        offset = 0
        while len(papers) < max_per_source:
            remaining = max_per_source - len(papers)
            rows = min(100, remaining)
            params: Dict[str, Any] = {
                "query.bibliographic": query,
                "rows": rows,
                "offset": offset,
            }
            filters: List[str] = []
            if year is not None:
                filters.append(f"from-pub-date:{year}-01-01")
                filters.append(f"until-pub-date:{year}-12-31")
            if filters:
                params["filter"] = ",".join(filters)
            if mailto:
                params["mailto"] = mailto

            payload = client.get_json(CROSSREF_URL, params=params)
            message = payload.get("message") or {}
            items = message.get("items") or []
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                paper = parse_crossref_item(item)
                if not paper:
                    continue
                if not paper_passes_filters(paper, venue_terms=venue_terms, years=years):
                    continue
                papers.append(paper)
                if len(papers) >= max_per_source:
                    break

            if len(items) < rows:
                break
            offset += rows

        if len(papers) >= max_per_source:
            break

    return papers


def fetch_semantic_scholar(
    client: PoliteHttpClient,
    *,
    query: str,
    max_per_source: int,
    venue_terms: Sequence[str],
    years: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    headers: Dict[str, str] = {}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key

    offset = 0
    while len(papers) < max_per_source:
        remaining = max_per_source - len(papers)
        limit = min(100, remaining)
        params = {
            "query": query,
            "offset": offset,
            "limit": limit,
            "fields": "paperId,title,abstract,year,venue,publicationVenue,externalIds,url,openAccessPdf,authors",
        }
        payload = client.get_json(SEMANTIC_SCHOLAR_URL, params=params, headers=headers)
        data = payload.get("data") or []
        if not isinstance(data, list) or not data:
            break

        for item in data:
            if not isinstance(item, dict):
                continue
            paper = parse_semantic_scholar_item(item)
            if not paper:
                continue
            if not paper_passes_filters(paper, venue_terms=venue_terms, years=years):
                continue
            papers.append(paper)
            if len(papers) >= max_per_source:
                break

        if len(data) < limit:
            break
        offset += limit

    return papers


def fetch_openreview(
    client: PoliteHttpClient,
    *,
    query: str,
    max_per_source: int,
    venue_terms: Sequence[str],
    years: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    for endpoint in OPENREVIEW_ENDPOINTS:
        papers: List[Dict[str, Any]] = []
        offset = 0
        endpoint_ok = False
        while len(papers) < max_per_source:
            remaining = max_per_source - len(papers)
            limit = min(100, remaining)
            payload = None
            for param_key in ("query", "term"):
                params = {param_key: query, "limit": limit, "offset": offset}
                try:
                    payload = client.get_json(endpoint, params=params)
                    endpoint_ok = True
                    break
                except error.HTTPError as exc:
                    if exc.code in {400, 404}:
                        continue
                    raise
            if payload is None:
                break

            notes = payload.get("notes") or []
            if not isinstance(notes, list) or not notes:
                break

            for item in notes:
                if not isinstance(item, dict):
                    continue
                paper = parse_openreview_item(item, endpoint)
                if not paper:
                    continue
                if not paper_passes_filters(paper, venue_terms=venue_terms, years=years):
                    continue
                papers.append(paper)
                if len(papers) >= max_per_source:
                    break

            if len(notes) < limit:
                break
            offset += limit

        if endpoint_ok:
            return papers

    return []


def canonicalize_raw_paper(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = normalize_whitespace(str(raw.get("title") or ""))
    if not title:
        return None

    doi = normalize_doi(raw.get("doi"))
    arxiv_id = normalize_arxiv_id(raw.get("arxiv_id"))
    if arxiv_id is None:
        arxiv_id = extract_arxiv_id(str(raw.get("url") or ""))

    url = clean_url(raw.get("url"))
    source = str(raw.get("source") or "unknown")
    source_id = raw.get("source_id")

    pdf_urls: List[str] = []
    for candidate in raw.get("pdf_urls") or []:
        cleaned = clean_url(candidate)
        if cleaned:
            pdf_urls.append(cleaned)
    if url and is_probably_pdf_url(url):
        pdf_urls.append(url)

    abstract = strip_html_tags(raw.get("abstract"))
    venue = normalize_whitespace(str(raw.get("venue") or ""))
    authors = normalize_authors(raw.get("authors") or [])

    canonical: Dict[str, Any] = {
        "paper_id": "",
        "title": title,
        "title_norm": normalize_title(title),
        "year": coerce_year(raw.get("year")),
        "venue": venue,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "url": url,
        "pdf_urls": [],
        "sources": [source],
        "source_ids": {},
    }
    append_unique(canonical["pdf_urls"], pdf_urls)
    if source_id:
        canonical["source_ids"][source] = str(source_id)
    return canonical


def dedup_keys(paper: Dict[str, Any]) -> List[Tuple[str, str]]:
    keys: List[Tuple[str, str]] = []
    if paper.get("doi"):
        keys.append(("doi", str(paper["doi"])))
    if paper.get("arxiv_id"):
        keys.append(("arxiv", str(paper["arxiv_id"])))
    if paper.get("title_norm"):
        keys.append(("title", str(paper["title_norm"])))
    if not keys:
        fallback = hashlib.sha1(
            f"{paper.get('title','')}|{paper.get('url','')}|{paper.get('sources','')}".encode(
                "utf-8",
                errors="ignore",
            )
        ).hexdigest()
        keys.append(("fallback", fallback))
    return keys


def better_text(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if not candidate:
        return current
    if not current:
        return candidate
    return candidate if len(candidate) > len(current) else current


def merge_papers(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    target["title"] = better_text(target.get("title"), incoming.get("title")) or ""
    target["title_norm"] = normalize_title(target.get("title") or "")
    target["venue"] = better_text(target.get("venue"), incoming.get("venue")) or ""
    target["abstract"] = better_text(target.get("abstract"), incoming.get("abstract"))

    target_year = coerce_year(target.get("year"))
    incoming_year = coerce_year(incoming.get("year"))
    if target_year is None and incoming_year is not None:
        target["year"] = incoming_year
    elif target_year is not None and incoming_year is not None:
        target["year"] = min(target_year, incoming_year)

    if not target.get("doi") and incoming.get("doi"):
        target["doi"] = incoming["doi"]
    if not target.get("arxiv_id") and incoming.get("arxiv_id"):
        target["arxiv_id"] = incoming["arxiv_id"]
    if not target.get("url") and incoming.get("url"):
        target["url"] = incoming["url"]

    append_unique(target["authors"], incoming.get("authors") or [])
    append_unique(target["pdf_urls"], incoming.get("pdf_urls") or [])
    append_unique(target["sources"], incoming.get("sources") or [])

    source_ids = target.get("source_ids") or {}
    for source, source_id in (incoming.get("source_ids") or {}).items():
        source_ids.setdefault(source, source_id)
    target["source_ids"] = source_ids


def deduplicate_papers(raw_papers: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: Dict[int, Dict[str, Any]] = {}
    key_to_record: Dict[Tuple[str, str], int] = {}
    next_record_id = 1

    for raw in raw_papers:
        canonical = canonicalize_raw_paper(raw)
        if canonical is None:
            continue

        keys = dedup_keys(canonical)
        matched = {
            key_to_record[key]
            for key in keys
            if key in key_to_record and key_to_record[key] in records
        }

        if not matched:
            record_id = next_record_id
            next_record_id += 1
            records[record_id] = canonical
        else:
            record_id = min(matched)
            for other_id in sorted(matched):
                if other_id == record_id:
                    continue
                merge_papers(records[record_id], records.pop(other_id))
                for key, owner in list(key_to_record.items()):
                    if owner == other_id:
                        key_to_record[key] = record_id
            merge_papers(records[record_id], canonical)

        for key in dedup_keys(records[record_id]):
            key_to_record[key] = record_id

    output: List[Dict[str, Any]] = []
    for record_id in sorted(records):
        paper = records[record_id]
        paper["authors"] = normalize_authors(paper.get("authors") or [])
        append_unique(paper["pdf_urls"], [])
        append_unique(paper["sources"], [])
        paper["source_ids"] = dict(sorted((paper.get("source_ids") or {}).items()))

        if paper.get("doi"):
            paper_id = f"doi:{paper['doi']}"
        elif paper.get("arxiv_id"):
            paper_id = f"arxiv:{paper['arxiv_id']}"
        else:
            title_norm = paper.get("title_norm") or ""
            digest = hashlib.sha1(title_norm.encode("utf-8", errors="ignore")).hexdigest()[:16]
            paper_id = f"title:{digest}"

        paper["paper_id"] = paper_id
        paper.pop("title_norm", None)
        output.append(paper)

    return output


def rewrite_known_pdf_urls(url: str) -> List[str]:
    rewritten: List[str] = []
    lowered = url.lower()

    if "arxiv.org/abs/" in lowered:
        rewritten.append(re.sub(r"/abs/([^?#]+)", r"/pdf/\1.pdf", url, flags=re.IGNORECASE))
    if "openreview.net/forum?id=" in lowered:
        rewritten.append(url.replace("/forum?id=", "/pdf?id="))
    if "openaccess.thecvf.com" in lowered and lowered.endswith(".html"):
        rewritten.append(url[:-5] + ".pdf")
    if "aclanthology.org/" in lowered and ".pdf" not in lowered:
        rewritten.append(url.rstrip("/") + ".pdf")
    if "proceedings.mlr.press" in lowered and lowered.endswith(".html"):
        rewritten.append(url[:-5] + ".pdf")

    cleaned: List[str] = []
    for candidate in rewritten:
        normalized = clean_url(candidate)
        if normalized:
            cleaned.append(normalized)
    return cleaned


def sniff_doi_pdf_urls(client: PoliteHttpClient, doi: str) -> List[str]:
    doi_url = f"https://doi.org/{parse.quote(doi)}"
    try:
        response = client.request(
            doi_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    except Exception as exc:
        logging.debug("DOI landing lookup failed for %s: %s", doi, exc)
        return []

    candidates: List[str] = []
    final_url = clean_url(response.url)
    if final_url and is_probably_pdf_url(final_url):
        candidates.append(final_url)

    html_text = response.body.decode("utf-8", errors="replace")

    for match in re.finditer(r"citation_pdf_url\s*['\"]\s*content=['\"]([^'\"]+)['\"]", html_text, re.IGNORECASE):
        href = clean_url(parse.urljoin(response.url, match.group(1)))
        if href:
            candidates.append(href)

    for match in re.finditer(r"href=['\"]([^'\"]+\.pdf(?:\?[^'\"]*)?)['\"]", html_text, re.IGNORECASE):
        href = clean_url(parse.urljoin(response.url, match.group(1)))
        if href:
            candidates.append(href)

    deduped: List[str] = []
    append_unique(deduped, candidates)
    return deduped


def filename_slug(text: str, limit: int = 120) -> str:
    normalized = normalize_title(text)
    normalized = normalized.replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9\-]+", "", normalized)
    normalized = normalized.strip("-")
    if not normalized:
        normalized = "paper"
    return normalized[:limit]


def unique_file_path(directory: Path, base_name: str, extension: str) -> Path:
    candidate = directory / f"{base_name}{extension}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{base_name}-{counter}{extension}"
        if not candidate.exists():
            return candidate
        counter += 1


def unique_pdf_path(pdf_dir: Path, base_name: str) -> Path:
    return unique_file_path(pdf_dir, base_name, ".pdf")


def resolve_mineru_cmd(configured_cmd: str) -> Optional[str]:
    candidates: List[str] = []
    if configured_cmd:
        candidates.append(configured_cmd)
    for candidate in MINERU_DEFAULT_CMD_CANDIDATES:
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if not candidate:
            continue
        expanded = Path(candidate).expanduser()
        if expanded.parent != Path("."):
            if expanded.is_file() and os.access(expanded, os.X_OK):
                return str(expanded)
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def parse_pdf_with_mineru(
    paper: Dict[str, Any],
    *,
    pdf_path: Path,
    papers_dir: Path,
    mineru_work_dir: Path,
    mineru_cmd: str,
    mineru_backend: str,
    mineru_api_base: str,
    mineru_timeout: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    run_name = filename_slug(f"{paper.get('paper_id', '')}-{pdf_path.stem}", limit=70)
    hash_suffix = hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:8]
    run_dir = mineru_work_dir / f"{run_name}-{hash_suffix}"

    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    command = [
        mineru_cmd,
        "--path",
        str(pdf_path),
        "--output",
        str(run_dir),
        "--backend",
        mineru_backend,
    ]
    if mineru_backend.endswith("http-client"):
        command.extend(["--url", mineru_api_base])

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=False,
            timeout=mineru_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        return None, {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "pdf_path": str(pdf_path),
            "reason": "mineru_timeout",
            "timeout_seconds": mineru_timeout,
            "command": command,
            "stderr_tail": clipped_text(decode_process_text(exc.stderr)),
            "stdout_tail": clipped_text(decode_process_text(exc.stdout)),
        }
    except OSError as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        return None, {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "pdf_path": str(pdf_path),
            "reason": "mineru_command_error",
            "command": command,
            "error": str(exc),
        }

    if completed.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return None, {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "pdf_path": str(pdf_path),
            "reason": "mineru_nonzero_exit",
            "returncode": completed.returncode,
            "command": command,
            "stderr_tail": clipped_text(decode_process_text(completed.stderr)),
            "stdout_tail": clipped_text(decode_process_text(completed.stdout)),
        }

    markdown_candidates = sorted(
        (path for path in run_dir.rglob("*.md") if path.is_file()),
        key=lambda file_path: (file_path.stat().st_size, str(file_path)),
        reverse=True,
    )
    if not markdown_candidates:
        shutil.rmtree(run_dir, ignore_errors=True)
        return None, {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "pdf_path": str(pdf_path),
            "reason": "mineru_no_markdown",
            "command": command,
            "stderr_tail": clipped_text(decode_process_text(completed.stderr)),
            "stdout_tail": clipped_text(decode_process_text(completed.stdout)),
        }

    markdown_source = markdown_candidates[0]
    destination_base = filename_slug(f"{paper.get('title', 'paper')}-{paper.get('paper_id', '')}")
    destination = unique_file_path(papers_dir, destination_base, ".md")
    try:
        shutil.copyfile(markdown_source, destination)
    except OSError as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        return None, {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "pdf_path": str(pdf_path),
            "reason": "mineru_copy_failed",
            "source_markdown": str(markdown_source),
            "destination": str(destination),
            "error": str(exc),
        }

    shutil.rmtree(run_dir, ignore_errors=True)
    return str(Path("papers") / destination.name), None


def attempt_single_pdf_download(
    client: PoliteHttpClient,
    *,
    url: str,
    timeout_headers: Optional[Dict[str, str]] = None,
) -> Optional[HttpResponse]:
    headers = {
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    if timeout_headers:
        headers.update(timeout_headers)
    try:
        return client.request(url, headers=headers)
    except Exception as exc:
        logging.debug("PDF download failed: %s (%s)", url, exc)
        return None


def download_pdf_for_paper(
    client: PoliteHttpClient,
    paper: Dict[str, Any],
    *,
    pdf_dir: Path,
    min_pdf_bytes: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    attempted: List[Dict[str, str]] = []

    direct_urls: List[str] = []
    append_unique(direct_urls, paper.get("pdf_urls") or [])

    arxiv_urls: List[str] = []
    if paper.get("arxiv_id"):
        arxiv_urls.append(f"https://arxiv.org/pdf/{paper['arxiv_id']}.pdf")

    openreview_urls: List[str] = []
    note_id = (paper.get("source_ids") or {}).get("openreview")
    if note_id:
        openreview_urls.append(f"https://openreview.net/pdf?id={note_id}")
    paper_url = clean_url(paper.get("url"))
    if paper_url and "openreview.net/forum?id=" in paper_url.lower():
        openreview_urls.append(paper_url.replace("/forum?id=", "/pdf?id="))

    doi_urls: List[str] = []
    if paper.get("doi"):
        doi_urls = sniff_doi_pdf_urls(client, str(paper["doi"]))

    rewrite_urls: List[str] = []
    rewrite_seeds: List[str] = []
    append_unique(rewrite_seeds, direct_urls)
    append_unique(rewrite_seeds, openreview_urls)
    if paper_url:
        append_unique(rewrite_seeds, [paper_url])
    for seed in rewrite_seeds:
        append_unique(rewrite_urls, rewrite_known_pdf_urls(seed))

    method_groups = [
        ("direct_oa", direct_urls),
        ("arxiv", arxiv_urls),
        ("openreview", openreview_urls),
        ("doi_sniff", doi_urls),
        ("host_rewrite", rewrite_urls),
    ]

    for method, urls in method_groups:
        deduped_urls: List[str] = []
        append_unique(deduped_urls, [clean_url(url) or "" for url in urls])
        for url in deduped_urls:
            if not url:
                continue
            response = attempt_single_pdf_download(client, url=url)
            if response is None:
                attempted.append({"method": method, "url": url, "error": "request_failed"})
                continue

            content_type = str(response.headers.get("Content-Type", "")).lower()
            body = response.body
            is_pdf = body.startswith(b"%PDF-") or "application/pdf" in content_type
            if not is_pdf:
                attempted.append(
                    {
                        "method": method,
                        "url": url,
                        "error": f"non_pdf_content_type:{content_type or 'unknown'}",
                    }
                )
                continue

            if len(body) < min_pdf_bytes:
                attempted.append(
                    {
                        "method": method,
                        "url": url,
                        "error": f"too_small:{len(body)}bytes",
                    }
                )
                continue

            base_name = filename_slug(f"{paper.get('title','paper')}-{paper.get('paper_id','')}")
            destination = unique_pdf_path(pdf_dir, base_name)
            destination.write_bytes(body)
            relative_path = destination.name
            return relative_path, None

    failure = {
        "paper_id": paper.get("paper_id"),
        "title": paper.get("title"),
        "reason": "all_methods_failed",
        "attempted": attempted,
    }
    return None, failure


def write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def write_csv(path: Path, papers: Sequence[Dict[str, Any]]) -> None:
    columns = [
        "paper_id",
        "title",
        "year",
        "venue",
        "authors",
        "doi",
        "arxiv_id",
        "url",
        "sources",
        "source_ids",
        "pdf_urls",
        "pdf_path",
        "paper_md_path",
        "abstract",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for paper in papers:
            writer.writerow(
                {
                    "paper_id": paper.get("paper_id", ""),
                    "title": paper.get("title", ""),
                    "year": paper.get("year", ""),
                    "venue": paper.get("venue", ""),
                    "authors": "; ".join(paper.get("authors") or []),
                    "doi": paper.get("doi", ""),
                    "arxiv_id": paper.get("arxiv_id", ""),
                    "url": paper.get("url", ""),
                    "sources": "; ".join(paper.get("sources") or []),
                    "source_ids": json.dumps(paper.get("source_ids") or {}, ensure_ascii=False),
                    "pdf_urls": "; ".join(paper.get("pdf_urls") or []),
                    "pdf_path": paper.get("pdf_path", ""),
                    "paper_md_path": paper.get("paper_md_path", ""),
                    "abstract": paper.get("abstract", "") or "",
                }
            )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect top-conference papers from OpenAlex, Crossref, Semantic Scholar, and OpenReview. "
            "Only public/open links are used; no paywall bypass or captcha evasion is attempted."
        ),
        formatter_class=HelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 collect_topconf_papers.py --query \"vision transformer\" "
            "--venues NeurIPS,ICLR,CVPR --years 2022-2025 --max-per-source 80 --out-dir ./out\n"
            "  python3 collect_topconf_papers.py --query \"large language model\" "
            "--years 2024,2025 --download-pdf --min-pdf-bytes 20000 "
            "--mineru-cmd /home/jinyilun/anaconda3/envs/pdf/bin/mineru "
            "--mineru-backend vlm-http-client --mineru-api-base http://127.0.0.1:8000"
        ),
    )
    parser.add_argument("--query", required=True, help="Search query for paper discovery.")
    parser.add_argument(
        "--venues",
        default="",
        help="Comma-separated venue keywords (e.g., NeurIPS,ICML,ICLR).",
    )
    parser.add_argument(
        "--years",
        default="",
        help="Comma-separated publication years and/or ranges (e.g., 2022,2024-2025).",
    )
    parser.add_argument(
        "--max-per-source",
        type=int,
        default=100,
        help="Maximum number of accepted papers to keep from each source.",
    )
    parser.add_argument("--out-dir", default="./topconf_papers_out", help="Output directory.")
    parser.add_argument(
        "--download-pdf",
        action="store_true",
        help="Attempt to download PDFs using multiple open-access methods.",
    )
    parser.add_argument(
        "--min-pdf-bytes",
        type=int,
        default=10_000,
        help="Reject downloaded PDFs smaller than this size.",
    )
    parser.add_argument(
        "--mineru-cmd",
        default="mineru",
        help=(
            "MinerU CLI command path for parsing downloaded PDFs. "
            "If not resolvable, fallback candidates are tried."
        ),
    )
    parser.add_argument(
        "--mineru-backend",
        default="vlm-http-client",
        choices=MINERU_BACKEND_CHOICES,
        help="MinerU backend for parsing downloaded PDFs.",
    )
    parser.add_argument(
        "--mineru-api-base",
        default="http://127.0.0.1:8000",
        help="MinerU local API base URL (used with *-http-client backends).",
    )
    parser.add_argument(
        "--mineru-timeout",
        type=int,
        default=900,
        help="Timeout in seconds for one MinerU PDF parsing task.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds for each request.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Max retry attempts for retryable failures.",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=1.0,
        help="Minimum interval (seconds) between requests per host.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args(argv)


def ensure_positive_int(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0")


def collect_all_sources(
    client: PoliteHttpClient,
    *,
    query: str,
    max_per_source: int,
    venue_terms: Sequence[str],
    years: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    raw_papers: List[Dict[str, Any]] = []
    source_functions = [
        ("OpenAlex", fetch_openalex),
        ("Crossref", fetch_crossref),
        ("Semantic Scholar", fetch_semantic_scholar),
        ("OpenReview", fetch_openreview),
    ]

    for source_name, fetcher in source_functions:
        try:
            logging.info("Collecting from %s ...", source_name)
            papers = fetcher(
                client,
                query=query,
                max_per_source=max_per_source,
                venue_terms=venue_terms,
                years=years,
            )
            raw_papers.extend(papers)
            logging.info("%s returned %d filtered papers", source_name, len(papers))
        except Exception as exc:
            logging.warning("%s collection failed: %s", source_name, exc)

    return raw_papers


def download_pdfs(
    client: PoliteHttpClient,
    papers: List[Dict[str, Any]],
    *,
    out_dir: Path,
    min_pdf_bytes: int,
    mineru_cmd: str,
    mineru_backend: str,
    mineru_api_base: str,
    mineru_timeout: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    pdf_dir = out_dir / "pdfs"
    papers_dir = out_dir / "papers"
    mineru_work_dir = out_dir / ".mineru_tmp"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    papers_dir.mkdir(parents=True, exist_ok=True)
    mineru_work_dir.mkdir(parents=True, exist_ok=True)
    failed_downloads: List[Dict[str, Any]] = []
    failed_mineru_parses: List[Dict[str, Any]] = []

    for index, paper in enumerate(papers, start=1):
        logging.info("[%d/%d] Downloading PDF for: %s", index, len(papers), paper.get("title", ""))
        relative_pdf_name, failure = download_pdf_for_paper(
            client,
            paper,
            pdf_dir=pdf_dir,
            min_pdf_bytes=min_pdf_bytes,
        )
        if relative_pdf_name:
            paper["pdf_path"] = str(Path("pdfs") / relative_pdf_name)
            pdf_path = out_dir / paper["pdf_path"]
            relative_md_path, parse_failure = parse_pdf_with_mineru(
                paper,
                pdf_path=pdf_path,
                papers_dir=papers_dir,
                mineru_work_dir=mineru_work_dir,
                mineru_cmd=mineru_cmd,
                mineru_backend=mineru_backend,
                mineru_api_base=mineru_api_base,
                mineru_timeout=mineru_timeout,
            )
            if relative_md_path:
                paper["paper_md_path"] = relative_md_path
            elif parse_failure:
                failed_mineru_parses.append(parse_failure)
        elif failure:
            failed_downloads.append(failure)
    return failed_downloads, failed_mineru_parses


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )

    try:
        ensure_positive_int("--max-per-source", int(args.max_per_source))
        ensure_positive_int("--min-pdf-bytes", int(args.min_pdf_bytes))
        ensure_positive_int("--mineru-timeout", int(args.mineru_timeout))
        if args.timeout <= 0:
            raise ValueError("--timeout must be > 0")
        if args.retries <= 0:
            raise ValueError("--retries must be > 0")
        if args.min_interval < 0:
            raise ValueError("--min-interval must be >= 0")
        years = parse_years(args.years)
    except ValueError as exc:
        logging.error("Invalid arguments: %s", exc)
        return 2

    venue_terms = normalize_requested_venue_terms(parse_csv_list(args.venues))

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    papers_dir = out_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    client = PoliteHttpClient(
        timeout=float(args.timeout),
        retries=int(args.retries),
        min_interval=float(args.min_interval),
        user_agent="topconf-paper-collector/1.0 (public-metadata-collector)",
    )

    raw_papers = collect_all_sources(
        client,
        query=args.query,
        max_per_source=int(args.max_per_source),
        venue_terms=venue_terms,
        years=years,
    )
    logging.info("Collected %d raw papers before dedup", len(raw_papers))

    deduped_papers = deduplicate_papers(raw_papers)
    logging.info("Retained %d papers after dedup", len(deduped_papers))

    failed_downloads: List[Dict[str, Any]] = []
    failed_mineru_parses: List[Dict[str, Any]] = []
    if args.download_pdf and deduped_papers:
        resolved_mineru_cmd = resolve_mineru_cmd(str(args.mineru_cmd))
        if not resolved_mineru_cmd:
            logging.error(
                "MinerU command not found. Tried --mineru-cmd=%s and fallbacks=%s",
                args.mineru_cmd,
                ", ".join(MINERU_DEFAULT_CMD_CANDIDATES),
            )
            return 2

        failed_downloads, failed_mineru_parses = download_pdfs(
            client,
            deduped_papers,
            out_dir=out_dir,
            min_pdf_bytes=int(args.min_pdf_bytes),
            mineru_cmd=resolved_mineru_cmd,
            mineru_backend=str(args.mineru_backend),
            mineru_api_base=str(args.mineru_api_base),
            mineru_timeout=int(args.mineru_timeout),
        )
        logging.info(
            "PDF download finished: success=%d failed=%d",
            len(deduped_papers) - len(failed_downloads),
            len(failed_downloads),
        )
        logging.info(
            "MinerU parse finished: success=%d failed=%d",
            len([paper for paper in deduped_papers if paper.get("paper_md_path")]),
            len(failed_mineru_parses),
        )

    jsonl_path = out_dir / "papers.jsonl"
    csv_path = out_dir / "papers.csv"
    write_jsonl(jsonl_path, deduped_papers)
    write_csv(csv_path, deduped_papers)

    if args.download_pdf:
        write_jsonl(out_dir / "failed_downloads.jsonl", failed_downloads)
        write_jsonl(out_dir / "failed_mineru_parses.jsonl", failed_mineru_parses)

    logging.info("Wrote metadata: %s", jsonl_path)
    logging.info("Wrote metadata: %s", csv_path)
    logging.info("Wrote markdown papers directory: %s", papers_dir)
    if args.download_pdf:
        logging.info("Wrote failures: %s", out_dir / "failed_downloads.jsonl")
        logging.info("Wrote failures: %s", out_dir / "failed_mineru_parses.jsonl")

    return 0


if __name__ == "__main__":
    sys.exit(main())
