from __future__ import annotations

from .base import WordPressMangaSource


class ManhuaUSSource(WordPressMangaSource):
    name = "ManhuaUS"
    domains = ("manhuaus.org",)
    cookie_domain = "manhuaus.org"
