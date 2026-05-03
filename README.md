# doi-downloader

A batch DOI paper PDF downloader with multi-threaded downloading, multi-source fallback, and Cloudflare bypass.

## Features

- **Batch download** from txt, csv, or xlsx files containing DOI lists
- **Multi-source fallback** — tries Publisher site, then Sci-Hub, then Unpaywall automatically
- **Multi-threaded** — default 8 concurrent download threads
- **Standardized renaming** — outputs `Author - Year - Title.pdf`
- **Cloudflare bypass** — uses Playwright headless browser for JS challenges
- **Progress bar** — real-time tqdm progress with per-DOI status

## Installation

### pip (recommended)

```bash
git clone https://github.com/rdbao/doi-downloader.git
cd doi-downloader
pip install -e ".[dev]"
playwright install chromium
```

### conda

```bash
git clone https://github.com/rdbao/doi-downloader.git
cd doi-downloader
conda env create -f environment.yml
conda activate doi-downloader
playwright install chromium
```

## Quick Start

```bash
# From a text file (one DOI per line)
doi-downloader dois.txt -o ./papers/

# From a CSV file (requires a 'doi' column)
doi-downloader dois.csv -o ./papers/

# From an Excel file (requires a 'doi' column)
doi-downloader dois.xlsx -o ./papers/
```

## Usage

```
doi-downloader [OPTIONS] INPUT_FILE

Options:
  -o, --output PATH    Output directory for PDFs  [required]
  -w, --workers INT    Number of download threads  [default: 8]
  --scihub-url TEXT    Sci-Hub mirror URL  [default: https://sci-hub.se]
  --email TEXT         Email for Unpaywall API  [default: user@example.com]
  --help               Show this message and exit.
```

### Input Formats

**txt** — one DOI per line:
```
10.1038/s41586-024-07487-w
10.1126/science.adp1234
# comments and blank lines are ignored
```

**csv** — must have a `doi` column (case-insensitive):
```csv
doi,tag
10.1038/s41586-024-07487-w,nature_paper
10.1126/science.adp1234,science_paper
```

**xlsx** — must have a `doi` column (case-insensitive):

Same structure as csv, first row is the header.

### Examples

```bash
# Use 12 threads
doi-downloader dois.txt -o ./papers/ -w 12

# Use a different Sci-Hub mirror
doi-downloader dois.txt -o ./papers/ --scihub-url https://sci-hub.st
```

## Architecture

```
Input File
  │
  ▼
parse_dois()          — parse txt/csv/xlsx → DOI list
  │
  ▼
resolve_metadata()    — CrossRef API → PaperMetadata (title, authors, year, journal)
  │                    [ThreadPoolExecutor, 10 threads]
  ▼
download_all()        — for each DOI, try sources in order:
  │                    1. Publisher (DOI redirect → find PDF link)
  │                    2. Sci-Hub  (Playwright → parse iframe)
  │                    3. Unpaywall (OA API → PDF URL)
  │                    [ThreadPoolExecutor, 8 threads]
  ▼
rename_pdf()          — "Author - Year - Title.pdf"
  │
  ▼
Output Directory      — PDFs + failed_dois.txt (if any)
```

### Modules

| Module | Responsibility |
|--------|---------------|
| `input_parser.py` | Parse DOI lists from txt/csv/xlsx |
| `metadata.py` | Resolve DOI metadata via CrossRef API |
| `renamer.py` | Rename PDFs to standardized format |
| `browser.py` | Thread-safe Playwright browser pool |
| `downloader.py` | Thread pool download orchestration with fallback |
| `sources/publisher.py` | Download from publisher websites |
| `sources/scihub.py` | Download from Sci-Hub |
| `sources/unpaywall.py` | Download from Unpaywall OA |
| `cli.py` | CLI entry point with tqdm progress bar |
| `config.py` | Configuration dataclass |

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run with coverage
pytest --cov=doi_downloader

# Run a specific test file
pytest tests/test_renamer.py -v
```

## License

MIT
