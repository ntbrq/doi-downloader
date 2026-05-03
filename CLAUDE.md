# doi-downloader

DOI 批量下载工具。通过 DOI 批量下载学术论文 PDF。

## 开发命令

```bash
# 安装依赖
pip install -e ".[dev]"
playwright install chromium

# 运行测试
pytest

# 运行单个测试
pytest tests/test_renamer.py -v

# 带覆盖率
pytest --cov=doi_downloader
```

## 架构

- `input_parser.py`: 解析 DOI 列表（txt/csv/xlsx）
- `metadata.py`: CrossRef API 解析元数据
- `renamer.py`: PDF 重命名（作者-年份-标题.pdf）
- `browser.py`: Playwright 浏览器池
- `downloader.py`: 线程池下载调度 + 多源回退
- `sources/`: 下载源实现（publisher, scihub, unpaywall）
- `cli.py`: CLI 入口

## TDD

所有功能先写测试再实现。测试用 mock，不依赖网络。
