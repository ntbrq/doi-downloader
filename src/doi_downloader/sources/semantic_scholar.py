"""Semantic Scholar OA PDF download source."""

import time
from pathlib import Path

import httpx

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource


class SemanticScholarSource(DownloadSource):
    """通过 Semantic Scholar API 获取开放获取 PDF。"""

    name = "semantic_scholar"

    def __init__(self, timeout: float = 30.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """查询 Semantic Scholar API 获取 OA PDF 链接。"""
        for attempt in range(self.max_retries + 1):
            try:
                url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf,isOpenAccess"
                resp = httpx.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429:
                    # Rate limit, wait and retry
                    time.sleep(5 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    if attempt < self.max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return None

                data = resp.json()
                oa_pdf = data.get("openAccessPdf")
                if oa_pdf and oa_pdf.get("url"):
                    return oa_pdf["url"]

                return None
            except (httpx.HTTPError, httpx.TimeoutException, ValueError):
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return None
        return None

    def download(self, url: str, dest: Path) -> bool:
        """流式下载 PDF 并验证文件头。"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        for attempt in range(self.max_retries + 1):
            try:
                client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout, read=self.timeout * 3),
                    follow_redirects=True,
                )
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        if attempt < self.max_retries:
                            time.sleep(2 ** attempt)
                            continue
                        return False

                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open("wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=8192):
                            f.write(chunk)

                if not self.is_pdf(dest):
                    dest.unlink(missing_ok=True)
                    return False

                return True
            except (httpx.HTTPError, httpx.TimeoutException):
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                dest.unlink(missing_ok=True)
                return False
        return False
