import pytest
from pathlib import Path
from click.testing import CliRunner

from doi_downloader.cli import main


class TestCli:
    def test_missing_input_file(self):
        runner = CliRunner()
        result = runner.invoke(main, ["nonexistent.txt", "-o", "/tmp/out"])
        assert result.exit_code != 0

    def test_unsupported_format(self, tmp_path: Path):
        f = tmp_path / "test.json"
        f.write_text("{}")
        runner = CliRunner()
        result = runner.invoke(main, [str(f), "-o", str(tmp_path / "out")])
        assert result.exit_code != 0
        assert "Unsupported" in result.output or "format" in result.output.lower()

    def test_empty_doi_list(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        runner = CliRunner()
        result = runner.invoke(main, [str(f), "-o", str(tmp_path / "out")])
        assert result.exit_code == 0
        assert "No DOIs" in result.output or "0" in result.output
