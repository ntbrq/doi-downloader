import threading
from contextlib import contextmanager
from typing import Generator

from playwright.sync_api import sync_playwright, Browser, Page, Playwright


class BrowserPool:
    """线程安全的 Playwright 浏览器页面池。"""

    def __init__(self, pool_size: int = 4):
        self._pool_size = pool_size
        self._lock = threading.Lock()
        self._available: list[Page] = []
        self._all_pages: list[Page] = []
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._initialized = False

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            for _ in range(self._pool_size):
                ctx = self._browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = ctx.new_page()
                self._available.append(page)
                self._all_pages.append(page)
            self._initialized = True

    @contextmanager
    def get_page(self) -> Generator[Page, None, None]:
        """获取一个浏览器页面，用完自动归还。"""
        self._ensure_initialized()
        with self._lock:
            if not self._available:
                raise RuntimeError("No available browser pages in pool")
            page = self._available.pop()
        try:
            yield page
        finally:
            with self._lock:
                self._available.append(page)

    def close(self):
        """关闭所有浏览器资源。"""
        with self._lock:
            for page in self._all_pages:
                try:
                    page.context.close()
                except Exception:
                    pass
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
            self._available.clear()
            self._all_pages.clear()
            self._initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
