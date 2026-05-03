"""批量下载调度器：线程池 + 多源回退。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


class BatchDownloader:
    def __init__(
        self,
        sources: list[DownloadSource],
        max_workers: int = 8,
        config: Config | None = None,
    ):
        self.sources = sources
        self.max_workers = max_workers
        self.config = config or Config()

    def _download_one(
        self,
        doi: str,
        output_dir: Path,
        metadata_resolver: Callable[[str], PaperMetadata],
    ) -> DownloadResult:
        """下载单个 DOI，依次尝试所有源。"""
        try:
            metadata = metadata_resolver(doi)
        except Exception as e:
            return DownloadResult(doi=doi, success=False, error=f"Metadata error: {e}")

        for source in self.sources:
            try:
                url = source.find_pdf_url(doi, metadata)
                if not url:
                    continue

                tmp_path = output_dir / f".tmp_{doi.replace('/', '_')}.pdf"
                if source.download(url, tmp_path):
                    if source.is_pdf(tmp_path):
                        final_path = rename_pdf(tmp_path, metadata, output_dir)
                        return DownloadResult(
                            doi=doi,
                            success=True,
                            file_path=final_path,
                            source_used=source.name,
                        )
                    else:
                        tmp_path.unlink(missing_ok=True)
            except Exception:
                continue

        return DownloadResult(doi=doi, success=False, error="All sources failed")

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
                executor.submit(self._download_one, doi, output_dir, metadata_resolver): doi
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
