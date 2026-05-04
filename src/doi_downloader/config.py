from dataclasses import dataclass


@dataclass
class Config:
    max_download_workers: int = 8
    max_metadata_workers: int = 10
    browser_pool_size: int = 4
    request_timeout: float = 30.0
    browser_timeout: float = 60.0
    user_agent: str = "doi-downloader/0.1 (mailto:user@example.com)"
    scihub_url: str = "https://sci-hub.se"
    unpaywall_email: str = "doi-downloader@github.com"
