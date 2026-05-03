"""Download source abstract base class."""

from abc import ABC, abstractmethod
from pathlib import Path

from doi_downloader.metadata import PaperMetadata


class DownloadSource(ABC):
    """下载源抽象基类。"""

    name: str = "base"

    @abstractmethod
    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """尝试找到 PDF 下载链接，找不到返回 None。"""

    @abstractmethod
    def download(self, url: str, dest: Path) -> bool:
        """下载 PDF 到目标路径。成功返回 True。"""

    def is_pdf(self, file_path: Path) -> bool:
        """验证文件是否为有效 PDF（检查文件头）。"""
        if not file_path.exists() or file_path.stat().st_size < 4:
            return False
        with file_path.open("rb") as f:
            return f.read(4) == b"%PDF"
