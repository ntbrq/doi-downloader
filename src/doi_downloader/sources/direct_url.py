"""直接 URL 构造下载源。根据已知出版商 URL 模式构造 PDF 链接。"""

import re
import time
from pathlib import Path

import httpx

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource


def _build_direct_urls(doi: str) -> list[str]:
    """根据 DOI 构造可能的直接 PDF URL。"""
    urls: list[str] = []

    # MDPI: 10.3390/JOURNALVOLUMEISSUEARTICLE
    m = re.match(r"10\.3390/([a-z]+)(\d+)(\d+)(\d+)", doi)
    if m:
        journal, volume, issue, article = m.groups()
        urls.append(f"https://www.mdpi.com/{journal}/{volume}/{issue}/{article}/pdf")

    # Frontiers: 10.3389/fxxxx.NNNNNN 或 10.3389/xxxx.NNNNNN
    if doi.startswith("10.3389/"):
        # Frontiers 的 DOI 解析后会重定向到有 PDF 的页面
        # 但我们可以尝试直接构造
        pass  # 由 publisher 源的 HTML 解析处理

    # ACM: 10.1145/NNNNNNN 或 10.1145/NNNNNNN.NNNNNNN
    if doi.startswith("10.1145/"):
        urls.append(f"https://dl.acm.org/doi/pdf/{doi}")

    # IOP: 10.1088/xxxx-xxxx/xx/xx/xxxxx
    if doi.startswith("10.1088/"):
        urls.append(f"https://iopscience.iop.org/article/{doi}/pdf")

    # AIP: 10.1063/NN.NNNNNN
    if doi.startswith("10.1063/"):
        # AIP 的 PDF URL 需要从页面解析
        pass

    # RSC: 10.1039/xxxxxxx
    if doi.startswith("10.1039/"):
        urls.append(f"https://pubs.rsc.org/en/content/articlepdf/{doi}")

    # OSA: 10.1364/XXXXX
    if doi.startswith("10.1364/"):
        parts = doi.split("/")
        if len(parts) >= 2:
            urls.append(f"https://opg.optica.org/{doi}/viewmedia.cfm?uri={doi}")

    # Springer Nature: 10.1038/xxxxx
    if doi.startswith("10.1038/"):
        urls.append(f"https://www.nature.com/articles/{doi.split('/', 1)[1]}.pdf")

    # Science/AAAS: 10.1126/xxxxx
    if doi.startswith("10.1126/"):
        urls.append(f"https://www.science.org/doi/pdf/{doi}")

    # Wiley: 10.1002/xxxxx
    if doi.startswith("10.1002/"):
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}")

    # IEEE: 10.1109/xxxxx (需要从页面获取文章编号)
    # IEEE 的 PDF URL 需要文章编号，不能直接从 DOI 构造

    # Elsevier: 10.1016/j.xxxx
    if doi.startswith("10.1016/"):
        # ScienceDirect PDF 需要从页面解析
        pass

    # MDPI (另一种格式)
    if doi.startswith("10.3390/"):
        # 也尝试不带 version 参数的 URL
        m2 = re.match(r"10\.3390/([a-z]+)(\d+)(\d+)(\d+)", doi)
        if m2:
            journal, volume, issue, article = m2.groups()
            urls.append(f"https://www.mdpi.com/{journal}/{volume}/{issue}/{article}/pdf")

    return urls


class DirectUrlSource(DownloadSource):
    """通过已知出版商 URL 模式直接构造 PDF 链接。"""

    name = "direct_url"

    REALISTIC_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout: float = 60.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """尝试直接构造的 PDF URL。"""
        urls = _build_direct_urls(doi)
        for url in urls:
            if self._probe_pdf_url(url):
                return url
        return None

    def _probe_pdf_url(self, url: str) -> bool:
        """用 HEAD 请求探查 URL 是否返回 PDF。"""
        try:
            client = httpx.Client(
                timeout=10,
                follow_redirects=True,
                headers={"User-Agent": self.REALISTIC_UA},
            )
            resp = client.head(url)
            ct = resp.headers.get("content-type", "")
            cl = resp.headers.get("content-length", "0")
            # 如果是 PDF 或者文件足够大（可能是 PDF）
            if "application/pdf" in ct:
                return True
            # 有些服务器不返回正确的 content-type，但返回大文件
            if int(cl) > 100000 and "text/html" not in ct:
                return True
            return False
        except Exception:
            return False

    def download(self, url: str, dest: Path) -> bool:
        """流式下载 PDF 并验证文件头。"""
        headers = {"User-Agent": self.REALISTIC_UA}
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
