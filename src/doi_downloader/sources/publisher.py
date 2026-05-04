"""出版商官网 PDF 下载源。通过 DOI 重定向或已知模式构造 PDF 链接。"""

import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource

# 已知出版商的 PDF URL 构造模式
_PUBLISHER_PDF_PATTERNS: list[tuple[str, str]] = [
    # MDPI: 10.3390/xxxxx -> https://www.mdpi.com/ISSUE/pdf?version=...
    # 需要动态构造，放在 _try_known_patterns 中
    # Frontiers: 10.3389/fxxxx.NNNNNN -> https://www.frontiersin.org/journals/xxx/articles/.../pdf
    # 需要动态构造
    # ACM: 直接尝试
    ("10.1145/", "https://dl.acm.org/doi/pdf/{doi}"),
    # IOP
    ("10.1088/", "https://iopscience.iop.org/article/{doi}/pdf"),
    # RSC
    ("10.1039/", "https://pubs.rsc.org/en/content/articlepdf/{doi}"),
    # OSA
    ("10.1364/", "https://opg.optica.org/{doi_full}/viewmedia.cfm?uri={doi_full}"),
]


def _build_known_pdf_urls(doi: str) -> list[str]:
    """根据 DOI 前缀构造可能的直接 PDF URL。"""
    urls: list[str] = []

    # MDPI: 10.3390/JOURNAL/ISSUE/ARTICLE (e.g. 10.3390/mi15020253)
    m = re.match(r"10\.3390/([a-z]+)(\d+)(\d+)(\d+)", doi)
    if m:
        journal, volume, issue, article = m.groups()
        urls.append(f"https://www.mdpi.com/{journal}/{volume}/{issue}/{article}/pdf")

    # Frontiers: 10.3389/xxxx.NNNNNN — open access
    if doi.startswith("10.3389/"):
        urls.append(f"https://www.frontiersin.org/articles/{doi}/pdf")

    # SSRN: 10.2139/ssrn.NNNNNNN — preprints, usually open
    if doi.startswith("10.2139/"):
        ssrn_id = doi.split(".")[-1]
        urls.append(f"https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID{ssrn_id}_code.pdf")

    # IEEE: 10.1109/xxxxx 或 10.23919/xxxxx
    if doi.startswith("10.1109/") or doi.startswith("10.23919/"):
        # IEEE 的 PDF 需要从 Xplore 页面解析，但可以尝试 stamp URL
        article_num = doi.split(".")[-1]
        urls.append(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={article_num}")

    # Elsevier/ScienceDirect: 10.1016/j.xxxx
    if doi.startswith("10.1016/"):
        pii = doi.split("/", 1)[1].replace("/", "")
        urls.append(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft")
        urls.append(f"https://www.sciencedirect.com/science/article/pii/{pii}")

    # ACS: 10.1021/xxxxx
    if doi.startswith("10.1021/"):
        urls.append(f"https://pubs.acs.org/doi/pdf/{doi}")

    # Nature: 10.1038/xxxxx
    if doi.startswith("10.1038/"):
        article = doi.split("/", 1)[1]
        urls.append(f"https://www.nature.com/articles/{article}.pdf")

    # Science/AAAS: 10.1126/xxxxx
    if doi.startswith("10.1126/"):
        urls.append(f"https://www.science.org/doi/pdf/{doi}")

    # IOP: 10.1088/xxxx-xxxx/xx/xx/xxxxx
    if doi.startswith("10.1088/"):
        urls.append(f"https://iopscience.iop.org/article/{doi}/pdf")

    # Wiley: 10.1002/xxxxx
    if doi.startswith("10.1002/"):
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}")

    # AIP: 10.1063/NN.NNNNNN
    if doi.startswith("10.1063/"):
        urls.append(f"https://pubs.aip.org/aip/apl/article-pdf/{doi.split('/', 1)[1]}")

    # Generic patterns
    for prefix, pattern in _PUBLISHER_PDF_PATTERNS:
        if doi.startswith(prefix):
            urls.append(pattern.format(doi=doi, doi_full=doi))

    return urls


class PublisherSource(DownloadSource):
    """从出版商官网下载 PDF。通过 DOI 重定向找到 PDF 链接。"""

    name = "publisher"

    REALISTIC_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout: float = 60.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = {
            "User-Agent": self.REALISTIC_UA,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response | None:
        """带重试的 HTTP 请求。"""
        for attempt in range(self.max_retries + 1):
            try:
                client = httpx.Client(timeout=self.timeout, follow_redirects=True)
                resp = client.request(method, url, headers=self.headers, **kwargs)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 403 and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return resp
            except (httpx.HTTPError, httpx.TimeoutException):
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return None
        return None

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """通过 DOI 重定向页面查找 PDF 链接。"""
        # 1. 先尝试已知出版商的直接 PDF URL
        known_urls = _build_known_pdf_urls(doi)
        for url in known_urls:
            if self._probe_pdf_url(url):
                return url

        # 2. 跟随 DOI 重定向，在 HTML 中搜索 PDF 链接
        try:
            resp = self._request_with_retry("GET", f"https://doi.org/{doi}")
            if resp is None or resp.status_code != 200:
                return None

            content_type = resp.headers.get("content-type", "")

            # 重定向后直接返回 PDF
            if "application/pdf" in content_type:
                return str(resp.url)

            # HTML 页面中搜索 PDF 链接
            if "text/html" in content_type:
                return self._extract_pdf_from_html(resp.text, str(resp.url))

            return None
        except Exception:
            return None

    def _probe_pdf_url(self, url: str) -> bool:
        """用 HEAD 请求探查 URL 是否返回 PDF。"""
        try:
            client = httpx.Client(timeout=10, follow_redirects=True)
            resp = client.head(url, headers=self.headers)
            ct = resp.headers.get("content-type", "")
            return resp.status_code == 200 and "application/pdf" in ct
        except Exception:
            return False

    def _extract_pdf_from_html(self, html: str, base_url: str) -> str | None:
        """从 HTML 中提取 PDF 链接。"""
        soup = BeautifulSoup(html, "html.parser")

        # 优先: citation_pdf_url meta 标签
        for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
            url = meta.get("content")
            if url:
                return url

        # 其次: 带 pdf 关键字的 <a> 标签
        pdf_candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            # 排除导航/广告链接
            if any(skip in href_lower for skip in ["signup", "login", "register", "help", "faq"]):
                continue
            if ".pdf" in href_lower or "/pdf" in href_lower or "getpdf" in href_lower:
                pdf_candidates.append(urljoin(base_url, href))

        if pdf_candidates:
            # 优先选择更具体的 URL（更长的）
            return max(pdf_candidates, key=len)

        # 最后: 检查 <iframe> 或 <embed> 中的 PDF
        for tag_name in ["iframe", "embed"]:
            for tag in soup.find_all(tag_name, src=True):
                src = tag["src"].lower()
                if ".pdf" in src or "pdf" in src:
                    return urljoin(base_url, tag["src"])

        return None

    def download(self, url: str, dest: Path) -> bool:
        """流式下载 PDF 并验证文件头。"""
        for attempt in range(self.max_retries + 1):
            try:
                client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout, read=self.timeout * 3),
                    follow_redirects=True,
                )
                with client.stream("GET", url, headers=self.headers) as resp:
                    if resp.status_code != 200:
                        if resp.status_code == 403 and attempt < self.max_retries:
                            time.sleep(2 ** attempt)
                            continue
                        return False

                    # 流式写入
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open("wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=8192):
                            f.write(chunk)

                # 验证 PDF 文件头
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
