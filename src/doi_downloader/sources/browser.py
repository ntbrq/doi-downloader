"""基于 Playwright 的下载源。处理 JS 渲染页面和 Cloudflare 验证。

Zotero 模式：自动查找 + 人工辅助认证
1. 先用 headless 模式尝试（完全自动）
2. 遇到 Cloudflare 反爬时，弹出可见浏览器窗口
3. 用户手动完成一次认证
4. 认证后自动继续下载，cookie 持久化复用
"""

import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from doi_downloader.metadata import PaperMetadata
from doi_downloader.sources.base import DownloadSource


class BrowserSource(DownloadSource):
    """通过 Playwright 浏览器下载 PDF。支持人工辅助 Cloudflare 认证。"""

    name = "browser"

    REALISTIC_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.6478.127 Safari/537.36"
    )

    STEALTH_SCRIPT = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
        delete navigator.__proto__.webdriver;
    """

    def __init__(self, timeout: float = 60.0, use_system_proxy: bool = False):
        self.timeout = timeout
        self.use_system_proxy = use_system_proxy
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._headed_browser = None
        # 持久化 context，保存人工认证后的 cookie
        self._persistent_ctx = None
        self._headed_mode_used = False
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _detect_proxy(self) -> str | None:
        if not self.use_system_proxy:
            return None
        try:
            proxies = urllib.request.getproxies()
            return proxies.get("http") or proxies.get("https")
        except Exception:
            return None

    def _ensure_playwright(self):
        if self._pw is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()

    def _ensure_browser(self):
        if self._browser is not None:
            return
        self._ensure_playwright()

        launch_options = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-popup-blocking",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        proxy_url = self._detect_proxy()
        if proxy_url:
            launch_options["proxy"] = {"server": proxy_url}
            print(f"  Browser proxy: {proxy_url}")
        else:
            launch_options["args"].append("--no-proxy-server")

        self._browser = self._pw.chromium.launch(**launch_options)

    def _launch_headed_browser(self):
        """启动可见浏览器窗口（用于人工辅助认证）。"""
        self._ensure_playwright()

        launch_options = {
            "headless": False,  # 可见窗口！
            "slow_mo": 100,     # 稍微减速，让用户能看到操作
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1280,900",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        proxy_url = self._detect_proxy()
        if proxy_url:
            launch_options["proxy"] = {"server": proxy_url}

        return self._pw.chromium.launch(**launch_options)

    def _close_in_thread(self):
        if self._persistent_ctx:
            try:
                self._persistent_ctx.close()
            except Exception:
                pass
            self._persistent_ctx = None
        if self._headed_browser:
            try:
                self._headed_browser.close()
            except Exception:
                pass
            self._headed_browser = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def close(self):
        try:
            self._executor.submit(self._close_in_thread).result(timeout=10)
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        future = self._executor.submit(self._find_pdf_url_in_thread, doi)
        try:
            return future.result(timeout=self.timeout * 3)
        except Exception:
            return None

    def _create_context(self, browser=None):
        """创建浏览器上下文。"""
        target = browser or self._browser
        ctx = target.new_context(
            user_agent=self.REALISTIC_UA,
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        ctx.add_init_script(self.STEALTH_SCRIPT)
        return ctx

    # ── Cloudflare 检测 ──────────────────────────────────────────

    def _is_blocked(self, page) -> bool:
        """检测页面是否被反爬系统拦截。"""
        try:
            html = page.content()
            text = page.text_content("body") or ""

            # Cloudflare 挑战页面
            if any(kw in html for kw in [
                "cf-browser-verification", "cf_chl_opt",
                "challenge-platform", "cf-turnstile",
                "Just a moment", "Checking your browser",
            ]):
                return True

            # 明确的 IP 封锁
            if "CLOUDFLARE_ERROR" in text or "IP+blocked" in text:
                return True

            return False
        except Exception:
            return False

    def _wait_for_cloudflare_auto(self, page, max_wait: int = 15) -> bool:
        """等待 Cloudflare 自动通过（headless 模式下有时能自动解决简单挑战）。
        返回 True 表示已通过，False 表示需要人工干预。"""
        for _ in range(max_wait):
            if not self._is_blocked(page):
                return True
            page.wait_for_timeout(1000)
        return False

    # ── 人工辅助认证（Zotero 模式）────────────────────────────────

    def _human_assisted_auth(self, url: str) -> bool:
        """弹出可见浏览器窗口，等待用户手动完成 Cloudflare 认证。
        认证成功后保存 cookie 供后续 headless 请求复用。

        Returns:
            True 表示认证成功，False 表示用户放弃或超时
        """
        print(f"\n  ╔══════════════════════════════════════════════════════╗")
        print(f"  ║  需要人工辅助认证                                    ║")
        print(f"  ║  浏览器窗口即将打开，请完成 Cloudflare 验证          ║")
        print(f"  ║  认证完成后程序会自动继续下载                        ║")
        print(f"  ╚══════════════════════════════════════════════════════╝")
        print(f"  目标: {url}")

        try:
            self._headed_browser = self._launch_headed_browser()
            ctx = self._create_context(self._headed_browser)
            page = ctx.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))

            # 等待用户完成认证（最多 5 分钟）
            print(f"  等待认证中...（最多 5 分钟）")
            for i in range(300):  # 5 分钟
                if not self._is_blocked(page):
                    print(f"  ✓ 认证成功！")
                    # 保存 cookie 到持久化 context
                    cookies = ctx.cookies()
                    self._save_cookies(cookies, ctx)
                    page.close()
                    ctx.close()
                    self._headed_browser.close()
                    self._headed_browser = None
                    self._headed_mode_used = True
                    return True
                page.wait_for_timeout(1000)

            print(f"  ✗ 认证超时")
            page.close()
            ctx.close()
            self._headed_browser.close()
            self._headed_browser = None
            return False

        except Exception as e:
            print(f"  ✗ 认证失败: {e}")
            if self._headed_browser:
                try:
                    self._headed_browser.close()
                except Exception:
                    pass
                self._headed_browser = None
            return False

    def _save_cookies(self, cookies: list, source_ctx):
        """保存 cookie 到持久化 context，供后续 headless 请求复用。"""
        # 创建持久化 context（如果不存在）
        if self._persistent_ctx is None:
            self._ensure_browser()
            self._persistent_ctx = self._create_context()

        # 将 cookie 注入到持久化 context
        if cookies:
            try:
                self._persistent_ctx.add_cookies(cookies)
            except Exception:
                pass

    def _get_context(self):
        """获取浏览器 context，优先使用持久化 context（含认证 cookie）。"""
        if self._persistent_ctx:
            return self._persistent_ctx
        return self._create_context()

    # ── PDF URL 查找 ─────────────────────────────────────────────

    def _find_pdf_url_in_thread(self, doi: str) -> str | None:
        self._ensure_browser()

        # Phase 1: 尝试 headless 模式（完全自动）
        ctx = self._create_context()
        try:
            page = ctx.new_page()

            doi_url = f"https://doi.org/{doi}"
            url = self._try_find_pdf(page, doi_url)
            if url:
                return url

            for pub_url in self._build_publisher_urls(doi):
                url = self._try_find_pdf(page, pub_url)
                if url:
                    return url

            # headless 模式被拦截，检查是否需要人工认证
            page2 = ctx.new_page()
            page2.goto(doi_url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))

            if self._is_blocked(page2):
                page2.close()
                page.close()
                ctx.close()

                # Phase 2: 弹出可见浏览器，等待人工认证
                auth_ok = self._human_assisted_auth(doi_url)
                if not auth_ok:
                    return None

                # Phase 3: 用认证后的 cookie 重试 headless 模式
                ctx2 = self._get_context()
                page3 = ctx2.new_page()
                try:
                    url = self._try_find_pdf(page3, doi_url)
                    if url:
                        return url

                    for pub_url in self._build_publisher_urls(doi):
                        url = self._try_find_pdf(page3, pub_url)
                        if url:
                            return url
                finally:
                    page3.close()
                    if ctx2 is not self._persistent_ctx:
                        ctx2.close()

                return None

            page2.close()
            return None

        except Exception:
            return None
        finally:
            page.close()
            ctx.close()

    def _try_find_pdf(self, page, url: str) -> str | None:
        """访问 URL，尝试查找 PDF 链接。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare_auto(page, max_wait=10)
            page.wait_for_timeout(5000)

            if self._is_blocked(page):
                return None

            if self._page_is_pdf_viewer(page):
                return url

            pdf_url = self._try_click_pdf_button(page, url)
            if pdf_url:
                return pdf_url

            return self._extract_pdf_from_page(page, url)
        except Exception:
            return None

    def _page_is_pdf_viewer(self, page) -> bool:
        try:
            for selector in ["embed[type='application/pdf']", "iframe[src*='.pdf']"]:
                if page.query_selector_all(selector):
                    return True
            if page.url.lower().endswith(".pdf"):
                return True
        except Exception:
            pass
        return False

    def _try_click_pdf_button(self, page, base_url: str) -> str | None:
        selectors = [
            "a.accessbar-tooltip-link",
            "a[data-aa-name='view-pdf']",
            "a:has-text('View PDF')",
            "a:has-text('Download PDF')",
            "a:has-text('PDF')",
            "a[href*='pdfdirect']",
            "a[href*='/pdf/']",
        ]

        for selector in selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    href = btn.get_attribute("href")
                    if href and (".pdf" in href.lower() or "/pdf" in href.lower()):
                        return urljoin(base_url, href)
                    btn.click()
                    page.wait_for_timeout(3000)
                    if self._page_is_pdf_viewer(page):
                        return page.url
                    pdf_url = self._extract_pdf_from_page(page, base_url)
                    if pdf_url:
                        return pdf_url
            except Exception:
                continue
        return None

    def _extract_pdf_from_page(self, page, base_url: str) -> str | None:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
            url = meta.get("content")
            if url:
                return url

        url = self._extract_publisher_specific(soup, base_url, html)
        if url:
            return url

        pdf_candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            if any(skip in href_lower for skip in ["signup", "login", "register", "help", "faq", "cookie"]):
                continue
            if ".pdf" in href_lower or "/pdf" in href_lower or "getpdf" in href_lower:
                pdf_candidates.append(urljoin(base_url, href))

        if pdf_candidates:
            return max(pdf_candidates, key=len)

        for tag_name in ["iframe", "embed"]:
            for tag in soup.find_all(tag_name, src=True):
                src = tag["src"].lower()
                if ".pdf" in src or "pdf" in src:
                    return urljoin(base_url, tag["src"])

        return None

    def _extract_publisher_specific(self, soup: BeautifulSoup, base_url: str, html: str) -> str | None:
        if "ieeexplore.ieee.org" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/document/" in href and "/pdf" in href.lower():
                    return urljoin(base_url, href)
            for a in soup.find_all("a", attrs={"class": re.compile(r"pdf", re.I)}):
                href = a.get("href", "")
                if href:
                    return urljoin(base_url, href)

        if "sciencedirect.com" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/pdf/" in href or "reader" in href.lower():
                    return urljoin(base_url, href)
            for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
                return meta.get("content")

        if "onlinelibrary.wiley.com" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "pdfdirect" in href or "/epdf/" in href:
                    return urljoin(base_url, href)

        if "nature.com" in base_url or "springer.com" in base_url:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href and ("article" in href or "chapter" in href):
                    return urljoin(base_url, href)

        return None

    def _build_publisher_urls(self, doi: str) -> list[str]:
        urls: list[str] = []
        if doi.startswith("10.1109/"):
            urls.append(f"https://ieeexplore.ieee.org/document/{doi.split('/')[-1]}")
        if doi.startswith("10.1016/"):
            article_id = doi.split("/", 1)[1]
            urls.append(f"https://www.sciencedirect.com/science/article/pii/{article_id}")
        if doi.startswith("10.3389/"):
            urls.append(f"https://www.frontiersin.org/articles/{doi}")
        if doi.startswith("10.2139/"):
            ssrn_id = doi.split(".")[-1]
            urls.append(f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_id}")
        return urls

    # ── PDF 下载 ─────────────────────────────────────────────────

    def download(self, url: str, dest: Path) -> bool:
        future = self._executor.submit(self._download_in_thread, url, dest)
        try:
            return future.result(timeout=self.timeout * 3)
        except Exception:
            return False

    def _download_in_thread(self, url: str, dest: Path) -> bool:
        # 优先使用含认证 cookie 的 context
        ctx = self._get_context()
        should_close_ctx = ctx is not self._persistent_ctx

        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare_auto(page, max_wait=10)

            if self._is_blocked(page):
                # 需要人工认证
                auth_ok = self._human_assisted_auth(url)
                if not auth_ok:
                    return False
                # 用认证后的 cookie 重试
                ctx2 = self._get_context()
                page.close()
                page = ctx2.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                self._wait_for_cloudflare_auto(page, max_wait=10)

            if self._page_is_pdf_viewer(page):
                return self._download_with_context(page, url, dest)

            pdf_url = self._extract_pdf_from_page(page, url)
            if pdf_url:
                return self._download_with_context(page, pdf_url, dest)

            return self._try_click_download(page, dest)

        except Exception:
            return False
        finally:
            page.close()
            if should_close_ctx:
                ctx.close()

    def _download_with_context(self, page, url: str, dest: Path) -> bool:
        """使用浏览器上下文下载 PDF（携带 cookie）。"""
        try:
            response = page.request.get(url, timeout=int(self.timeout * 1000))
            if not response.ok:
                return False

            content_type = response.headers.get("content-type", "")
            if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
                return False

            dest.parent.mkdir(parents=True, exist_ok=True)
            body = response.body()

            if not body.startswith(b"%PDF"):
                dest.unlink(missing_ok=True)
                return False

            dest.write_bytes(body)
            if not self.is_pdf(dest):
                dest.unlink(missing_ok=True)
                return False
            return True

        except Exception:
            dest.unlink(missing_ok=True)
            return False

    def _try_click_download(self, page, dest: Path) -> bool:
        selectors = [
            "a[href*='.pdf']",
            "a[href*='/pdf']",
            "a[href*='getpdf']",
            "a[href*='download']",
            "a.download",
            "a[title*='PDF']",
            "a[aria-label*='PDF']",
            "button:has-text('Download PDF')",
            "button:has-text('PDF')",
        ]

        for selector in selectors:
            try:
                element = page.query_selector(selector)
                if element:
                    with page.expect_download(timeout=30000) as download_info:
                        element.click()
                    download = download_info.value
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    download.save_as(str(dest))
                    if self.is_pdf(dest):
                        return True
                    else:
                        dest.unlink(missing_ok=True)
            except Exception:
                continue
        return False
