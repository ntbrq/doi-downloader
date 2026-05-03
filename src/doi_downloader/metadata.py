"""CrossRef DOI metadata resolver."""

from dataclasses import dataclass

import httpx


@dataclass
class PaperMetadata:
    doi: str
    title: str
    authors: list[str]
    year: int | None
    journal: str | None


def resolve_metadata(doi: str) -> PaperMetadata:
    """通过 CrossRef API 解析 DOI 元数据。"""
    url = f"https://api.crossref.org/works/{doi}"
    resp = httpx.get(
        url,
        headers={"User-Agent": "doi-downloader/0.1 (mailto:user@example.com)"},
        timeout=30,
    )
    if resp.status_code == 404:
        raise ValueError(f"DOI not found: {doi}")
    resp.raise_for_status()

    msg = resp.json()["message"]

    title_list = msg.get("title", [])
    title = title_list[0] if title_list else "Untitled"

    raw_authors = msg.get("author", [])
    authors = []
    for a in raw_authors:
        family = a.get("family", "")
        given = a.get("given", "")
        if family and given:
            authors.append(f"{family} {given}")
        elif family:
            authors.append(family)
        elif given:
            authors.append(given)

    year = None
    for date_field in ("published-print", "published-online", "created"):
        parts = msg.get(date_field, {}).get("date-parts", [[]])
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break

    journal_list = msg.get("container-title", [])
    journal = journal_list[0] if journal_list else None

    return PaperMetadata(doi=doi, title=title, authors=authors, year=year, journal=journal)
