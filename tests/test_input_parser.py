from pathlib import Path
import pytest
from openpyxl import Workbook

from doi_downloader.input_parser import parse_dois


class TestParseTxt:
    def test_parse_txt_file(self, fixtures_dir: Path):
        dois = parse_dois(fixtures_dir / "dois.txt")
        assert len(dois) == 3
        assert "10.1038/s41586-024-07487-w" in dois

    def test_parse_txt_strips_whitespace(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("  10.1234/test  \n10.5678/test2\n")
        dois = parse_dois(f)
        assert dois == ["10.1234/test", "10.5678/test2"]

    def test_parse_txt_skips_empty_lines(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("10.1234/test\n\n\n10.5678/test2\n")
        dois = parse_dois(f)
        assert len(dois) == 2

    def test_parse_txt_deduplicates(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("10.1234/test\n10.1234/test\n10.5678/test2\n")
        dois = parse_dois(f)
        assert len(dois) == 2

    def test_parse_txt_skips_comments(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("# comment\n10.1234/test\n// another comment\n")
        dois = parse_dois(f)
        assert dois == ["10.1234/test"]


class TestParseCsv:
    def test_parse_csv_with_doi_column(self, fixtures_dir: Path):
        dois = parse_dois(fixtures_dir / "dois.csv")
        assert len(dois) == 2
        assert "10.1038/s41586-024-07487-w" in dois

    def test_parse_csv_deduplicates(self, tmp_path: Path):
        f = tmp_path / "test.csv"
        f.write_text("doi,tag\n10.1234/test,a\n10.1234/test,b\n")
        dois = parse_dois(f)
        assert len(dois) == 1

    def test_parse_csv_missing_doi_column_raises(self, tmp_path: Path):
        f = tmp_path / "test.csv"
        f.write_text("id,name\n1,foo\n")
        with pytest.raises(ValueError, match="doi"):
            parse_dois(f)


class TestParseXlsx:
    def _create_xlsx(self, path: Path, rows: list[list[str]]):
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        wb.save(path)

    def test_parse_xlsx_with_doi_column(self, tmp_path: Path):
        f = tmp_path / "test.xlsx"
        self._create_xlsx(f, [["doi", "tag"], ["10.1234/test", "a"], ["10.5678/test2", "b"]])
        dois = parse_dois(f)
        assert len(dois) == 2
        assert "10.1234/test" in dois

    def test_parse_xlsx_deduplicates(self, tmp_path: Path):
        f = tmp_path / "test.xlsx"
        self._create_xlsx(f, [["doi"], ["10.1234/test"], ["10.1234/test"]])
        dois = parse_dois(f)
        assert len(dois) == 1

    def test_parse_xlsx_skips_empty_rows(self, tmp_path: Path):
        f = tmp_path / "test.xlsx"
        self._create_xlsx(f, [["doi"], ["10.1234/test"], [""]])
        dois = parse_dois(f)
        assert len(dois) == 1
