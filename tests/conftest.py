import pytest
from pathlib import Path


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_output(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir()
    return output
