from __future__ import annotations

from .base import WordPressMangaSource


class MangaReadSource(WordPressMangaSource):
    name = "MangaRead"
    domains = ("mangaread.org",)
    cookie_domain = "www.mangaread.org"
