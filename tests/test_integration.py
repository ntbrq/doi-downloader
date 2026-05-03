"""End-to-end integration test (mock mode, no network required)."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner
from doi_downloader.cli import main


class TestIntegration:
    def test_full_flow_mock(self, tmp_path: Path):
        """端到端测试：模拟完整流程（无需网络）。"""
        input_file = tmp_path / "dois.txt"
        input_file.write_text("10.1234/test1\n10.1234/test2\n")
        output_dir = tmp_path / "output"

        from doi_downloader.metadata import PaperMetadata

        def mock_resolve(doi: str) -> PaperMetadata:
            return PaperMetadata(
                doi=doi,
                title=f"Paper about {doi}",
                authors=["Zhang Wei"],
                year=2024,
                journal="Test Journal",
            )

        from doi_downloader.sources.base import DownloadSource

        class MockSource(DownloadSource):
            name = "mock"

            def find_pdf_url(self, doi, metadata):
                return f"https://example.com/{doi}.pdf"

            def download(self, url, dest):
                dest.write_bytes(b"%PDF-1.4 mock content")
                return True

        with patch("doi_downloader.cli.resolve_metadata", side_effect=mock_resolve), \
             patch("doi_downloader.cli.PublisherSource", return_value=MockSource()), \
             patch("doi_downloader.cli.SciHubSource", return_value=MockSource()), \
             patch("doi_downloader.cli.UnpaywallSource", return_value=MockSource()):

            runner = CliRunner()
            result = runner.invoke(main, [str(input_file), "-o", str(output_dir)])

            assert result.exit_code == 0
            assert "2 downloaded" in result.output

            pdfs = list(output_dir.glob("*.pdf"))
            assert len(pdfs) == 2
            assert any("Zhang Wei" in p.name for p in pdfs)
