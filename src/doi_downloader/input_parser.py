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
    raise NotImplementedError


def _parse_xlsx(file_path: Path) -> list[str]:
    raise NotImplementedError
