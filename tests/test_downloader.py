import pytest
from pathlib import Path
from unittest.mock import MagicMock

from doi_downloader.downloader import BatchDownloader, DownloadResult
from doi_downloader.sources.base import DownloadSource
from doi_downloader.metadata import PaperMetadata


class FakeSource(DownloadSource):
    """测试用的假下载源。"""

    def __init__(self, name: str, should_succeed: bool = True):
        self.name = name
        self.should_succeed = should_succeed

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        if self.should_succeed:
            return f"https://example.com/{doi}.pdf"
        return None

    def download(self, url: str, dest: Path) -> bool:
        if self.should_succeed:
            dest.write_bytes(b"%PDF-1.4 test")
            return True
        return False


class TestBatchDownloader:
    def test_download_all_success(self, tmp_path: Path):
        source = FakeSource("fake", should_succeed=True)
        downloader = BatchDownloader(sources=[source], max_workers=2)

        def mock_resolve(doi: str) -> PaperMetadata:
            return PaperMetadata(doi=doi, title=f"Paper {doi}", authors=["A B"], year=2024, journal=None)

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        results = downloader.download_all(["10.1234/a", "10.1234/b"], output_dir, metadata_resolver=mock_resolve)

        assert len(results) == 2
        assert all(r.success for r in results)
        assert all(r.source_used == "fake" for r in results)

    def test_download_fallback_to_second_source(self, tmp_path: Path):
        fail_source = FakeSource("fail", should_succeed=False)
        ok_source = FakeSource("ok", should_succeed=True)
        downloader = BatchDownloader(sources=[fail_source, ok_source], max_workers=2)

        def mock_resolve(doi: str) -> PaperMetadata:
            return PaperMetadata(doi=doi, title=f"Paper {doi}", authors=["A B"], year=2024, journal=None)

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        results = downloader.download_all(["10.1234/test"], output_dir, metadata_resolver=mock_resolve)

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].source_used == "ok"

    def test_download_all_sources_fail(self, tmp_path: Path):
        fail_source = FakeSource("fail1", should_succeed=False)
        fail_source2 = FakeSource("fail2", should_succeed=False)
        downloader = BatchDownloader(sources=[fail_source, fail_source2], max_workers=2)

        def mock_resolve(doi: str) -> PaperMetadata:
            return PaperMetadata(doi=doi, title=f"Paper {doi}", authors=["A B"], year=2024, journal=None)

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        results = downloader.download_all(["10.1234/test"], output_dir, metadata_resolver=mock_resolve)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].source_used is None

    def test_metadata_failure_skips_doi(self, tmp_path: Path):
        source = FakeSource("fake", should_succeed=True)
        downloader = BatchDownloader(sources=[source], max_workers=2)

        def mock_resolve(doi: str) -> PaperMetadata:
            raise ValueError("DOI not found")

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        results = downloader.download_all(["10.1234/test"], output_dir, metadata_resolver=mock_resolve)

        assert len(results) == 1
        assert results[0].success is False
        assert "metadata" in results[0].error.lower()

    def test_writes_failed_dois_file(self, tmp_path: Path):
        fail_source = FakeSource("fail", should_succeed=False)
        downloader = BatchDownloader(sources=[fail_source], max_workers=2)

        def mock_resolve(doi: str) -> PaperMetadata:
            return PaperMetadata(doi=doi, title="T", authors=["A"], year=2024, journal=None)

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        downloader.download_all(["10.1234/a", "10.1234/b"], output_dir, metadata_resolver=mock_resolve)

        failed_file = output_dir / "failed_dois.txt"
        assert failed_file.exists()
        content = failed_file.read_text()
        assert "10.1234/a" in content
        assert "10.1234/b" in content
