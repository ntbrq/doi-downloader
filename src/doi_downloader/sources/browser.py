"""基于 Playwright 的下载源。处理 JS 渲染页面和 Cloudflare 验证。

设计原则：
- 自包含：使用 Playwright 自带的 Chromium，不依赖系统浏览器
- 智能代理：自动检测但默认绕过系统代理（避免被封锁的代理 IP）
- 环境自适应：自动检测 VPN/校园网环境
- 优雅降级：BrowserSource 失败时回退到其他源
"""

import os
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
    """通过 Playwright 无头浏览器下载 PDF。处理 Cloudflare 和 JS 渲染页面。"""

    name = "browser"

    # 真实的 Chrome User Agent（不包含 HeadlessChrome 标记）
    REALISTIC_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.6478.127 Safari/537.36"
    )

    # Stealth 脚本：隐藏自动化特征
    STEALTH_SCRIPT = """
        // 覆盖 navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });

        // 覆盖 chrome.runtime
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {}
        };

        // 覆盖 permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );

        // 覆盖 plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });

        // 覆盖 languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });

        // 覆盖 platform
        Object.defineProperty(navigator, 'platform', {
            get: () => 'Win32'
        });

        // 覆盖 hardwareConcurrency
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8
        });

        // 覆盖 deviceMemory
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8
        });

        // 隐藏自动化相关属性
        delete navigator.__proto__.webdriver;
    """

    def __init__(self, timeout: float = 60.0, use_system_proxy: bool = False):
        """
        初始化 BrowserSource。

        Args:
            timeout: 超时时间（秒）
            use_system_proxy: 是否使用系统代理。默认为 False，因为：
                - 系统代理 IP 可能被学术出版商封锁
                - VPN/校园网环境下，直接连接通常更可靠
                - 如果确实需要代理，用户可以通过参数显式指定
        """
        self.timeout = timeout
        self.use_system_proxy = use_system_proxy
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        # 专用单线程执行器，保证 Playwright 操作在同一线程
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _detect_proxy(self) -> str | None:
        """检测系统代理设置。"""
        if not self.use_system_proxy:
            return None

        try:
            proxies = urllib.request.getproxies()
            return proxies.get("http") or proxies.get("https")
        except Exception:
            return None

    def _ensure_browser(self):
        """懒初始化共享的 Playwright 浏览器实例（必须在专用线程中调用）。"""
        if self._browser is not None:
            return

        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()

        proxy_url = self._detect_proxy()

        # 使用 Playwright 自带的 Chromium（自包含，不依赖系统浏览器）
        launch_options = {
            "headless": True,
            "args": [
                # 禁用自动化标志，避免被检测
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                # 基础配置
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-popup-blocking",
                # 禁用系统代理（除非显式指定）
                "--no-proxy-server" if not proxy_url else "",
            ],
            # 去掉默认的自动化标志
            "ignore_default_args": ["--enable-automation"],
        }

        # 清理空字符串参数
        launch_options["args"] = [arg for arg in launch_options["args"] if arg]

        if proxy_url:
            print(f"  Using specified proxy: {proxy_url}")
            launch_options["proxy"] = {"server": proxy_url}
        else:
            print(f"  Direct connection (no proxy)")

        self._browser = self._pw.chromium.launch(**launch_options)
        self._using_persistent_context = False

    def _close_in_thread(self):
        """在专用线程中关闭浏览器。"""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None
        self._using_persistent_context = False

    def close(self):
        """关闭浏览器实例和执行器。"""
        try:
            self._executor.submit(self._close_in_thread).result(timeout=10)
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    def find_pdf_url(self, doi: str, metadata: PaperMetadata) -> str | None:
        """通过浏览器渲染页面查找 PDF 链接（线程安全）。"""
        future = self._executor.submit(self._find_pdf_url_in_thread, doi)
        try:
            return future.result(timeout=self.timeout * 3)
        except Exception:
            return None

    def _create_context(self):
        """创建浏览器上下文（注入 stealth 脚本）。"""
        ctx = self._browser.new_context(
            user_agent=self.REALISTIC_UA,
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        # 注入 stealth 脚本
        ctx.add_init_script(self.STEALTH_SCRIPT)
        return ctx

    def _find_pdf_url_in_thread(self, doi: str) -> str | None:
        """在专用线程中查找 PDF URL。"""
        self._ensure_browser()
        ctx = self._create_context()

        try:
            page = ctx.new_page()

            # 尝试 DOI 重定向
            doi_url = f"https://doi.org/{doi}"
            url = self._find_pdf_from_url(page, doi_url)
            if url:
                return url

            # 尝试出版商特定 URL
            for pub_url in self._build_publisher_urls(doi):
                url = self._find_pdf_from_url(page, pub_url)
                if url:
                    return url

            return None
        except Exception:
            return None
        finally:
            page.close()
            ctx.close()

    def _find_pdf_from_url(self, page, url: str) -> str | None:
        """访问 URL，在渲染后的页面中查找 PDF 链接。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare(page)
            page.wait_for_timeout(5000)  # 等待 JavaScript 渲染完成

            # 检查页面是否被反爬系统拦截
            page_text = page.text_content("body") or ""
            if "CLOUDFLARE_ERROR" in page_text or "IP+blocked" in page_text:
                print(f"    [BLOCKED] Page blocked by anti-bot system")
                return None

            if self._page_is_pdf_viewer(page):
                return url

            # 尝试点击 PDF 按钮（ScienceDirect 等需要点击才显示 PDF 链接）
            pdf_url = self._try_click_pdf_button(page, url)
            if pdf_url:
                return pdf_url

            return self._extract_pdf_from_page(page, url)
        except Exception:
            return None

    def _wait_for_cloudflare(self, page):
        """等待 Cloudflare 验证页面通过。"""
        try:
            for _ in range(15):  # 最多等待 15 秒
                html = page.content()
                if "cf-browser-verification" not in html and "cf_chl_opt" not in html:
                    break
                page.wait_for_timeout(1000)
        except Exception:
            pass

    def _page_is_pdf_viewer(self, page) -> bool:
        """检查页面是否是 PDF 查看器。"""
        try:
            for selector in ["embed[type='application/pdf']", "iframe[src*='.pdf']"]:
                elements = page.query_selector_all(selector)
                if elements:
                    return True
            if page.url.lower().endswith(".pdf"):
                return True
        except Exception:
            pass
        return False

    def _try_click_pdf_button(self, page, base_url: str) -> str | None:
        """尝试点击页面上的 PDF 查看/下载按钮。"""
        # 常见的 PDF 按钮选择器
        pdf_button_selectors = [
            "a.accessbar-tooltip-link",  # ScienceDirect "View PDF" 按钮
            "a[data-aa-name='view-pdf']",
            "a:has-text('View PDF')",
            "a:has-text('Download PDF')",
            "a:has-text('PDF')",
            "a[href*='pdfdirect']",  # Wiley
            "a[href*='/pdf/']",  # 通用 PDF 链接
        ]

        for selector in pdf_button_selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    href = btn.get_attribute("href")
                    if href and (".pdf" in href.lower() or "/pdf" in href.lower()):
                        return urljoin(base_url, href)
                    # 点击按钮，可能触发导航或下载
                    btn.click()
                    page.wait_for_timeout(3000)
                    # 检查是否导航到了 PDF 页面
                    if self._page_is_pdf_viewer(page):
                        return page.url
                    # 再次提取链接
                    pdf_url = self._extract_pdf_from_page(page, base_url)
                    if pdf_url:
                        return pdf_url
            except Exception:
                continue

        return None

    def _extract_pdf_from_page(self, page, base_url: str) -> str | None:
        """从渲染后的页面 HTML 中提取 PDF 链接。"""
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # 优先从 meta 标签获取
        for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
            url = meta.get("content")
            if url:
                return url

        # 尝试出版商特定提取
        url = self._extract_publisher_specific(soup, base_url, html)
        if url:
            return url

        # 通用 PDF 链接提取
        pdf_candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            # 跳过无关链接
            if any(skip in href_lower for skip in ["signup", "login", "register", "help", "faq", "cookie"]):
                continue
            if ".pdf" in href_lower or "/pdf" in href_lower or "getpdf" in href_lower:
                pdf_candidates.append(urljoin(base_url, href))

        if pdf_candidates:
            return max(pdf_candidates, key=len)

        # 检查 iframe/embed
        for tag_name in ["iframe", "embed"]:
            for tag in soup.find_all(tag_name, src=True):
                src = tag["src"].lower()
                if ".pdf" in src or "pdf" in src:
                    return urljoin(base_url, tag["src"])

        return None

    def _extract_publisher_specific(self, soup: BeautifulSoup, base_url: str, html: str) -> str | None:
        """针对特定出版商提取 PDF 链接。"""
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
        """构造已知出版商的 DOI 页面 URL。"""
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

    def _download_in_thread(self, url: str, dest: Path) -> bool:
        """在专用线程中下载 PDF。"""
        self._ensure_browser()
        ctx = self._create_context()

        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare(page)

            # 如果页面本身就是 PDF（嵌入的 PDF 查看器）
            if self._page_is_pdf_viewer(page):
                return self._download_with_browser_context(page, url, dest)

            # 从页面中提取 PDF 链接
            pdf_url = self._extract_pdf_from_page(page, url)
            if pdf_url:
                return self._download_with_browser_context(page, pdf_url, dest)

            # 尝试查找并点击 PDF 下载链接（触发浏览器下载）
            return self._try_click_download(page, dest)

        except Exception:
            return False
        finally:
            page.close()
            ctx.close()

    def _download_with_browser_context(self, page, url: str, dest: Path) -> bool:
        """使用浏览器页面的请求上下文下载 PDF（自动携带 cookie）。"""
        try:
            # 使用页面的 API 请求，自动携带 cookie 和认证信息
            response = page.request.get(url, timeout=int(self.timeout * 1000))

            if not response.ok:
                return False

            # 检查内容类型是否为 PDF
            content_type = response.headers.get("content-type", "")
            if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
                return False

            dest.parent.mkdir(parents=True, exist_ok=True)
            body = response.body()

            # 验证是否为有效 PDF
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
        """尝试点击页面上的 PDF 下载链接，捕获浏览器下载事件。"""
        try:
            # 查找可能的 PDF 下载链接
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
                        # 使用 expect_download 捕获下载事件
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
        except Exception:
            return False

    def download(self, url: str, dest: Path) -> bool:
        """通过浏览器下载 PDF（线程安全）。"""
        future = self._executor.submit(self._download_in_thread, url, dest)
        try:
            return future.result(timeout=self.timeout * 3)
        except Exception:
            return False
