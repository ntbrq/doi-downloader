"""批量下载调度器：并发竞争 + 多源回退 + 浏览器兜底。"""

from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
import threading

from doi_downloader.config import Config
from doi_downloader.metadata import PaperMetadata
from doi_downloader.renamer import rename_pdf
from doi_downloader.sources.base import DownloadSource


@dataclass
class DownloadResult:
    doi: str
    success: bool
    file_path: Path | None = None
    error: str | None = None
    source_used: str | None = None


@dataclass
class _SourceAttempt:
    """单个源的尝试结果。"""
    url: str | None = None
    downloaded: bool = False
    source_name: str = ""


class _OnceResult:
    """线程安全的单次赋值容器。"""

    def __init__(self):
        self._value: DownloadResult | None = None
        self._lock = threading.Lock()
        self._set = False

    def set_if_first(self, result: DownloadResult) -> bool:
        with self._lock:
            if self._set:
                return False
            self._set = True
            self._value = result
            return True

    @property
    def is_set(self) -> bool:
        return self._set


class BatchDownloader:
    def __init__(
        self,
        sources: list[DownloadSource],
        max_workers: int = 8,
        config: Config | None = None,
        browser_source: DownloadSource | None = None,
    ):
        self.sources = sources
        self.max_workers = max_workers
        self.config = config or Config()
        self.browser_source = browser_source

    def _try_source(
        self,
        source: DownloadSource,
        doi: str,
        metadata: PaperMetadata,
        output_dir: Path,
        once: _OnceResult,
        found_urls: list[str],
        urls_lock: threading.Lock,
    ) -> DownloadResult | None:
        """尝试单个源下载。返回结果或 None。"""
        if once.is_set:
            return None

        try:
            url = source.find_pdf_url(doi, metadata)
            if url:
                with urls_lock:
                    found_urls.append(url)
            if not url:
                return None
            if once.is_set:
                return None

            tmp_path = output_dir / f".tmp_{source.name}_{doi.replace('/', '_')}.pdf"
            try:
                if source.download(url, tmp_path) and source.is_pdf(tmp_path):
                    if once.is_set:
                        tmp_path.unlink(missing_ok=True)
                        return None
                    return DownloadResult(
                        doi=doi,
                        success=True,
                        file_path=tmp_path,
                        source_used=source.name,
                    )
                else:
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

        return None

    def _download_one_concurrent(
        self,
        doi: str,
        output_dir: Path,
        metadata_resolver: Callable[[str], PaperMetadata],
    ) -> DownloadResult:
        """并发尝试所有源下载单个 DOI。失败后用 BrowserSource 重试。"""
        try:
            metadata = metadata_resolver(doi)
        except Exception as e:
            return DownloadResult(doi=doi, success=False, error=f"Metadata error: {e}")

        once = _OnceResult()
        winning_result: DownloadResult | None = None
        found_urls: list[str] = []
        urls_lock = threading.Lock()

        # Phase 1: 并发尝试所有非浏览器源
        non_browser = [s for s in self.sources if s is not self.browser_source]
        with ThreadPoolExecutor(max_workers=len(non_browser)) as source_executor:
            futures: list[Future] = []
            for source in non_browser:
                fut = source_executor.submit(
                    self._try_source, source, doi, metadata, output_dir,
                    once, found_urls, urls_lock,
                )
                futures.append(fut)

            for future in as_completed(futures):
                result = future.result()
                if result and result.success and winning_result is None:
                    winning_result = result
                    once.set_if_first(result)
                    for f in futures:
                        f.cancel()

        # Phase 2: 如果 Phase 1 失败且有 BrowserSource，用浏览器重试
        if not winning_result and self.browser_source and found_urls:
            winning_result = self._try_browser_with_urls(
                doi, metadata, output_dir, found_urls,
            )

        # Phase 3: BrowserSource 自己查找 URL
        if not winning_result and self.browser_source:
            winning_result = self._try_browser_find_and_download(
                doi, metadata, output_dir,
            )

        # 清理和重命名
        if winning_result:
            try:
                final_path = rename_pdf(winning_result.file_path, metadata, output_dir)
                winning_result.file_path = final_path
            except (FileNotFoundError, PermissionError):
                winning_result = None

        self._cleanup_tmp(output_dir, doi)

        if winning_result:
            return winning_result
        return DownloadResult(doi=doi, success=False, error="All sources failed")

    def _try_browser_with_urls(
        self,
        doi: str,
        metadata: PaperMetadata,
        output_dir: Path,
        urls: list[str],
    ) -> DownloadResult | None:
        """用 BrowserSource 尝试下载已知 URL。"""
        for url in urls:
            tmp_path = output_dir / f".tmp_browser_{doi.replace('/', '_')}.pdf"
            try:
                if self.browser_source.download(url, tmp_path) and self.browser_source.is_pdf(tmp_path):
                    return DownloadResult(
                        doi=doi,
                        success=True,
                        file_path=tmp_path,
                        source_used="browser",
                    )
                else:
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                tmp_path.unlink(missing_ok=True)
        return None

    def _try_browser_find_and_download(
        self,
        doi: str,
        metadata: PaperMetadata,
        output_dir: Path,
    ) -> DownloadResult | None:
        """用 BrowserSource 自己查找并下载 PDF。"""
        try:
            url = self.browser_source.find_pdf_url(doi, metadata)
            if not url:
                return None

            tmp_path = output_dir / f".tmp_browser_{doi.replace('/', '_')}.pdf"
            if self.browser_source.download(url, tmp_path) and self.browser_source.is_pdf(tmp_path):
                return DownloadResult(
                    doi=doi,
                    success=True,
                    file_path=tmp_path,
                    source_used="browser",
                )
            else:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    def _cleanup_tmp(self, output_dir: Path, doi: str):
        """清理指定 DOI 的所有临时文件。"""
        safe_doi = doi.replace("/", "_")
        for tmp_file in output_dir.glob(f".tmp_*_{safe_doi}.pdf"):
            try:
                tmp_file.unlink(missing_ok=True)
            except PermissionError:
                pass

    def download_all(
        self,
        dois: list[str],
        output_dir: Path,
        metadata_resolver: Callable[[str], PaperMetadata] | None = None,
        progress_callback: Callable[[DownloadResult], None] | None = None,
    ) -> list[DownloadResult]:
        """批量下载所有 DOI。"""
        if metadata_resolver is None:
            from doi_downloader.metadata import resolve_metadata
            metadata_resolver = resolve_metadata

        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[DownloadResult] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._download_one_concurrent, doi, output_dir, metadata_resolver
                ): doi
                for doi in dois
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if progress_callback:
                    progress_callback(result)

        failed = [r for r in results if not r.success]
        if failed:
            failed_file = output_dir / "failed_dois.txt"
            failed_file.write_text("\n".join(r.doi for r in failed) + "\n")

        return results
