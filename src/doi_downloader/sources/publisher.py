"""出版商官网 PDF 下载源。通过 DOI 重定向找到 PDF 链接。"""

from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource


class PublisherSource(DownloadSource):
    """从出版商官网下载 PDF。通过 DOI 重定向找到 PDF 链接。"""

    name = "publisher"

    def __init__(self, timeout: float = 30.0, user_agent: str = "doi-downloader/0.1"):
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent}

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """通过 DOI 重定向页面查找 PDF 链接。"""
        try:
            resp = httpx.get(
                f"https://doi.org/{doi}",
                headers=self.headers,
                timeout=self.timeout,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("content-type", "")

            # 重定向后直接返回 PDF
            if "application/pdf" in content_type:
                return str(resp.url)

            # HTML 页面中搜索 PDF 链接
            if "text/html" in content_type:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].lower()
                    if "pdf" in href or ".pdf" in href:
                        return urljoin(str(resp.url), a["href"])
                for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
                    url = meta.get("content")
                    if url:
                        return url

            return None
        except httpx.HTTPError:
            return None

    def download(self, url: str, dest: Path) -> bool:
        """下载 PDF 并验证文件头。"""
        try:
            resp = httpx.get(
                url,
                headers=self.headers,
                timeout=self.timeout,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return False

            content = resp.content
            if len(content) < 4 or content[:4] != b"%PDF":
                return False

            dest.write_bytes(content)
            return True
        except httpx.HTTPError:
            return False
