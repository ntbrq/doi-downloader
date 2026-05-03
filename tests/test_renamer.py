import pytest
from pathlib import Path

from doi_downloader.renamer import rename_pdf, sanitize_filename
from doi_downloader.metadata import PaperMetadata


def _make_pdf(path: Path, content: bytes = b"%PDF-1.4 test"):
    path.write_bytes(content)


class TestSanitizeFilename:
    def test_removes_special_characters(self):
        result = sanitize_filename('A: B/C\\D<E>F"G|H?I*J')
        assert result == "A BCDEFGHIJ"

    def test_collapses_whitespace(self):
        result = sanitize_filename("hello   world  test")
        assert result == "hello world test"

    def test_truncates_long_names(self):
        result = sanitize_filename("a" * 300, max_length=200)
        assert len(result) <= 200

    def test_strips_trailing_dots_and_spaces(self):
        result = sanitize_filename("test . ")
        assert result == "test"


class TestRenamePdf:
    def test_renames_with_first_author_year_title(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        _make_pdf(src)
        meta = PaperMetadata(
            doi="10.1234/test",
            title="A Study of Things",
            authors=["Zhang Wei", "Li Na"],
            year=2024,
            journal="Nature",
        )
        result = rename_pdf(src, meta, tmp_path)
        assert result.name == "Zhang Wei et al. - 2024 - A Study of Things.pdf"
        assert result.exists()

    def test_renames_single_author_no_et_al(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        _make_pdf(src)
        meta = PaperMetadata(
            doi="10.1234/test",
            title="Solo Work",
            authors=["Zhang Wei"],
            year=2023,
            journal=None,
        )
        result = rename_pdf(src, meta, tmp_path)
        assert result.name == "Zhang Wei - 2023 - Solo Work.pdf"

    def test_renames_no_authors(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        _make_pdf(src)
        meta = PaperMetadata(
            doi="10.1234/test",
            title="Anonymous Paper",
            authors=[],
            year=2024,
            journal=None,
        )
        result = rename_pdf(src, meta, tmp_path)
        assert result.name == "Unknown Author - 2024 - Anonymous Paper.pdf"

    def test_renames_no_year(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        _make_pdf(src)
        meta = PaperMetadata(
            doi="10.1234/test",
            title="Old Paper",
            authors=["A B"],
            year=None,
            journal=None,
        )
        result = rename_pdf(src, meta, tmp_path)
        assert result.name == "A B - n.d. - Old Paper.pdf"

    def test_handles_filename_conflict(self, tmp_path: Path):
        meta = PaperMetadata(
            doi="10.1234/test",
            title="Dup Title",
            authors=["A B"],
            year=2024,
            journal=None,
        )
        src1 = tmp_path / "paper1.pdf"
        _make_pdf(src1)
        result1 = rename_pdf(src1, meta, tmp_path)
        assert result1.name == "A B - 2024 - Dup Title.pdf"

        src2 = tmp_path / "paper2.pdf"
        _make_pdf(src2)
        result2 = rename_pdf(src2, meta, tmp_path)
        assert result2.name == "A B - 2024 - Dup Title (2).pdf"

    def test_handles_chinese_characters(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        _make_pdf(src)
        meta = PaperMetadata(
            doi="10.1234/test",
            title="基于忆阻器的边缘计算研究",
            authors=["张伟", "李娜"],
            year=2024,
            journal=None,
        )
        result = rename_pdf(src, meta, tmp_path)
        assert "忆阻器" in result.name
        assert result.suffix == ".pdf"
