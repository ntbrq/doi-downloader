"""Unpaywall OA PDF download source."""

from pathlib import Path

import httpx

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource


class UnpaywallSource(DownloadSource):
    """通过 Unpaywall API 获取开放获取 PDF。"""

    name = "unpaywall"

    def __init__(self, email: str = "user@example.com", timeout: float = 30.0):
        self.email = email
        self.timeout = timeout

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """查询 Unpaywall API 获取 OA PDF 链接。"""
        try:
            url = f"https://api.unpaywall.org/v2/{doi}?email={self.email}"
            resp = httpx.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None

            data = resp.json()
            best = data.get("best_oa_location")
            if best and best.get("url_for_pdf"):
                return best["url_for_pdf"]

            for loc in data.get("oa_locations", []):
                pdf_url = loc.get("url_for_pdf")
                if pdf_url:
                    return pdf_url

            return None
        except (httpx.HTTPError, ValueError):
            return None

    def download(self, url: str, dest: Path) -> bool:
        """下载 PDF 并验证文件头。"""
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
            if resp.status_code != 200:
                return False
            content = resp.content
            if len(content) < 4 or content[:4] != b"%PDF":
                return False
            dest.write_bytes(content)
            return True
        except httpx.HTTPError:
            return False
