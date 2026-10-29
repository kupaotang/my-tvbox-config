import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests

# =========================
# 基本配置
# =========================

INTERFACE_PAGES = [
    "https://gitee.com/zhanghong1983/tvbox",
    "https://gitee.com/liu673cn/box",
    "https://jihulab.com/ygbh1/tvbox",
]

PARSE_CONFIG_URLS = [
    "https://gitee.com/zhanghong1983/tvbox/raw/master/jx.json",
    "https://raw.liucn.cc/box/m.json",
    "https://jihulab.com/ygbh1/tvbox/-/raw/main/jx.json",
]

LIVE_URLS = [
    "https://iptv-org.github.io/iptv/index.m3u",
    "https://live.fanmingming.com/tv/m3u8",
]

TIMEOUT = 10
MAX_WORKERS = 4
MAX_LIVE_CHECKS = 50

OUTPUT_CONFIG = "tv.json"
OUTPUT_REPORT = "report.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
}

CONFIG_PATTERN = re.compile(
    r"""https?://[^\s"'<>]+?\.json(?:\?[^\s"'<>]*)?""",
    re.IGNORECASE,
)


# =========================
# 网络请求
# =========================

def valid_url(url):
    if not isinstance(url, str):
        return False

    url = url.strip()

    if not url or len(url) > 2048:
        return False

    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except Exception:
        return False


def fetch(url, max_bytes=500000):
    if not valid_url(url):
        raise ValueError("URL 格式不正确")

    with requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
        stream=True,
        allow_redirects=True,
    ) as response:
        if not valid_url(response.url):
            raise ValueError("跳转后的 URL 不合法")

        data = bytearray()

        for chunk in response.iter_content(chunk_size=16384):
            if not chunk:
                continue

            remaining = max_bytes - len(data)

            if remaining <= 0:
                break

            data.extend(chunk[:remaining])

            if len(data) >= max_bytes:
                break

        encoding = response.encoding or "utf-8"

        return {
            "url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get(
                "Content-Type", ""
            ).lower(),
            "text": bytes(data).decode(
                encoding,
                errors="replace",
            ),
        }


# =========================
# 自动发现配置文件
# =========================

def discover_configs(page_url):
    found = set()

    try:
        response = fetch(page_url, 1000000)

        if response["status"] != 200:
            return []

        for url in CONFIG_PATTERN.findall(response["text"]):
            url = url.rstrip(".,);]")

            if valid_url(url):
                found.add(url)

    except Exception as exc:
        print(f"[页面读取失败] {page_url}: {exc}")

    return sorted(found)


# =========================
# 读取 JSON
# =========================

def load_json(url):
    try:
        response = fetch(url, 2000000)

        if response["status"] != 200:
            raise ValueError(f"HTTP {response['status']}")

        data = json.loads(response["text"])

        if not isinstance(data, (dict, list)):
            raise ValueError("JSON 格式不正确")

        return data, {
            "url": url,
            "ok": True,
            "reason": "读取成功",
        }

    except Exception as exc:
        return None, {
            "url": url,
            "ok": False,
            "reason": str(exc),
        }


# =========================
# 提取站点和解析接口
# =========================

def extract_sites(data):
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("sites", [])
    else:
        return []

    result = []

    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        url = item.get("api") or item.get("url")

        if name and valid_url(url):
            result.append(item)

    return result


def extract_parses(data):
    if isinstance(data, dict):
        items = data.get("parses", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    result = []

    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict):
            continue

        url = item.get("url")

        if valid_url(url):
            result.append(item)

    return result


def deduplicate(items, key_func):
    result = []
    seen = set()

    for item in items:
        key = key_func(item)

        if key and key not in seen:
            seen.add(key)
            result.append(item)

    return result


# =========================
# 检查解析接口
# =========================

def check_parse(item):
    url = item.get("url", "")
    name = item.get("name", "")

    result = {
        "name": name,
        "url": url,
        "ok": False,
        "reason": "",
    }

    try:
        response = fetch(url)

        if response["status"] != 200:
            result["reason"] = f"HTTP {response['status']}"
            return result

        if not response["text"].strip():
            result["reason"] = "返回内容为空"
            return result

        result["ok"] = True
        result["reason"] = "地址可访问，未验证实际解析"

    except Exception as exc:
        result["reason"] = str(exc)

    return result


# =========================
# 解析 M3U 直播列表
# =========================

def parse_m3u(text, base_url):
    lines = text.splitlines()

    if not any(
        line.strip() == "#EXTM3U"
        for line in lines[:10]
    ):
        return []

    result = []
    name = "未命名频道"

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF"):
            match = re.search(r",\s*(.+)$", line)

            if match:
                name = match.group(1).strip()

        elif not line.startswith("#"):
            url = urljoin(base_url, line)

            if valid_url(url):
                result.append({
                    "name": name,
                    "url": url,
                })

            name = "未命名频道"

    return result


# =========================
# 检查直播地址
# =========================

def check_live(item):
    result = {
        "name": item["name"],
        "url": item["url"],
        "ok": False,
        "reason": "",
    }

    try:
        response = fetch(item["url"])

        if response["status"] not in (200, 206):
            result["reason"] = f"HTTP {response['status']}"
            return result

        content = response["text"].lstrip()
        content_type = response["content_type"]

        if "#EXTM3U" in content[:1000]:
            result["ok"] = True
            result["reason"] = "检测到 HLS 播放列表"

        elif content_type.startswith("video/"):
            result["ok"] = True
            result["reason"] = "媒体地址可访问"

        elif "mpegurl" in content_type:
            result["ok"] = True
            result["reason"] = "媒体播放列表可访问"

        else:
            result["reason"] = "未识别为有效媒体"

    except Exception as exc:
        result["reason"] = str(exc)

    return result


def run_checks(items, func, title):
    results = []

    print(f"\n{title}：共 {len(items)} 项")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(func, item)
            for item in items
        ]

        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception as exc:
                item = {
                    "ok": False,
                    "reason": str(exc),
                }

            results.append(item)

            status = "通过" if item.get("ok") else "失败"

            print(
                f"[{status}] "
                f"{item.get('name', '')} "
                f"{item.get('url', '')} "
                f"{item.get('reason', '')}"
            )

    return sorted(
        results,
        key=lambda x: x.get("ok", False),
        reverse=True,
    )


# =========================
# 主程序
# =========================

def main():
    start_time = time.time()

    print("=" * 50)
    print("影视仓配置自动检测")
    print("=" * 50)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_sources": [],
        "parse_checks": [],
        "live_sources": [],
        "live_checks": [],
        "summary": {},
    }

    config_urls = set(PARSE_CONFIG_URLS)

    print("\n[1/5] 自动发现配置文件")

    for page in INTERFACE_PAGES:
        print(f"读取：{page}")
        config_urls.update(discover_configs(page))

    print("\n[2/5] 下载 JSON 配置")

    all_sites = []
    all_parses = []

    for url in sorted(config_urls):
        data, check = load_json(url)
        report["config_sources"].append(check)

        if data is None:
            continue

        all_sites.extend(extract_sites(data))
        all_parses.extend(extract_parses(data))

    all_sites = deduplicate(
        all_sites,
        lambda x: str(x.get("api") or x.get("url") or ""),
    )

    all_parses = deduplicate(
        all_parses,
        lambda x: str(x.get("url") or ""),
    )

    print("\n[3/5] 检查解析接口")

    parse_checks = run_checks(
        all_parses,
        check_parse,
        "解析接口检查",
    )

    report["parse_checks"] = parse_checks

    good_parse_urls = {
        x["url"]
        for x in parse_checks
        if x.get("ok")
    }

    good_parses = [
        x for x in all_parses
        if x.get("url") in good_parse_urls
    ]

    print("\n[4/5] 检查直播源")

    live_items = []

    for url in LIVE_URLS:
        try:
            response = fetch(url, 4000000)

            if response["status"] != 200:
                report["live_sources"].append({
                    "url": url,
                    "ok": False,
                    "reason": f"HTTP {response['status']}",
                })
                continue

            channels = parse_m3u(
                response["text"],
                response["url"],
            )

            report["live_sources"].append({
                "url": url,
                "ok": bool(channels),
                "channel_count": len(channels),
            })

            live_items.extend(channels)

        except Exception as exc:
            report["live_sources"].append({
                "url": url,
                "ok": False,
                "reason": str(exc),
            })

    live_items = deduplicate(
        live_items,
        lambda x: x["url"],
    )[:MAX_LIVE_CHECKS]

    live_checks = run_checks(
        live_items,
        check_live,
        "直播源检查",
    )

    report["live_checks"] = live_checks

    good_lives = [
        {
            "name": item["name"],
            "type": 0,
            "url": item["url"],
        }
        for item in live_checks
        if item.get("ok")
    ]

    result = {
        "sites": all_sites,
        "parses": good_parses,
        "lives": good_lives,
    }

    print("\n[5/5] 保存结果")

    with open(OUTPUT_CONFIG, "w", encoding="utf-8") as file:
        json.dump(
            result,
            file,
            ensure_ascii=False,
            indent=2,
        )

    report["summary"] = {
        "config_sources": len(config_urls),
        "sites": len(all_sites),
        "parses": len(good_parses),
        "lives": len(good_lives),
        "elapsed_seconds": round(time.time() - start_time, 2),
    }

    with open(OUTPUT_REPORT, "w", encoding="utf-8") as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("\n检测完成")
    print(json.dumps(
        report["summary"],
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
