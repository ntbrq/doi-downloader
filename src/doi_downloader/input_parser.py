import csv
from pathlib import Path


def parse_dois(file_path: Path) -> list[str]:
    """从文件解析 DOI 列表，支持 txt/csv/xlsx，返回去重列表。"""
    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return _parse_txt(file_path)
    elif suffix == ".csv":
        return _parse_csv(file_path)
    elif suffix == ".xlsx":
        return _parse_xlsx(file_path)
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


def _parse_txt(file_path: Path) -> list[str]:
    dois: list[str] = []
    seen: set[str] = set()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if line not in seen:
            seen.add(line)
            dois.append(line)
    return dois


def _parse_csv(file_path: Path) -> list[str]:
    dois: list[str] = []
    seen: set[str] = set()
    with file_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "doi" not in [h.lower() for h in reader.fieldnames]:
            raise ValueError(f"CSV file must have a 'doi' column. Found: {reader.fieldnames}")
        doi_field = next(h for h in reader.fieldnames if h.lower() == "doi")
        for row in reader:
            doi = row[doi_field].strip()
            if doi and doi not in seen:
                seen.add(doi)
                dois.append(doi)
    return dois


def _parse_xlsx(file_path: Path) -> list[str]:
    raise NotImplementedError
