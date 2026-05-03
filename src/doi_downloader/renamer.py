"""PDF 重命名：根据元数据生成 '作者 - 年份 - 标题.pdf' 格式文件名。"""

import re
from pathlib import Path

from doi_downloader.metadata import PaperMetadata


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """移除文件名中的非法字符，压缩空白，截断长度。"""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    return name


def rename_pdf(file_path: Path, metadata: PaperMetadata, output_dir: Path) -> Path:
    """重命名 PDF 为 '作者 - 年份 - 标题.pdf' 格式。

    Args:
        file_path: 源 PDF 文件路径。
        metadata: 论文元数据。
        output_dir: 输出目录。

    Returns:
        重命名后的文件路径。

    Raises:
        FileNotFoundError: 源文件不存在。
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Source file not found: {file_path}")

    if not metadata.authors:
        author_part = "Unknown Author"
    elif len(metadata.authors) == 1:
        author_part = metadata.authors[0]
    else:
        author_part = f"{metadata.authors[0]} et al."

    year_part = str(metadata.year) if metadata.year else "n.d."
    title_part = metadata.title

    base_name = sanitize_filename(f"{author_part} - {year_part} - {title_part}")
    target = output_dir / f"{base_name}.pdf"

    if target.exists():
        counter = 2
        while True:
            candidate = output_dir / f"{base_name} ({counter}).pdf"
            if not candidate.exists():
                target = candidate
                break
            counter += 1

    file_path.rename(target)
    return target
