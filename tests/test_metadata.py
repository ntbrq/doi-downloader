import pytest
import httpx
import respx

from doi_downloader.metadata import PaperMetadata, resolve_metadata


class TestResolveMetadata:
    @respx.mock
    def test_resolve_metadata_success(self):
        doi = "10.1038/s41586-024-07487-w"
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(200, json={
                "message": {
                    "title": ["A test paper title"],
                    "author": [
                        {"given": "Wei", "family": "Zhang"},
                        {"given": "Na", "family": "Li"},
                    ],
                    "published-print": {"date-parts": [[2024]]},
                    "container-title": ["Nature"],
                }
            })
        )
        meta = resolve_metadata(doi)
        assert meta.doi == doi
        assert meta.title == "A test paper title"
        assert meta.authors == ["Zhang Wei", "Li Na"]
        assert meta.year == 2024
        assert meta.journal == "Nature"

    @respx.mock
    def test_resolve_metadata_no_year(self):
        doi = "10.1234/test"
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(200, json={
                "message": {
                    "title": ["No year paper"],
                    "author": [{"given": "A", "family": "B"}],
                }
            })
        )
        meta = resolve_metadata(doi)
        assert meta.year is None
        assert meta.journal is None

    @respx.mock
    def test_resolve_metadata_not_found(self):
        doi = "10.9999/nonexistent"
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(ValueError, match="not found"):
            resolve_metadata(doi)

    @respx.mock
    def test_resolve_metadata_empty_title(self):
        doi = "10.1234/test"
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(200, json={
                "message": {
                    "title": [],
                    "author": [{"given": "A", "family": "B"}],
                }
            })
        )
        meta = resolve_metadata(doi)
        assert meta.title == "Untitled"
