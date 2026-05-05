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
        self._is_cdp = False  # 是否通过 CDP 连接用户浏览器
        self._cdp_browser = None  # 缓存的 CDP 连接
        self._cdp_page = None  # CDP 模式下已认证的页面（供 download 复用）
        # 保存认证后的 cookies（不保存 context，因为 greenlet 绑定问题）
        self._persistent_cookies: list[dict] = []
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

    def _launch_headed_browser(self, use_firefox=False):
        """启动可见浏览器窗口（用于人工辅助认证）。

        优先尝试连接用户已打开的 Chrome（CDP 模式），完全使用真实浏览器。
        如果 Chrome 未开启调试模式，回退到 Firefox 或 Playwright Chromium。
        """
        self._ensure_playwright()

        # 优先：连接用户已打开的 Chrome（真实浏览器，不会被检测）
        cdp_browser = self._try_connect_chrome_cdp()
        if cdp_browser:
            return cdp_browser

        # 回退：Firefox（不同 TLS 指纹）
        if use_firefox:
            return self._launch_firefox()

        # 最后：Playwright Chromium
        launch_options = {
            "headless": False,
            "slow_mo": 100,
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

    def _try_connect_chrome_cdp(self):
        """尝试连接用户已打开的 Chrome 浏览器（CDP 调试模式）。结果会被缓存。"""
        # 已有缓存连接，检查是否仍可用
        if self._cdp_browser is not None:
            try:
                _ = self._cdp_browser.contexts
                self._is_cdp = True
                return self._cdp_browser
            except Exception:
                self._cdp_browser = None
                self._is_cdp = False

        import socket
        self._ensure_playwright()
        for port in [9222, 9223, 9229]:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(("127.0.0.1", port))
                sock.close()
                if result == 0:
                    try:
                        browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                        self._is_cdp = True
                        self._cdp_browser = browser
                        print(f"  [OK] 已连接到 Chrome 浏览器 (端口 {port})")
                        return browser
                    except Exception as e:
                        continue
            except Exception:
                continue
        return None

    def _launch_firefox(self):
        """启动 Firefox 浏览器（TLS 指纹不同于 Chromium，不易被反爬检测）。"""
        try:
            launch_options = {
                "headless": False,
                "slow_mo": 100,
                "firefox_user_prefs": {
                    "media.navigator.enabled": False,
                    "privacy.resistFingerprinting": False,
                },
            }
            proxy_url = self._detect_proxy()
            if proxy_url:
                launch_options["proxy"] = {"server": proxy_url}
            return self._pw.firefox.launch(**launch_options)
        except Exception as e:
            print(f"  Firefox 启动失败: {e}")
            print(f"  请运行: playwright install firefox")
            return None

    def _close_in_thread(self):
        self._persistent_cookies = []
        self._cdp_page = None
        if self._cdp_browser:
            try:
                self._cdp_browser.close()
            except Exception:
                pass
            self._cdp_browser = None
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
        """创建浏览器上下文。CDP 模式下直接复用已有 context（保留完整 cookie/session）。"""
        target = browser or self._browser

        # CDP 模式：直接用已有的 context（不新建，保留 Cloudflare cookie 等）
        if self._is_cdp and hasattr(target, 'contexts'):
            for existing_ctx in target.contexts:
                try:
                    _ = existing_ctx.pages  # 验证 context 可用
                    return existing_ctx
                except Exception:
                    continue

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
        """检测页面是否被反爬系统拦截（Cloudflare / Akamai / 其他）。"""
        try:
            html = page.content()
            title = (page.title() or "").lower()
            text = page.text_content("body") or ""

            # Cloudflare 挑战页面
            if any(kw in html for kw in [
                "cf-browser-verification", "cf_chl_opt",
                "challenge-platform", "cf-turnstile",
                "Just a moment", "Checking your browser",
            ]):
                return True

            # Cloudflare / IP 封锁
            if "CLOUDFLARE_ERROR" in text or "IP+blocked" in text:
                return True

            # Akamai / 通用 "Access Denied"（MDPI、ScienceDirect 等）
            if "access denied" in title or "access denied" in text.lower():
                return True

            # Cloudflare challenge form / turnstile widget
            if "challenge-form" in html or "challenge-platform" in html:
                return True
            if "cf-turnstile" in html or "turnstile-wrapper" in html:
                return True

            # ScienceDirect pdfft: URL 未重定向 = 被挑战
            if "sciencedirect.com" in page.url and "/pdfft" in page.url:
                if "pdf.sciencedirectassets.com" not in page.url:
                    return True

            # 页面内容极短（被拦截时通常只有几百字节）
            if len(html) < 1000 and ("denied" in html.lower() or "blocked" in html.lower()):
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

    def _human_auth_and_find(self, doi: str, doi_url: str, all_urls: list[str]) -> str | None:
        """弹出可见浏览器 → 用户认证 → 直接在已认证的浏览器中查找 PDF。

        流程：
        1. 尝试连接用户已打开的 Chrome（CDP 模式，不会被检测）
        2. 如果 CDP 不可用，尝试 Firefox（不同 TLS 指纹）
        3. 最后回退到 Playwright Chromium
        """
        print(f"\n  [{'='*56}]")
        print(f"  |  需要人工辅助认证{' '*36}|")
        print(f"  |  浏览器窗口即将打开，请完成 Cloudflare 验证{' '*10}|")
        print(f"  |  认证完成后程序会自动继续查找 PDF{' '*18}|")
        print(f"  [{'='*56}]")
        print(f"  目标: {doi_url}")

        # 优先：连接用户已打开的 Chrome（CDP 模式）
        self._ensure_playwright()
        cdp = self._try_connect_chrome_cdp()
        if cdp:
            self._headed_browser = cdp
            result = self._auth_with_browser(doi_url, all_urls, "Chrome(CDP)", use_firefox=False)
            if result is not None:
                return result
            # CDP 失败（用户关闭了标签页等），不回退到其他浏览器
            return None

        # 回退：Firefox（不同 TLS 指纹，不易被检测）
        print(f"  未检测到 Chrome 调试模式，使用 Firefox")
        result = self._auth_with_browser(doi_url, all_urls, "Firefox", use_firefox=True)
        if result is not None:
            return result

        # 最后：Playwright Chromium
        print(f"  Firefox 失败，尝试 Chromium")
        result = self._auth_with_browser(doi_url, all_urls, "Chromium", use_firefox=False)
        return result

    def _auth_with_browser(self, doi_url: str, all_urls: list[str],
                           browser_name: str, use_firefox: bool) -> str | None:
        """用指定浏览器尝试人工认证 + PDF 查找。"""
        try:
            # CDP 模式下 browser 已在调用前设置
            if self._headed_browser is None:
                self._headed_browser = self._launch_headed_browser(use_firefox=use_firefox)
            if self._headed_browser is None:
                return None
            ctx = self._create_context(self._headed_browser)
            page = ctx.new_page()
            page.goto(doi_url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            page.wait_for_timeout(3000)

            # 检测是否被封锁
            blocked = self._is_blocked(page)
            cf_challenge = self._is_cloudflare_challenge(page) if blocked else False
            if blocked:
                if not cf_challenge:
                    # 非 Cloudflare 挑战 — 可能是自动化检测或 Akamai 拦截
                    if self._is_cdp:
                        print(f"  [{browser_name}] 页面被拦截，请在浏览器中手动处理")
                    else:
                        title = (page.title() or "").lower()
                        if "access denied" in title:
                            print(f"  [{browser_name}] Access Denied，请在浏览器中尝试手动解决")
                        else:
                            print(f"  [{browser_name}] 被网站检测为自动化浏览器")
                            self._close_headed(page, ctx)
                            return None
                else:
                    print(f"  [{browser_name}] 检测到 Cloudflare 验证，请在浏览器中完成验证")
            else:
                print(f"  [{browser_name}] 页面加载成功，查找 PDF 中...")

            # 没被拦 → 直接查找；被拦 → 等用户认证
            if not blocked:
                print(f"  [{browser_name}] 无需认证，直接查找 PDF...")
                page.wait_for_timeout(2000)
                result = self._find_pdf_on_page(page, ctx, doi_url, all_urls)
                if result:
                    return result
                cookies = ctx.cookies()
                self._save_cookies(cookies)
                self._close_headed(page, ctx)
                return ""

            # 被拦了，等用户完成认证
            print(f"  [{browser_name}] 等待认证...（最多 5 分钟，请在浏览器中完成 Cloudflare 验证）")
            for i in range(300):
                try:
                    if not self._is_blocked(page):
                        print(f"  [OK] 认证成功！正在查找 PDF...")
                        page.wait_for_timeout(2000)
                        result = self._find_pdf_on_page(page, ctx, doi_url, all_urls)
                        if result:
                            return result
                        cookies = ctx.cookies()
                        self._save_cookies(cookies)
                        print(f"  认证成功但未找到 PDF 链接")
                        self._close_headed(page, ctx)
                        return ""
                    page.wait_for_timeout(1000)
                except Exception:
                    print(f"  [{browser_name}] 浏览器已关闭")
                    self._cleanup_headed()
                    return None

            print(f"  认证超时")
            self._close_headed(page, ctx)
            return None

        except Exception as e:
            print(f"  错误: {e}")
            self._cleanup_headed()
            return None

    def _is_cloudflare_challenge(self, page) -> bool:
        """检测是否是 Cloudflare 挑战页面（可等待/可解决）。"""
        try:
            html = page.content()
            return any(kw in html for kw in [
                "cf-browser-verification", "cf_chl_opt",
                "challenge-platform", "cf-turnstile",
                "Just a moment", "Checking your browser",
            ])
        except Exception:
            return False

    def _find_pdf_on_page(self, page, ctx, doi_url: str, all_urls: list[str]) -> str | None:
        """在已认证的页面中查找 PDF 链接。"""
        # 当前页面是否已是 PDF
        if self._page_is_pdf_viewer(page):
            cookies = ctx.cookies()
            self._save_cookies(cookies)
            result = page.url
            self._close_headed(page, ctx)
            return result

        actual_url = page.url

        # ScienceDirect 需要额外等待 React 渲染 "View PDF" 链接
        if "sciencedirect.com" in actual_url:
            for i in range(30):
                link = page.query_selector("a[href*='pdfft']")
                if link and link.is_visible():
                    break
                page.wait_for_timeout(500)

        # 点击 PDF 按钮
        pdf_url = self._try_click_pdf_button(page, actual_url)
        if pdf_url:
            cookies = ctx.cookies()
            self._save_cookies(cookies)
            # CDP 模式：保存已认证的页面供 download 复用
            if self._is_cdp:
                self._cdp_page = page
            self._close_headed(page, ctx)
            return pdf_url

        # 从 HTML 提取
        pdf_url = self._extract_pdf_from_page(page, actual_url)
        if pdf_url:
            cookies = ctx.cookies()
            self._save_cookies(cookies)
            self._close_headed(page, ctx)
            return pdf_url

        # 尝试其他 URL（新标签页，保持 cookie）
        for url in all_urls[1:]:
            try:
                page2 = ctx.new_page()
                page2.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                page2.wait_for_timeout(3000)
                if not self._is_blocked(page2):
                    pdf_url = self._try_click_pdf_button(page2, page2.url)
                    if not pdf_url:
                        pdf_url = self._extract_pdf_from_page(page2, page2.url)
                page2.close()
                if pdf_url:
                    cookies = ctx.cookies()
                    self._save_cookies(cookies)
                    self._close_headed(page, ctx)
                    return pdf_url
            except Exception:
                try:
                    page2.close()
                except Exception:
                    pass

        return None

    def _close_headed(self, page=None, ctx=None):
        """关闭人工认证的浏览器资源。保存 cookies 供下载复用。"""
        # 保存 cookies（所有模式都保存，不依赖 context 跨 greenlet）
        if ctx:
            try:
                cookies = ctx.cookies()
                if cookies:
                    self._persistent_cookies = cookies
            except Exception:
                pass
        # CDP 模式：不关闭 page（可能被 _cdp_page 引用用于下载）
        if page and not self._is_cdp:
            try:
                page.close()
            except Exception:
                pass
        if ctx and not self._is_cdp:
            try:
                ctx.close()
            except Exception:
                pass
        self._cleanup_headed()

    def _cleanup_headed(self):
        """清理 headed 浏览器。CDP 模式下保留连接。"""
        if self._headed_browser:
            if not self._is_cdp:
                try:
                    self._headed_browser.close()
                except Exception:
                    pass
            self._headed_browser = None
            # CDP 状态由 _try_connect_chrome_cdp 管理，不在这里重置

    def _human_assisted_auth(self, url: str) -> bool:
        """弹出可见浏览器窗口，等待用户手动完成 Cloudflare 认证。
        认证成功后保存 cookie 供后续 headless 请求复用。

        Returns:
            True 表示认证成功，False 表示用户放弃或超时
        """
        print(f"\n  [{'='*56}]")
        print(f"  |  需要人工辅助认证{' '*36}|")
        print(f"  |  浏览器窗口即将打开，请完成 Cloudflare 验证{' '*10}|")
        print(f"  |  认证完成后程序会自动继续下载{' '*22}|")
        print(f"  [{'='*56}]")
        print(f"  目标: {url}")

        # 优先：连接用户已打开的 Chrome（CDP 模式）
        self._ensure_playwright()
        cdp = self._try_connect_chrome_cdp()
        if cdp:
            self._headed_browser = cdp
            result = self._auth_for_download(url, "Chrome(CDP)", use_firefox=False)
            if result:
                return True
            return False

        # 回退：Firefox、Chromium
        for browser_name, use_firefox in [("Firefox", True), ("Chromium", False)]:
            result = self._auth_for_download(url, browser_name, use_firefox)
            if result:
                return True
            if not use_firefox:
                print(f"  Chromium 被检测，尝试 Firefox...")

        return False

    def _auth_for_download(self, url: str, browser_name: str, use_firefox: bool) -> bool:
        """用指定浏览器尝试人工认证（下载流程）。"""
        try:
            # CDP 模式下 browser 已在调用前设置
            if self._headed_browser is None:
                self._headed_browser = self._launch_headed_browser(use_firefox=use_firefox)
            if self._headed_browser is None:
                return False
            ctx = self._create_context(self._headed_browser)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            page.wait_for_timeout(3000)

            # 检查是否被直接封锁（非 Cloudflare 挑战）
            if self._is_blocked(page) and not self._is_cloudflare_challenge(page):
                html = page.content()
                if "There was a problem" in html or "CLOUDFLARE_ERROR" in html:
                    self._close_headed(page, ctx)
                    return False

            # 等待用户完成认证（最多 5 分钟）
            print(f"  [{browser_name}] 等待认证中...（最多 5 分钟）")
            for i in range(300):
                if not self._is_blocked(page):
                    print(f"  [OK] 认证成功！")
                    cookies = ctx.cookies()
                    self._save_cookies(cookies, ctx)
                    self._close_headed(page, ctx)
                    self._headed_mode_used = True
                    return True
                page.wait_for_timeout(1000)

            print(f"  认证超时")
            self._close_headed(page, ctx)
            return False

        except Exception as e:
            print(f"  认证失败: {e}")
            self._cleanup_headed()
            return False

    def _save_cookies(self, cookies: list, source_ctx=None):
        """保存 cookies 供后续下载复用。"""
        if cookies:
            self._persistent_cookies = cookies

    def _get_context(self):
        """获取浏览器 context。优先 CDP（真实浏览器），回退 headless。"""
        self._ensure_playwright()

        # 优先：CDP 连接（真实 Chrome，不会被反爬检测）
        cdp = self._try_connect_chrome_cdp()
        if cdp:
            ctx = self._create_context(cdp)
            # CDP context 已含用户 cookies，额外的也注入
            if self._persistent_cookies:
                try:
                    ctx.add_cookies(self._persistent_cookies)
                except Exception:
                    pass
            return ctx

        # 回退：headless 浏览器 + 注入 cookies
        self._ensure_browser()
        ctx = self._create_context()
        if self._persistent_cookies:
            try:
                ctx.add_cookies(self._persistent_cookies)
            except Exception:
                pass
        return ctx

    # ── PDF URL 查找 ─────────────────────────────────────────────

    def _find_pdf_url_in_thread(self, doi: str) -> str | None:
        self._ensure_browser()

        doi_url = f"https://doi.org/{doi}"
        all_urls = [doi_url] + self._build_publisher_urls(doi)

        # ScienceDirect 等需要 Cloudflare 认证的站点：跳过 headless，直接人工认证
        # headless 模式必然被拦截，浪费时间且可能导致 CF cookie 窗口缩小
        if doi.startswith("10.1016/"):
            return self._human_auth_and_find(doi, doi_url, all_urls) or None

        # Phase 1: 尝试 headless 模式（完全自动）
        ctx = self._create_context()
        try:
            page = ctx.new_page()

            for url in all_urls:
                result = self._try_find_pdf(page, url)
                if result:
                    ctx.close()
                    return result

            # headless 被拦截，检查是否需要人工认证
            page2 = ctx.new_page()
            page2.goto(doi_url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._wait_for_cloudflare_auto(page2, max_wait=5)
            page2.wait_for_timeout(3000)

            blocked = self._is_blocked(page2)
            page2.close()
            page.close()
            ctx.close()

            if not blocked:
                return None

        except Exception:
            return None

        # Phase 2: 弹出可见浏览器，等待人工认证
        return self._human_auth_and_find(doi, doi_url, all_urls) or None

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
            "a[href*='pdfft']",  # ScienceDirect: /pdfft?md5=...
            "a.accessbar-tooltip-link",
            "a[data-aa-name='view-pdf']",
            "a:has-text('View PDF')",
            "a:has-text('Download PDF')",
            "a:has-text('PDF')",
            "a[href*='pdfdirect']",
            "a[href*='/pdf/']",
        ]

        ctx = page.context

        for selector in selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    href = btn.get_attribute("href")
                    if href:
                        href_lower = href.lower()
                        # 直接返回 PDF 链接（包括 ScienceDirect pdfft）
                        if ".pdf" in href_lower or "/pdf" in href_lower or "pdfft" in href_lower:
                            return urljoin(base_url, href)

                    # 记录点击前的页面列表
                    pages_before = set(ctx.pages)

                    # 监听新标签页
                    with page.expect_popup(timeout=10000) as popup_info:
                        btn.click()

                    new_page = popup_info.value
                    new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    new_page.wait_for_timeout(3000)

                    # 检查新标签页是否是 PDF
                    if self._page_is_pdf_viewer(new_page):
                        pdf_url = new_page.url
                        new_page.close()
                        return pdf_url

                    # 用新标签页的实际 URL 作为 base_url
                    new_base = new_page.url
                    pdf_url = self._extract_pdf_from_page(new_page, new_base)
                    new_page.close()
                    if pdf_url:
                        return pdf_url

                    # 检查原页面是否变化
                    page.wait_for_timeout(2000)
                    if self._page_is_pdf_viewer(page):
                        return page.url
                    pdf_url = self._extract_pdf_from_page(page, base_url)
                    if pdf_url:
                        return pdf_url

            except Exception:
                # 没有弹出新标签页，检查当前页面
                try:
                    page.wait_for_timeout(3000)
                    if self._page_is_pdf_viewer(page):
                        return page.url
                    pdf_url = self._extract_pdf_from_page(page, base_url)
                    if pdf_url:
                        return pdf_url
                except Exception:
                    pass
                continue
        return None

    def _extract_pdf_from_page(self, page, base_url: str) -> str | None:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
            url = meta.get("content")
            if url:
                return url

        url = self._extract_publisher_specific(soup, base_url, html, page=page)
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

    def _extract_publisher_specific(self, soup: BeautifulSoup, base_url: str, html: str, page=None) -> str | None:
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
            # 优先：用 Playwright 提取带 md5 参数的 pdfft 链接（React 渲染后才可见）
            if page:
                pii_match = re.search(r'/pii/(\w+)', base_url)
                pii = pii_match.group(1) if pii_match else None
                # 找当前文章的 pdfft 链接（匹配 PII）
                for a in page.query_selector_all("a[href*='pdfft']"):
                    try:
                        href = a.get_attribute("href")
                        if href and a.is_visible():
                            if pii and pii in href:
                                return urljoin(base_url, href)
                    except Exception:
                        continue
                # 没有匹配 PII 的，取第一个可见的
                for a in page.query_selector_all("a[href*='pdfft']"):
                    try:
                        href = a.get_attribute("href")
                        if href and a.is_visible():
                            return urljoin(base_url, href)
                    except Exception:
                        continue

            # 回退：从 PII 构造（无 md5，可能被地区限制）
            pii_match = re.search(r'/pii/(\w+)', base_url)
            if pii_match:
                pii = pii_match.group(1)
                return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft"

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
        # CDP 模式：如果有已认证的页面，直接在上面下载（保留 Cloudflare cookie）
        if self._is_cdp and self._cdp_page:
            result = self._download_on_cdp_page(self._cdp_page, url, dest)
            if result:
                return result
            # CDP 页面下载失败（可能是 pdfft CF 挑战），尝试认证后重试
            if self._human_assisted_auth(url):
                try:
                    self._cdp_page.reload(wait_until="domcontentloaded", timeout=15000)
                    self._cdp_page.wait_for_timeout(2000)
                    result = self._download_on_cdp_page(self._cdp_page, url, dest)
                    if result:
                        return result
                except Exception:
                    pass

        ctx = self._get_context()
        page = None

        try:
            # 优先用 request.get（快速，不触发页面渲染）
            if self._try_download_request(ctx, url, dest):
                return True

            # request.get 失败，回退到浏览器导航
            page = ctx.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            except Exception as e:
                if "Download is starting" in str(e):
                    try:
                        with page.expect_download(timeout=30000) as dl_info:
                            pass
                        download = dl_info.value
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        download.save_as(str(dest))
                        if self.is_pdf(dest):
                            return True
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return False
                raise

            self._wait_for_cloudflare_auto(page, max_wait=10)

            if self._is_blocked(page):
                auth_ok = self._human_assisted_auth(url)
                if not auth_ok:
                    return False
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
            if page and not self._is_cdp:
                try:
                    page.close()
                except Exception:
                    pass
            if not self._is_cdp:
                try:
                    ctx.close()
                except Exception:
                    pass

    def _download_on_cdp_page(self, page, url: str, dest: Path) -> str | None:
        """在已认证的 CDP 页面上下载 PDF。

        ScienceDirect 的 /pdfft 有自己的 Cloudflare 保护，需要单独认证。
        导航到 pdfft URL，如果遇到挑战就等待用户手动完成。
        使用 page.evaluate fetch() 下载（浏览器上下文有分区 cookie）。
        """
        try:
            # 导航到 pdfft URL（可能触发 Cloudflare 挑战或直接下载）
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                if "Download is starting" in str(e):
                    # 直接触发下载
                    try:
                        with page.expect_download(timeout=30000) as dl_info:
                            pass
                        download = dl_info.value
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        download.save_as(str(dest))
                        if self.is_pdf(dest):
                            return True
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return None
                raise

            current_url = page.url

            # ScienceDirect pdfft: 等待重定向到 pdf.sciencedirectassets.com
            if "sciencedirect.com" in current_url and "/pdfft" in current_url:
                for _ in range(5):
                    page.wait_for_timeout(1000)
                    if "pdf.sciencedirectassets.com" in page.url:
                        break
                current_url = page.url

            # 检查是否有 Cloudflare 挑战（pdfft 可能有独立的 CF 保护）
            if self._is_blocked(page):
                print(f"  PDF 页面需要 Cloudflare 认证，请在浏览器中完成验证（最多 5 分钟）...")
                for _ in range(300):
                    if not self._is_blocked(page):
                        break
                    page.wait_for_timeout(1000)
                else:
                    return None
                current_url = page.url

            # ScienceDirect: pdfft 重定向到 init 页面，等待 JS 跳转到实际 PDF URL
            if "pdf.sciencedirectassets.com" in current_url and "/craft/" in current_url:
                for _ in range(20):
                    page.wait_for_timeout(1000)
                    if "/main.pdf" in page.url and "/craft/" not in page.url:
                        break
                    # JS 跳转期间可能触发 CF 挑战
                    if self._is_blocked(page):
                        print(f"  PDF 页面需要 Cloudflare 认证，请在浏览器中完成验证...")
                        for _ in range(300):
                            if not self._is_blocked(page):
                                break
                            page.wait_for_timeout(1000)
                        break
                current_url = page.url

            # 使用浏览器内 fetch 下载（带分区 cookie 的请求）
            if "pdf.sciencedirectassets.com" in current_url:
                pdf_b64 = None
                for attempt in range(3):
                    result = page.evaluate("""
                        async (url) => {
                            try {
                                const resp = await fetch(url, {credentials: 'include'});
                                const ct = resp.headers.get('content-type') || '';
                                if (!ct.includes('pdf')) return {error: 'content-type: ' + ct + ', status: ' + resp.status};
                                const blob = await resp.blob();
                                const buf = await blob.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                let binary = '';
                                for (let i = 0; i < bytes.length; i++) {
                                    binary += String.fromCharCode(bytes[i]);
                                }
                                return {data: btoa(binary)};
                            } catch (e) {
                                return {error: e.message};
                            }
                        }
                    """, current_url)
                    if isinstance(result, dict) and result.get("data"):
                        pdf_b64 = result["data"]
                        break
                    err = result.get("error", "unknown") if isinstance(result, dict) else str(result)
                    if attempt < 2:
                        page.wait_for_timeout(2000)
                        # 刷新页面重新触发 pdfft -> init -> main.pdf 重定向链
                        page.reload(wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        # 等待重定向到 pdf.sciencedirectassets.com
                        for _ in range(10):
                            if "pdf.sciencedirectassets.com" in page.url:
                                break
                            page.wait_for_timeout(1000)
                        # 等待 JS 跳转到 main.pdf
                        if "/craft/" in page.url:
                            for _ in range(15):
                                page.wait_for_timeout(1000)
                                if "/main.pdf" in page.url:
                                    break
                        current_url = page.url
                if pdf_b64:
                    import base64
                    pdf_bytes = base64.b64decode(pdf_b64)
                    if pdf_bytes[:4] == b"%PDF":
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(pdf_bytes)
                        if self.is_pdf(dest):
                            return True
                        dest.unlink(missing_ok=True)

        except Exception:
            pass
        return None

    def _try_download_request(self, ctx, url: str, dest: Path) -> bool:
        """尝试用 request.get 直接下载（不触发页面导航）。"""
        try:
            # ScienceDirect: 添加 ?download=true 参数（Zotero 策略）
            download_url = url
            if "sciencedirect.com" in url and "pdfft" in url and "download=true" not in url:
                download_url = url + ("&" if "?" in url else "?") + "download=true"

            page = ctx.new_page()
            response = page.request.get(download_url, timeout=int(self.timeout * 1000))

            if not response.ok:
                if not self._is_cdp:
                    page.close()
                return False

            body = response.body()

            # 是 PDF 内容
            if body[:4] == b"%PDF":
                if not self._is_cdp:
                    page.close()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                if self.is_pdf(dest):
                    return True
                dest.unlink(missing_ok=True)
                return False

            # ScienceDirect: /pdfft 返回的是中间重定向页面（HTML），不是 PDF
            # 解析 meta-refresh 提取真实 PDF URL（Zotero parseIntermediatePDFPage 策略）
            if b"meta" in body[:2000].lower() and b"refresh" in body[:2000].lower():
                real_url = self._parse_intermediate_pdf(body, download_url)
                if real_url:
                    resp2 = page.request.get(real_url, timeout=int(self.timeout * 1000))
                    if not self._is_cdp:
                        page.close()
                    if resp2.ok:
                        body2 = resp2.body()
                        if body2[:4] == b"%PDF":
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(body2)
                            if self.is_pdf(dest):
                                return True
                            dest.unlink(missing_ok=True)
                    return False

            if not self._is_cdp:
                page.close()
            return False
        except Exception:
            return False

    def _parse_intermediate_pdf(self, body: bytes, base_url: str) -> str | None:
        """解析 ScienceDirect 中间重定向页面，提取真实 PDF URL。

        Zotero parseIntermediatePDFPage 策略：
        1. <meta HTTP-EQUIV="Refresh" CONTENT="0;URL=...">
        2. <a id="redirect-message" href="...">
        """
        from bs4 import BeautifulSoup
        text = body.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(text, "html.parser")

        # 策略 1: meta refresh
        meta = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "refresh"})
        if meta:
            content = meta.get("content", "")
            import re
            match = re.search(r'\d+;URL=(.+)', content)
            if match:
                return match.group(1).strip()

        # 策略 2: redirect-message link
        a = soup.select_one("#redirect-message a")
        if a and a.get("href"):
            return a["href"]

        # 策略 3: URL 本身包含 .pdf
        if ".pdf" in base_url.lower():
            return base_url

        return None

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
