"""Sci-Hub PDF 下载源。通过 Sci-Hub 搜索 DOI 找到 PDF。"""

import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource

# Sci-Hub 镜像列表，按优先级排序
SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ren",
    "https://sci-hub.shop",
]


class SciHubSource(DownloadSource):
    """通过 Sci-Hub 下载 PDF。支持多镜像回退。"""

    name = "sci-hub"

    def __init__(
        self,
        base_url: str = "https://sci-hub.se",
        timeout: float = 60.0,
        mirrors: list[str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mirrors = mirrors or SCIHUB_MIRRORS
        # 确保 base_url 在镜像列表首位
        if self.base_url not in self.mirrors:
            self.mirrors.insert(0, self.base_url)

    def _get_page(self, doi: str, mirror: str):
        """通过 Sci-Hub 搜索 DOI 并返回页面。仅用于测试 mock。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()
            page.goto(
                f"{mirror}/{doi}",
                wait_until="networkidle",
                timeout=int(self.timeout * 1000),
            )
            return page

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """在 Sci-Hub 页面中查找 PDF 链接。遍历多个镜像。"""
        for mirror in self.mirrors:
            try:
                page = self._get_page(doi, mirror)
                content = page.content()
                page.context.close()

                url = self._extract_pdf_url(content, mirror)
                if url:
                    return url
            except Exception:
                continue
        return None

    def _extract_pdf_url(self, html: str, mirror: str) -> str | None:
        """从 Sci-Hub 页面 HTML 中提取 PDF URL。"""
        soup = BeautifulSoup(html, "html.parser")

        # Sci-Hub 通常将 PDF 放在 iframe 中
        iframe = soup.find("iframe", attrs={"id": "pdf"}) or soup.find(
            "iframe", src=True
        )
        if iframe:
            src = iframe.get("src", "")
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = mirror + src
                return src

        # 也检查直接的 PDF 链接
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                if href.startswith("/"):
                    href = mirror + href
                return href

        # 检查 embed/object 标签
        for tag_name in ["embed", "object"]:
            for tag in soup.find_all(tag_name):
                src = tag.get("src") or tag.get("data", "")
                if src and ".pdf" in src.lower():
                    if src.startswith("/"):
                        src = mirror + src
                    return src

        return None

    def download(self, url: str, dest: Path) -> bool:
        """流式下载 PDF 并验证文件头。"""
        for attempt in range(3):
            try:
                client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout, read=self.timeout * 3),
                    follow_redirects=True,
                )
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        if attempt < 2:
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
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                dest.unlink(missing_ok=True)
                return False
        return False
