import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from playwright.sync_api import Page

from doi_downloader.sources.scihub import SciHubSource
from doi_downloader.metadata import PaperMetadata


@pytest.fixture
def scihub() -> SciHubSource:
    return SciHubSource()


class TestSciHubFindPdfUrl:
    def test_finds_pdf_url_from_page(self, scihub: SciHubSource):
        doi = "10.1234/test"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        mock_page = MagicMock(spec=Page)
        mock_page.content.return_value = '<iframe src="https://sci-hub.se/downloads/test.pdf"></iframe>'

        with patch.object(scihub, "_get_page", return_value=mock_page):
            url = scihub.find_pdf_url(doi, meta)
            assert url is not None
            assert ".pdf" in url

    def test_returns_none_on_empty_page(self, scihub: SciHubSource):
        doi = "10.1234/test"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        mock_page = MagicMock(spec=Page)
        mock_page.content.return_value = "<html><body>Not found</body></html>"

        with patch.object(scihub, "_get_page", return_value=mock_page):
            url = scihub.find_pdf_url(doi, meta)
            assert url is None


class TestSciHubDownload:
    def test_download_success(self, scihub: SciHubSource, tmp_path: Path):
        pdf_bytes = b"%PDF-1.4 real content"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_bytes.return_value = [pdf_bytes]
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.stream.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("doi_downloader.sources.scihub.httpx.Client", return_value=mock_client):
            dest = tmp_path / "out.pdf"
            result = scihub.download("https://example.com/paper.pdf", dest)
            assert result is True
            assert dest.read_bytes() == pdf_bytes
