"""专门测试 ScienceDirect PDF 下载流程。"""
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

    # 只测试一个 ScienceDirect DOI
    doi = "10.1016/j.compag.2019.105017"
    label = "Elsevier ScienceDirect"

    output_dir = Path("E:/CC_SPACE/my_create/doi-downloader/test_output")
    output_dir.mkdir(exist_ok=True)

    print("检查 Chrome CDP 端口...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    if sock.connect_ex(("127.0.0.1", 9222)) == 0:
        sock.close()
        print("  端口 9222 已打开，Chrome 已在运行")
    else:
        sock.close()
        print("  端口 9222 未打开，启动 Chrome...")
        if not ensure_chrome_cdp():
            print("Chrome 启动失败")
            return
    print("Chrome 就绪\n")

    browser = BrowserSource(timeout=120.0, use_system_proxy=False)

    print(f"\n{'='*60}")
    print(f"[{label}] {doi}")

    try:
        metadata = resolve_metadata(doi)
        print(f"  标题: {(metadata.title or 'N/A')[:60]}...")

        # Step 1: 查找 PDF URL
        print("\n--- Step 1: 查找 PDF URL ---")
        url = browser.find_pdf_url(doi, metadata)
        if url:
            print(f"  找到 URL: {url[:120]}...")
        else:
            print(f"  未找到 PDF URL")
            browser.close()
            return

        # Step 2: 下载 PDF
        print("\n--- Step 2: 下载 PDF ---")
        dest = output_dir / f"{doi.replace('/', '_')}.pdf"
        success = browser.download(url, dest)
        if success and dest.exists():
            size_kb = dest.stat().st_size / 1024
            print(f"\n  [OK] download success ({size_kb:.0f} KB)")
        else:
            print(f"\n  [FAIL] download failed")

    except Exception as e:
        print(f"  [FAIL] error: {e}")
        import traceback
        traceback.print_exc()

    browser.close()

    # 清理测试文件
    for f in output_dir.glob("*.pdf"):
        f.unlink()

if __name__ == "__main__":
    main()
