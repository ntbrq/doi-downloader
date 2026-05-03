"""CLI entry point for doi-downloader."""

import sys
from pathlib import Path

import click
from tqdm import tqdm

from doi_downloader.config import Config
from doi_downloader.downloader import BatchDownloader, DownloadResult
from doi_downloader.input_parser import parse_dois
from doi_downloader.metadata import resolve_metadata
from doi_downloader.sources.publisher import PublisherSource
from doi_downloader.sources.scihub import SciHubSource
from doi_downloader.sources.unpaywall import UnpaywallSource


@click.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_dir", type=click.Path(path_type=Path), required=True, help="Output directory for PDFs")
@click.option("-w", "--workers", type=int, default=8, help="Number of download threads")
@click.option("--scihub-url", default="https://sci-hub.se", help="Sci-Hub mirror URL")
@click.option("--email", default="user@example.com", help="Email for Unpaywall API")
def main(input_file: Path, output_dir: Path, workers: int, scihub_url: str, email: str):
    """DOI 批量下载工具。根据 DOI 列表批量下载学术论文 PDF。"""
    click.echo(f"Reading DOIs from {input_file}...")
    try:
        dois = parse_dois(input_file)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not dois:
        click.echo("No DOIs found in input file.")
        sys.exit(0)

    click.echo(f"Found {len(dois)} unique DOIs.")

    sources = [
        PublisherSource(timeout=30.0),
        SciHubSource(base_url=scihub_url, timeout=60.0),
        UnpaywallSource(email=email, timeout=30.0),
    ]

    config = Config(max_download_workers=workers, scihub_url=scihub_url, unpaywall_email=email)
    downloader = BatchDownloader(sources=sources, max_workers=workers, config=config)

    pbar = tqdm(total=len(dois), desc="Downloading", unit="paper")

    def on_progress(result: DownloadResult):
        pbar.update(1)
        if result.success:
            pbar.set_postfix_str(f"OK: {result.source_used}")
        else:
            pbar.set_postfix_str(f"FAIL: {result.error}")

    results = downloader.download_all(
        dois, output_dir, metadata_resolver=resolve_metadata, progress_callback=on_progress
    )
    pbar.close()

    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    click.echo(f"\nDone: {len(success)} downloaded, {len(failed)} failed.")
    if failed:
        failed_file = output_dir / "failed_dois.txt"
        click.echo(f"Failed DOIs saved to: {failed_file}")
