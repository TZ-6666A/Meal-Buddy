"""Bilibili video search for the in-app watch room."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import quote_plus, urlsplit, urlunsplit

import httpx

logger = logging.getLogger("fandazi.watch")

VIDEO_SEARCH_URL = "https://search.bilibili.com/all"
VIDEO_SEARCH_API_URL = "https://api.bilibili.com/x/web-interface/search/type"
SEARCH_PAGE_SIZE = 20
SEARCH_PAGE_RETRY_DELAY = 0.2
WATCH_PAGE_SIZE = 8
WATCH_ALL_PAGES = 150
WATCH_CATEGORY_PAGES = 50
SEARCH_PAGE_CACHE_TTL = 3 * 60 * 60
SEARCH_PAGE_CACHE_MAX_ENTRIES = 1024
SEARCH_ORDERS = ("totalrank", "click", "pubdate")
CATEGORY_TIDS = {"综艺": 71}
MAX_COVER_BYTES = 5 * 1024 * 1024
ALLOWED_COVER_TYPES = {"image/avif", "image/jpeg", "image/png", "image/webp"}
COVER_CACHE_TTL = 6 * 60 * 60
COVER_CACHE_MAX_BYTES = 48 * 1024 * 1024
COVER_CACHE_MAX_ENTRIES = 512
# B 站图片走多台 CDN；某台节点抽风时换一台通常只要几百毫秒。
COVER_CDN_HOSTS = ("i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com")
COVER_MIRROR_DELAY = 0.35

_cover_client: httpx.AsyncClient | None = None
_cover_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_cover_cache_bytes = 0
_search_client: httpx.AsyncClient | None = None
_search_client_loop: asyncio.AbstractEventLoop | None = None
_search_page_cache: OrderedDict[tuple[str, str, str, int], tuple[float, list[dict]]] = OrderedDict()
_search_prefetch_keys: set[tuple[str, str, str, int]] = set()
_search_prefetch_tasks: set[asyncio.Task[None]] = set()

CATEGORY_TERMS = {
    "all": "",
    "动画": "动画",
    "动漫": "番剧",
    "电视剧": "电视剧",
    "电影": "电影",
    "电影解说": "电影解说",
    "纪录片": "纪录片",
    "综艺": "综艺",
    "美剧": "美剧",
    "短剧": "短剧",
    "生活": "生活",
}


class WatchSearchUnavailable(Exception):
    """Raised when Bilibili search cannot return usable results."""


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: object) -> str:
    parser = _PlainText()
    parser.feed(str(value or ""))
    return " ".join("".join(parser.parts).split())


class _SearchPageParser(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._card: dict | None = None
        self._depth = 0
        self._author_depth: int | None = None
        self._author_parts: list[str] = []
        self._stats_depth: int | None = None
        self._stats_parts: list[str] = []
        self._has_stats = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        is_card = tag == "div" and ("bili-video-card" in classes or "bili-video-card__wrap" in classes)
        if self._card is None:
            if not is_card:
                return
            self._card = {"title": "", "cover": "", "bvid": "", "author": "", "play": ""}
            self._depth = 1
            self._has_stats = False
            return

        if tag not in self._VOID_TAGS:
            self._depth += 1

        if tag == "a":
            match = re.search(r"/video/(BV[A-Za-z0-9]{10})(?:/|$)", urlsplit(attributes.get("href") or "").path)
            if match:
                self._card["bvid"] = match.group(1)
        elif tag == "img" and not self._card["cover"]:
            self._card["cover"] = attributes.get("src") or ""
        elif tag == "source" and not self._card["cover"]:
            srcset = attributes.get("srcset") or ""
            self._card["cover"] = srcset.split(",", 1)[0].split(" ", 1)[0]
        elif tag == "h3" and "bili-video-card__info--tit" in classes:
            self._card["title"] = attributes.get("title") or ""
        elif tag == "span" and "bili-video-card__info--author" in classes:
            self._author_depth = self._depth
            self._author_parts = []
        elif tag == "span" and "bili-video-card__stats--item" in classes and not self._has_stats:
            self._stats_depth = self._depth
            self._stats_parts = []

    def handle_data(self, data: str) -> None:
        if self._card is None:
            return
        if self._author_depth is not None and self._depth >= self._author_depth:
            self._author_parts.append(data)
        if self._stats_depth is not None and self._depth >= self._stats_depth:
            self._stats_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if self._author_depth == self._depth and tag == "span":
            self._card["author"] = " ".join("".join(self._author_parts).split())
            self._author_depth = None
        if self._stats_depth == self._depth and tag == "span":
            self._card["play"] = " ".join("".join(self._stats_parts).split())
            self._stats_depth = None
            self._has_stats = True

        if tag not in self._VOID_TAGS:
            self._depth -= 1
        if self._depth == 0:
            if self._card["bvid"] and self._card["title"]:
                self.items.append(self._card)
            self._card = None


def _play_count(value: object) -> int:
    text = str(value or "").replace(",", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万亿]?)", text)
    if not match:
        return 0
    multiplier = {"": 1, "万": 10_000, "亿": 100_000_000}[match.group(2)]
    return max(0, int(float(match.group(1)) * multiplier))


def _cover_url(value: object) -> str:
    cover = str(value or "").strip()
    if cover.startswith("//"):
        cover = "https:" + cover
    parsed = urlsplit(cover)
    host = (parsed.hostname or "").lower()
    if host != "hdslb.com" and not host.endswith(".hdslb.com"):
        return ""
    # 搜索接口偶尔返回 http 地址，直接丢弃会让前端只剩占位图，这里统一升级。
    # Keep Bilibili's thumbnail transform so the proxy does not fetch the original image.
    path = parsed.path
    if "@" not in path and path.lower().endswith((".jpg", ".jpeg")):
        path += "@672w_378h_1c_!web-search-common-cover.avif"
    return urlunsplit(("https", parsed.netloc, path, parsed.query, ""))


def _get_cover_client() -> httpx.AsyncClient:
    global _cover_client
    if _cover_client is None:
        _cover_client = httpx.AsyncClient(
            timeout=httpx.Timeout(4.0, connect=1.5),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
            },
        )
    return _cover_client


def _cached_cover(url: str) -> tuple[bytes, str] | None:
    global _cover_cache_bytes
    entry = _cover_cache.get(url)
    if entry is None:
        return None
    expires_at, content, content_type = entry
    if expires_at <= time.monotonic():
        _cover_cache_bytes -= len(content)
        del _cover_cache[url]
        return None
    _cover_cache.move_to_end(url)
    return content, content_type


def _cache_cover(url: str, content: bytes, content_type: str) -> None:
    global _cover_cache_bytes
    if len(content) > COVER_CACHE_MAX_BYTES:
        return
    previous = _cover_cache.pop(url, None)
    if previous:
        _cover_cache_bytes -= len(previous[1])
    _cover_cache[url] = (time.monotonic() + COVER_CACHE_TTL, content, content_type)
    _cover_cache_bytes += len(content)
    while (
        _cover_cache_bytes > COVER_CACHE_MAX_BYTES
        or len(_cover_cache) > COVER_CACHE_MAX_ENTRIES
    ) and _cover_cache:
        _, (_, removed, _) = _cover_cache.popitem(last=False)
        _cover_cache_bytes -= len(removed)


async def close_cover_client() -> None:
    """Release the shared upstream connection pool during application shutdown."""
    global _cover_client, _cover_cache_bytes
    if _cover_client is not None:
        await _cover_client.aclose()
        _cover_client = None
    _cover_cache.clear()
    _cover_cache_bytes = 0


def _cover_candidates(url: str) -> list[str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in COVER_CDN_HOSTS:
        return [url]
    return [url] + [
        urlunsplit((parsed.scheme, mirror, parsed.path, parsed.query, ""))
        for mirror in COVER_CDN_HOSTS
        if mirror != host
    ]


async def _download_cover(url: str) -> tuple[bytes, str]:
    client = _get_cover_client()
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise WatchSearchUnavailable
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in ALLOWED_COVER_TYPES:
            raise WatchSearchUnavailable
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_COVER_BYTES:
            raise WatchSearchUnavailable
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_COVER_BYTES:
                raise WatchSearchUnavailable
            chunks.append(chunk)
        return b"".join(chunks), content_type


async def _download_cover_with_mirrors(url: str) -> tuple[bytes, str]:
    """先请求原节点；短时间没响应就并行试其他 CDN，谁先成功用谁。"""
    candidates = _cover_candidates(url)
    tasks: list[asyncio.Task] = [asyncio.create_task(_download_cover(candidates[0]))]
    next_index = 1
    try:
        while tasks:
            timeout = COVER_MIRROR_DELAY if next_index < len(candidates) else None
            done, _ = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            failed = False
            for task in done:
                tasks.remove(task)
                try:
                    return task.result()
                except (httpx.HTTPError, ValueError, WatchSearchUnavailable):
                    failed = True
            if next_index < len(candidates) and (failed or not done):
                tasks.append(asyncio.create_task(_download_cover(candidates[next_index])))
                next_index += 1
        raise WatchSearchUnavailable
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def fetch_cover(url: str) -> tuple[bytes, str]:
    if len(url) > 2048:
        raise ValueError("封面地址无效")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not (host == "hdslb.com" or host.endswith(".hdslb.com"))
        or not parsed.path.startswith("/bfs/")
    ):
        raise ValueError("封面地址无效")

    cached = _cached_cover(url)
    if cached is not None:
        return cached

    try:
        content, content_type = await _download_cover_with_mirrors(url)
    except (httpx.HTTPError, ValueError) as exc:
        raise WatchSearchUnavailable from exc
    _cache_cover(url, content, content_type)
    return content, content_type


def _video_item(raw: dict, category: str) -> dict | None:
    title = _plain_text(raw.get("title"))
    if not title:
        return None

    bvid = str(raw.get("bvid") or "")
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
        url = f"https://www.bilibili.com/video/{bvid}/"
    else:
        url = f"{VIDEO_SEARCH_URL}?search_type=video&keyword={quote_plus(title)}"

    return {
        "title": title,
        "cover": _cover_url(raw.get("cover") or raw.get("pic")),
        "url": url,
        "author": _plain_text(raw.get("author")),
        "play_count": _play_count(raw.get("play") or raw.get("play_count")),
        "category": _plain_text(raw.get("typename")) or category_label(category),
    }


def category_label(category: str) -> str:
    return "全部" if category == "all" else category


async def _get_search_client(keyword: str) -> httpx.AsyncClient:
    global _search_client, _search_client_loop
    loop = asyncio.get_running_loop()
    if _search_client is None or _search_client_loop is not loop:
        _search_client = httpx.AsyncClient(
            timeout=httpx.Timeout(4.5, connect=1.8),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
                "Referer": "https://search.bilibili.com/",
                "Origin": "https://search.bilibili.com",
                "Accept": "application/json, text/plain, */*",
            },
        )
        _search_client_loop = loop
    return _search_client


def _search_page_cache_get(key: tuple[str, str, int]) -> list[dict] | None:
    entry = _search_page_cache.get(key)
    if entry is None:
        return None
    expires_at, rows = entry
    if expires_at <= time.monotonic():
        del _search_page_cache[key]
        return None
    _search_page_cache.move_to_end(key)
    return [dict(row) for row in rows]


def _search_page_cache_set(key: tuple[str, str, int], rows: list[dict]) -> None:
    _search_page_cache[key] = (
        time.monotonic() + SEARCH_PAGE_CACHE_TTL,
        [dict(row) for row in rows],
    )
    _search_page_cache.move_to_end(key)
    while len(_search_page_cache) > SEARCH_PAGE_CACHE_MAX_ENTRIES:
        _search_page_cache.popitem(last=False)


async def _fetch_search_page(client: httpx.AsyncClient, keyword: str,
                             order: str, page: int, tid: int | None = None) -> list[dict]:
    tid_key = str(tid or "")
    key = (keyword, tid_key, order, page)
    cached = _search_page_cache_get(key)
    if cached is not None:
        return cached

    response = None
    for attempt in range(2):
        try:
            params = {
                "search_type": "video",
                "keyword": keyword,
                "order": order,
                "page": page,
                "pagesize": SEARCH_PAGE_SIZE,
            }
            if tid is not None:
                params["tids"] = str(tid)
            response = await client.get(VIDEO_SEARCH_API_URL, params=params)
        except httpx.HTTPError:
            logger.warning("bilibili_search_request_failed page=%d", page, exc_info=True)
            return []
        if response.status_code == 200:
            break
        if response.status_code not in (412, 429, 503) or attempt == 1:
            logger.warning("bilibili_search_upstream_status status=%d page=%d",
                           response.status_code, page)
            return []
        await asyncio.sleep(SEARCH_PAGE_RETRY_DELAY)

    try:
        payload = response.json() if response is not None else {}
    except ValueError:
        return []
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    data = payload.get("data")
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    _search_page_cache_set(key, rows)
    return [dict(row) for row in rows]


def _page_source(keyword: str, category: str, page: int, visit_id: str) -> tuple[int, str]:
    if category == "all":
        group = (page - 1) // WATCH_CATEGORY_PAGES
        upstream_page = (page - 1) % WATCH_CATEGORY_PAGES + 1
        return upstream_page, SEARCH_ORDERS[group]
    return page, SEARCH_ORDERS[0]


async def close_search_client() -> None:
    """Release the shared search connection pool during application shutdown."""
    global _search_client, _search_client_loop
    tasks = list(_search_prefetch_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _search_prefetch_tasks.clear()
    _search_prefetch_keys.clear()
    if _search_client is not None:
        await _search_client.aclose()
    _search_client = None
    _search_client_loop = None
    _search_page_cache.clear()


def _prefetch_next_page(client: httpx.AsyncClient, keyword: str, category: str,
                        page: int, visit_id: str) -> None:
    page_count = WATCH_ALL_PAGES if category == "all" else WATCH_CATEGORY_PAGES
    if page >= page_count:
        return
    next_upstream_page, next_order = _page_source(keyword, category, page + 1, visit_id)
    tid = CATEGORY_TIDS.get(category)
    key = (keyword, str(tid or ""), next_order, next_upstream_page)
    if _search_page_cache_get(key) is not None or key in _search_prefetch_keys:
        return

    _search_prefetch_keys.add(key)

    async def fetch() -> None:
        try:
            await _fetch_search_page(client, keyword, next_order, next_upstream_page, tid)
        finally:
            _search_prefetch_keys.discard(key)

    task = asyncio.create_task(fetch())
    _search_prefetch_tasks.add(task)
    task.add_done_callback(_search_prefetch_tasks.discard)


async def search_videos(query: str = "", category: str = "all",
                        page: int = 1, visit_id: str = "") -> dict:
    category_term = CATEGORY_TERMS.get(category)
    if category_term is None:
        raise ValueError("不支持的内容分类")

    user_query = " ".join(query.strip().split())
    search_terms = [term for term in (user_query, category_term) if term]
    keyword = " ".join(search_terms) or "下饭"
    page_count = WATCH_ALL_PAGES if category == "all" else WATCH_CATEGORY_PAGES
    if page < 1 or page > page_count:
        raise ValueError("页码超出范围")
    visit_id = visit_id.strip()[:80] or uuid.uuid4().hex
    upstream_page, order = _page_source(keyword, category, page, visit_id)
    tid = CATEGORY_TIDS.get(category)
    client = await _get_search_client(keyword)

    # Start the alternate rankings with the first request so sparse category
    # pages can be filled without paying two sequential network round trips.
    alternate_tasks: list[asyncio.Task[list[dict]]] = []
    if not user_query and category != "all" and upstream_page == 1:
        alternate_tasks = [
            asyncio.create_task(_fetch_search_page(client, keyword, candidate, upstream_page, tid))
            for candidate in SEARCH_ORDERS
            if candidate != order
        ]
    raw_rows = await _fetch_search_page(client, keyword, order, upstream_page, tid)

    # Narrow searches may run out before the requested page count; fill from the selected category.
    fallback_keyword = category_term if category != "all" else "下饭"
    if len(raw_rows) < WATCH_PAGE_SIZE and fallback_keyword and fallback_keyword != keyword:
        fallback_rows = await _fetch_search_page(client, fallback_keyword, order, upstream_page, tid)
        raw_rows = [*raw_rows, *fallback_rows]
    elif len(raw_rows) < WATCH_PAGE_SIZE and not user_query:
        alternate_rows = await asyncio.gather(*alternate_tasks)
        alternate_tasks.clear()
        for rows in alternate_rows:
            raw_rows.extend(rows)

    for task in alternate_tasks:
        task.cancel()
    if alternate_tasks:
        await asyncio.gather(*alternate_tasks, return_exceptions=True)

    items: list[dict] = []
    seen: set[str] = set()
    for raw in raw_rows:
        item = _video_item(raw, category)
        if item is None or item["url"] in seen:
            continue
        seen.add(item["url"])
        items.append(item)

    random.Random(f"{visit_id}|{keyword}|{category}|{page}").shuffle(items)
    items = items[:WATCH_PAGE_SIZE]
    _prefetch_next_page(client, keyword, category, page, visit_id)
    return {
        "items": items,
        "page": page,
        "page_count": page_count,
        "complete": len(items) == WATCH_PAGE_SIZE,
    }
