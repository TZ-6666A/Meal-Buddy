"""首页传输优化：预压缩、ETag/304 与缓存头。"""

from app.main import _WEB_PAGE_BYTES


def test_homepage_serves_compressed_html_with_cache_headers(client):
    plain = client.get("/", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200
    assert "饭搭子" in plain.text
    assert plain.headers["cache-control"] == "public, max-age=0, s-maxage=300"
    assert "Accept-Encoding" in plain.headers["vary"]
    assert plain.headers["etag"]
    assert len(plain.content) == len(_WEB_PAGE_BYTES)

    compressed = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert compressed.status_code == 200
    assert compressed.headers["content-encoding"] == "gzip"
    assert int(compressed.headers["content-length"]) < len(_WEB_PAGE_BYTES)
    assert "饭搭子" in compressed.text


def test_homepage_returns_304_for_matching_etag(client):
    first = client.get("/", headers={"Accept-Encoding": "gzip"})
    etag = first.headers["etag"]

    cached = client.get(
        "/",
        headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
    )
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag
