from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from software_app.core.blocklist import BlocklistStore
from software_app.crawlers.jmcomic.client import JmComicCrawler


ALBUM_HTML = """
<html>
  <head><meta property="og:image" content="https://cdn.example/media/albums/123.jpg"></head>
  <body>
    <h1 class="book-name" id="book-name">本地测试漫画</h1>
    作者：<span itemprop="author" data-type="author"><a>示例作者</a></span>
    <span itemprop="genre" data-type="tags"><a>测试标签</a><a>单本</a></span>
    <a data-album="456"><li>第1話 开始</li></a>
  </body>
</html>
"""

PHOTO_HTML = """
<html>
  <title>第1話 | JMComic</title>
  <script>var scramble_id = 0; var page_arr = ["00001.jpg", "00002.jpg"];</script>
  <img data-original="https://cdn.example/media/photos/456/00001.jpg">
</html>
"""

SEARCH_HTML = """
<a href="/album/123" title="本地测试漫画">
  <img data-original="https://cdn.example/media/albums/123.jpg" alt="本地测试漫画">
</a>
"""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    crawler = object.__new__(JmComicCrawler)
    crawler.domain = "https://18comic.example"
    crawler.session = SimpleNamespace(cookies={})
    requested_urls: list[str] = []

    def page(url: str) -> str:
        requested_urls.append(url)
        if "/photo/" in url:
            return PHOTO_HTML
        if "/search/" in url or "/albums" in url or "/favorite/" in url:
            return SEARCH_HTML
        return ALBUM_HTML

    crawler._get_text = page
    crawler._get_account_text = lambda url, _feature: page(url)

    local_album = crawler.preview("JM123", {"input_kind": "album"})
    online_album = crawler.preview("JM123", {"input_kind": "album", "live": True})
    local_photo = crawler.preview("456", {"input_kind": "photo"})
    search_rows = crawler.search(
        "本地测试漫画",
        options={"match_mode": "exact", "order_by": "mv", "time_range": "m"},
    )
    crawler.category(options={"category": "3D", "order_by": "mp", "time_range": "w"})

    print("本地漫画预览:", local_album.metadata["browser_destination"], "（没有网络请求）")
    print("在线漫画资料:", online_album.title, online_album.metadata["author"],
          len(online_album.metadata["chapters"]), online_album.metadata["tags"])
    print("在线第一页:", online_album.metadata["first_page_url"])
    print("本地章节预览:", local_photo.metadata["browser_destination"])
    print("搜索候选:", [(row["id"], row["title"]) for row in search_rows])
    assert any("main_tag=0" in url and "o=mv" in url and "t=m" in url for url in requested_urls)
    assert any("/albums/3D?" in url and "o=mp" in url and "t=w" in url for url in requested_urls)
    print("搜索/分类设置:", "域名、排序、时间、分类参数已进入请求 URL")

    try:
        crawler.favorites("demo-user")
    except PermissionError as exc:
        print("未登录收藏夹:", exc)
    else:
        raise AssertionError("没有 Cookie 时收藏夹必须失败")

    crawler.session.cookies = {"AVS": "fixture-only"}
    favorites = crawler.favorites("demo-user", options={"favorite_folder_id": "7"})
    assert any(
        "/user/demo-user/favorite/albums?" in url and "folder=7" in url and "folder_id=7" in url
        for url in requested_urls
    )
    print("登录收藏夹示例:", [(row["id"], row["title"]) for row in favorites])

    with tempfile.TemporaryDirectory() as directory:
        blocklist = BlocklistStore(Path(directory) / "blocklist.json")
        blocklist.add_group(("jmcomic_author", "示例作者"))
        blocklist.add_work("jmcomic", "JM123")
        blocklist.add_work("jmcomic", "https://18comic.example/photo/456")
        blocklist.add_work("jmcomic_novel", "https://18comic.example/novel/4459")
        print("作者屏蔽:", blocklist.is_blocked(
            "jmcomic", "https://18comic.example/album/999", author_id="示例作者"
        ))
        print("漫画屏蔽:", blocklist.is_blocked("jmcomic", "JM123", input_kind="album"))
        print("章节屏蔽:", blocklist.is_blocked("jmcomic", "456", input_kind="photo"))
        print("小说屏蔽:", blocklist.is_blocked("jmcomic", "https://18comic.example/novel/4459", input_kind="novel"))

    print("结果: JMComic 本地功能示例通过；本脚本不访问真实站点，也不读取真实 Cookie。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
