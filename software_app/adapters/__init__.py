from __future__ import annotations

from .bluesky import BlueskyNativeAdapter
from .ehentai import EhentaiNativeAdapter
from .google_image import GoogleImageNativeAdapter
from .instagram import InstagramNativeAdapter
from .jmcomic import JmComicNativeAdapter
from .pixiv import PixivNativeAdapter
from .twitter import TwitterNativeAdapter, TwitterScriptAdapter
from .webpage import WebPageCrawlerAdapter
from software_app.core.plugin_loader import AdapterBundle, load_adapter_plugins
from software_app.core.settings import PLUGINS_DIR


def create_adapter_bundle(plugin_root=PLUGINS_DIR) -> AdapterBundle:
    factories = [
        ("twitter", "Twitter/X", TwitterNativeAdapter),
        ("jmcomic", "JMComic", JmComicNativeAdapter),
        ("pixiv", "Pixiv", PixivNativeAdapter),
        ("google_image", "Google 相似图片", GoogleImageNativeAdapter),
        ("website", "网页资源", WebPageCrawlerAdapter),
        ("bluesky", "Bluesky", BlueskyNativeAdapter),
        ("instagram", "Instagram", InstagramNativeAdapter),
        ("ehentai", "E-Hentai / ExHentai", EhentaiNativeAdapter),
    ]
    return load_adapter_plugins(factories, plugin_root)


def create_default_adapters(plugin_root=PLUGINS_DIR):
    return list(create_adapter_bundle(plugin_root).adapters)


__all__ = [
    "TwitterScriptAdapter",
    "TwitterNativeAdapter",
    "JmComicNativeAdapter",
    "PixivNativeAdapter",
    "GoogleImageNativeAdapter",
    "WebPageCrawlerAdapter",
    "BlueskyNativeAdapter",
    "InstagramNativeAdapter",
    "EhentaiNativeAdapter",
    "create_default_adapters",
    "create_adapter_bundle",
]
