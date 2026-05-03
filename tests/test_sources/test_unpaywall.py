import pytest
from pathlib import Path
import httpx
import respx

from doi_downloader.sources.unpaywall import UnpaywallSource
from doi_downloader.metadata import PaperMetadata


@pytest.fixture
def unpaywall() -> UnpaywallSource:
    return UnpaywallSource(email="test@example.com")


class TestUnpaywallFindPdfUrl:
    @respx.mock
    def test_finds_oa_pdf_url(self, unpaywall: UnpaywallSource):
        doi = "10.1234/test"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        respx.get(f"https://api.unpaywall.org/v2/{doi}?email=test@example.com").mock(
            return_value=httpx.Response(200, json={
                "best_oa_location": {
                    "url_for_pdf": "https://example.com/paper.pdf",
                }
            })
        )
        url = unpaywall.find_pdf_url(doi, meta)
        assert url == "https://example.com/paper.pdf"

    @respx.mock
    def test_returns_none_when_no_oa(self, unpaywall: UnpaywallSource):
        doi = "10.1234/test"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        respx.get(f"https://api.unpaywall.org/v2/{doi}?email=test@example.com").mock(
            return_value=httpx.Response(200, json={
                "best_oa_location": None,
            })
        )
        url = unpaywall.find_pdf_url(doi, meta)
        assert url is None

    @respx.mock
    def test_returns_none_on_not_found(self, unpaywall: UnpaywallSource):
        doi = "10.9999/nonexistent"
        meta = PaperMetadata(doi=doi, title="Test", authors=[], year=2024, journal=None)

        respx.get(f"https://api.unpaywall.org/v2/{doi}?email=test@example.com").mock(
            return_value=httpx.Response(404)
        )
        url = unpaywall.find_pdf_url(doi, meta)
        assert url is None


class TestUnpaywallDownload:
    @respx.mock
    def test_download_success(self, unpaywall: UnpaywallSource, tmp_path: Path):
        pdf_content = b"%PDF-1.4 oa content"
        respx.get("https://example.com/paper.pdf").mock(
            return_value=httpx.Response(200, content=pdf_content)
        )
        dest = tmp_path / "out.pdf"
        result = unpaywall.download("https://example.com/paper.pdf", dest)
        assert result is True
        assert dest.read_bytes() == pdf_content

    @respx.mock
    def test_download_non_pdf(self, unpaywall: UnpaywallSource, tmp_path: Path):
        respx.get("https://example.com/paper.pdf").mock(
            return_value=httpx.Response(200, content=b"<html>not pdf</html>")
        )
        dest = tmp_path / "out.pdf"
        result = unpaywall.download("https://example.com/paper.pdf", dest)
        assert result is False
