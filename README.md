# doi-downloader

DOI 批量下载工具。根据 DOI 列表批量下载学术论文 PDF，支持多线程、多源回退、Cloudflare 绕过。

## 功能

- 批量下载：从 txt/csv/xlsx 文件读取 DOI 列表
- 多源回退：出版商官网 → Sci-Hub → Unpaywall，自动切换
- 多线程：默认 8 线程并发下载
- 标准化重命名：`作者 - 年份 - 标题.pdf`
- Cloudflare 绕过：Playwright 无头浏览器
- 进度条：tqdm 实时显示

## 安装

```bash
pip install -e ".[dev]"
playwright install chromium
```

## 使用

```bash
# txt 文件（每行一个 DOI）
doi-downloader dois.txt -o ./papers/

# csv 文件（需要 doi 列）
doi-downloader dois.csv -o ./papers/

# Excel 文件（需要 doi 列）
doi-downloader dois.xlsx -o ./papers/

# 指定线程数
doi-downloader dois.txt -o ./papers/ -w 12

# 指定 Sci-Hub 镜像
doi-downloader dois.txt -o ./papers/ --scihub-url https://sci-hub.st
```

## 开发

```bash
# 运行测试
pytest

# 带覆盖率
pytest --cov=doi_downloader
```
