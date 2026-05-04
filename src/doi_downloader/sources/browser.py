"""基于 Playwright 的下载源。处理 JS 渲染页面和 Cloudflare 验证。

Playwright 的同步 API 使用 greenlet，不支持多线程。
所有 Playwright 操作必须在同一个线程中执行。
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource


class BrowserSource(DownloadSource):
    """通过 Playwright 无头浏览器下载 PDF。处理 Cloudflare 和 JS 渲染页面。"""

    name = "browser"

    REALISTIC_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        # 专用单线程执行器，保证 Playwright 操作在同一线程
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _ensure_browser(self):
        """懒初始化共享的 Playwright 浏览器实例（必须在专用线程中调用）。"""
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)

    def _close_in_thread(self):
        """在专用线程中关闭浏览器。"""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def close(self):
        """关闭浏览器实例和执行器。"""
        try:
            self._executor.submit(self._close_in_thread).result(timeout=10)
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    def _find_pdf_url_in_thread(self, doi: str) -> str | None:
        """在专用线程中查找 PDF URL。"""
        self._ensure_browser()
        ctx = self._browser.new_context(
            user_agent=self.REALISTIC_UA,
            accept_downloads=True,
        )
        try:
            page = ctx.new_page()
            url = self._find_pdf_from_url(page, f"https://doi.org/{doi}")
            if url:
                return url

            for pub_url in self._build_publisher_urls(doi):
                url = self._find_pdf_from_url(page, pub_url)
                if url:
                    return url

            return None
        except Exception:
            return None
        finally:
            ctx.close()

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """通过浏览器渲染页面查找 PDF 链接（线程安全）。"""
        future = self._executor.submit(self._find_pdf_url_in_thread, doi)
        try:
            return future.result(timeout=self.timeout * 3)
        except Exception:
            return None

    def _find_pdf_from_url(self, page, url: str) -> str | None:
        """访问 URL，在渲染后的页面中查找 PDF 链接。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare(page)
            page.wait_for_timeout(2000)

            if self._page_is_pdf_viewer(page):
                return url

            return self._extract_pdf_from_page(page, url)
        except Exception:
            return None

    def _wait_for_cloudflare(self, page):
        """等待 Cloudflare 验证页面通过。"""
        try:
            for _ in range(10):
                html = page.content()
                if "cf-browser-verification" not in html and "cf_chl_opt" not in html:
                    break
                page.wait_for_timeout(1000)
        except Exception:
            pass

    def _page_is_pdf_viewer(self, page) -> bool:
        """检查页面是否是 PDF 查看器。"""
        try:
            for selector in ["embed[type='application/pdf']", "iframe[src*='.pdf']"]:
                elements = page.query_selector_all(selector)
                if elements:
                    return True
            if page.url.lower().endswith(".pdf"):
                return True
        except Exception:
            pass
        return False

    def _extract_pdf_from_page(self, page, base_url: str) -> str | None:
        """从渲染后的页面 HTML 中提取 PDF 链接。"""
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
            url = meta.get("content")
            if url:
                return url

        url = self._extract_publisher_specific(soup, base_url, html)
        if url:
            return url

        pdf_candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            if any(skip in href_lower for skip in ["signup", "login", "register", "help", "faq", "cookie"]):
                continue
            if ".pdf" in href_lower or "/pdf" in href_lower or "getpdf" in href_lower:
                pdf_candidates.append(urljoin(base_url, href))

        if pdf_candidates:
            return max(pdf_candidates, key=len)

        for tag_name in ["iframe", "embed"]:
            for tag in soup.find_all(tag_name, src=True):
                src = tag["src"].lower()
                if ".pdf" in src or "pdf" in src:
                    return urljoin(base_url, tag["src"])

        return None

    def _extract_publisher_specific(self, soup: BeautifulSoup, base_url: str, html: str) -> str | None:
        """针对特定出版商提取 PDF 链接。"""
        if "ieeexplore.ieee.org" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/document/" in href and "/pdf" in href.lower():
                    return urljoin(base_url, href)
            for a in soup.find_all("a", attrs={"class": re.compile(r"pdf", re.I)}):
                href = a.get("href", "")
                if href:
                    return urljoin(base_url, href)

        if "sciencedirect.com" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/pdf/" in href or "reader" in href.lower():
                    return urljoin(base_url, href)
            for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
                return meta.get("content")

        if "onlinelibrary.wiley.com" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "pdfdirect" in href or "/epdf/" in href:
                    return urljoin(base_url, href)

        if "nature.com" in base_url or "springer.com" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href and ("article" in href or "chapter" in href):
                    return urljoin(base_url, href)

        return None

    def _build_publisher_urls(self, doi: str) -> list[str]:
        """构造已知出版商的 DOI 页面 URL。"""
        urls: list[str] = []

        if doi.startswith("10.1109/"):
            urls.append(f"https://ieeexplore.ieee.org/document/{doi.split('/')[-1]}")

        if doi.startswith("10.1016/"):
            article_id = doi.split("/", 1)[1]
            urls.append(f"https://www.sciencedirect.com/science/article/pii/{article_id}")

        if doi.startswith("10.3389/"):
            urls.append(f"https://www.frontiersin.org/articles/{doi}")

        if doi.startswith("10.2139/"):
            ssrn_id = doi.split(".")[-1]
            urls.append(f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_id}")

        return urls

    def _download_in_thread(self, url: str, dest: Path) -> bool:
        """在专用线程中下载 PDF。"""
        self._ensure_browser()
        ctx = self._browser.new_context(
            user_agent=self.REALISTIC_UA,
            accept_downloads=True,
        )
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare(page)

            if self._page_is_pdf_viewer(page):
                return self._download_pdf_url(url, dest)

            pdf_url = self._extract_pdf_from_page(page, url)
            if pdf_url:
                return self._download_pdf_url(pdf_url, dest)

            return False
        except Exception:
            return False
        finally:
            ctx.close()

    def download(self, url: str, dest: Path) -> bool:
        """通过浏览器下载 PDF（线程安全）。"""
        future = self._executor.submit(self._download_in_thread, url, dest)
        try:
            return future.result(timeout=self.timeout * 3)
        except Exception:
            return False

    def _download_pdf_url(self, url: str, dest: Path) -> bool:
        """通过 httpx 下载 PDF 文件。"""
        import httpx

        headers = {"User-Agent": self.REALISTIC_UA}
        try:
            client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, read=self.timeout * 3),
                follow_redirects=True,
            )
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=8192):
                        f.write(chunk)

            if not self.is_pdf(dest):
                dest.unlink(missing_ok=True)
                return False
            return True
        except Exception:
            dest.unlink(missing_ok=True)
            return False
