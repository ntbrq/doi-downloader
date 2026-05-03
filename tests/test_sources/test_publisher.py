import pytest
from pathlib import Path
import httpx
import respx

from doi_downloader.sources.publisher import PublisherSource
from doi_downloader.metadata import PaperMetadata


@pytest.fixture
def publisher() -> PublisherSource:
    return PublisherSource()


class TestPublisherFindPdfUrl:
    @respx.mock
    def test_finds_pdf_link_from_doi_redirect(self, publisher: PublisherSource):
        doi = "10.1038/s41586-024-07487-w"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        html = '<html><a href="/articles/s41586-024-07487-w.pdf">PDF</a></html>'
        respx.get(f"https://doi.org/{doi}").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )

        url = publisher.find_pdf_url(doi, meta)
        assert url is not None
        assert "pdf" in url.lower()

    @respx.mock
    def test_returns_none_when_no_pdf_link(self, publisher: PublisherSource):
        doi = "10.1234/test"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        html = "<html><p>No PDF available</p></html>"
        respx.get(f"https://doi.org/{doi}").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )

        url = publisher.find_pdf_url(doi, meta)
        assert url is None

    @respx.mock
    def test_returns_none_on_http_error(self, publisher: PublisherSource):
        doi = "10.1234/test"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        respx.get(f"https://doi.org/{doi}").mock(
            return_value=httpx.Response(403)
        )

        url = publisher.find_pdf_url(doi, meta)
        assert url is None


class TestPublisherDownload:
    @respx.mock
    def test_download_success(self, publisher: PublisherSource, tmp_path: Path):
        pdf_content = b"%PDF-1.4 test content"
        respx.get("https://example.com/paper.pdf").mock(
            return_value=httpx.Response(200, content=pdf_content)
        )
        dest = tmp_path / "out.pdf"
        result = publisher.download("https://example.com/paper.pdf", dest)
        assert result is True
        assert dest.read_bytes() == pdf_content

    @respx.mock
    def test_download_non_pdf_content(self, publisher: PublisherSource, tmp_path: Path):
        respx.get("https://example.com/paper.pdf").mock(
            return_value=httpx.Response(200, content=b"<html>not a pdf</html>")
        )
        dest = tmp_path / "out.pdf"
        result = publisher.download("https://example.com/paper.pdf", dest)
        assert result is False
        assert not dest.exists()

    @respx.mock
    def test_download_http_error(self, publisher: PublisherSource, tmp_path: Path):
        respx.get("https://example.com/paper.pdf").mock(
            return_value=httpx.Response(404)
        )
        dest = tmp_path / "out.pdf"
        result = publisher.download("https://example.com/paper.pdf", dest)
        assert result is False
