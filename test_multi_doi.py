"""综合测试：多个有效 DOI，覆盖不同出版商。"""
import subprocess
import time
import socket
from pathlib import Path

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(Path.home().name),
]
CHROME_PROFILE = Path("E:/CC_SPACE/my_create/doi-downloader/.chrome-profile")

def ensure_chrome_cdp():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    if sock.connect_ex(("127.0.0.1", 9222)) == 0:
        sock.close()
        return True
    sock.close()
    for p in CHROME_PATHS:
        if Path(p).exists():
            CHROME_PROFILE.mkdir(exist_ok=True)
            subprocess.Popen([p, f"--user-data-dir={CHROME_PROFILE}",
                              "--remote-debugging-port=9222",
                              "--no-first-run", "--no-default-browser-check"])
            for _ in range(15):
                time.sleep(1)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", 9222)) == 0:
                    sock.close()
                    return True
                sock.close()
    return False

def main():
    from doi_downloader.sources.browser import BrowserSource
    from doi_downloader.metadata import resolve_metadata

    test_dois = [
        ("10.1038/s41586-024-07487-w",  "Nature (OA)"),
        ("10.3390/s20092547",            "MDPI Sensors (OA)"),
        ("10.3390/rs12162580",           "MDPI Remote Sensing (OA)"),
        ("10.1016/j.compag.2019.105017", "Elsevier ScienceDirect"),
        ("10.1016/j.rse.2020.112114",    "Elsevier RSE"),
    ]

    output_dir = Path("E:/CC_SPACE/my_create/doi-downloader/test_output")
    output_dir.mkdir(exist_ok=True)

    print("启动 Chrome（CDP 调试模式）...")
    if not ensure_chrome_cdp():
        print("Chrome 启动失败")
        return
    print("Chrome 就绪\n")

    browser = BrowserSource(timeout=120.0, use_system_proxy=False)
    results = []

    for doi, label in test_dois:
        print(f"\n{'='*60}")
        print(f"[{label}] {doi}")

        try:
            metadata = resolve_metadata(doi)
            print(f"  标题: {(metadata.title or 'N/A')[:60]}...")

            url = browser.find_pdf_url(doi, metadata)
            if url:
                print(f"  URL: {url[:80]}...")
                dest = output_dir / f"{doi.replace('/', '_')}.pdf"
                success = browser.download(url, dest)
                if success and dest.exists():
                    size_kb = dest.stat().st_size / 1024
                    print(f"  [OK] download success ({size_kb:.0f} KB)")
                    results.append((label, "OK", f"{size_kb:.0f} KB"))
                else:
                    print(f"  [FAIL] download failed")
                    results.append((label, "download failed", ""))
            else:
                print(f"  [FAIL] PDF URL not found")
                results.append((label, "URL not found", ""))
        except Exception as e:
            print(f"  [FAIL] error: {e}")
            results.append((label, "error", str(e)[:40]))

    browser.close()

    print(f"\n{'='*60}")
    print(f"结果汇总:")
    ok = sum(1 for _, s, _ in results if s == "OK")
    print(f"成功: {ok}/{len(results)}\n")
    for label, status, info in results:
        icon = "[OK]" if status == "OK" else "[FAIL]"
        print(f"  {icon} {label:30s} {status} {info}")

    for f in output_dir.glob("*.pdf"):
        f.unlink()

if __name__ == "__main__":
    main()
