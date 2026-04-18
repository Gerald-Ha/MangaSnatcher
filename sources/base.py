from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Chapter:
    title: str
    url: str
    number: int | None

    @property
    def display_name(self) -> str:
        if self.number is not None:
            return f"Chapter {self.number}"
        return self.title


def normalize_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("The URL cannot be empty.")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned


def hostname_from_url(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return (parsed.hostname or "").lower().removeprefix("www.")


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def normalize_image_sources(sources: Iterable[str | None], chapter_url: str) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not source:
            continue
        cleaned_source = re.sub(r"\s+", "", source)
        absolute_source = urljoin(chapter_url, cleaned_source)
        if absolute_source in seen:
            continue
        seen.add(absolute_source)
        images.append(absolute_source)
    return images


class SourceAdapter:
    name = "Unknown"
    domains: tuple[str, ...] = ()
    cookie_domain: str | None = None

    @property
    def download_folder(self) -> str:
        return self.domains[0]

    def supports_url(self, url: str) -> bool:
        hostname = hostname_from_url(url)
        return any(hostname == domain for domain in self.domains)

    def normalize_series_url(self, url: str) -> str:
        raise NotImplementedError

    def extract_title(self, html: str, fallback_url: str) -> str:
        raise NotImplementedError

    def parse_chapters(self, html: str, base_url: str) -> list[Chapter]:
        raise NotImplementedError

    def extract_chapter_number(self, chapter_url: str, title: str) -> int | None:
        raise NotImplementedError

    def parse_chapter_images(self, html: str, chapter_url: str) -> list[str]:
        raise NotImplementedError

    def has_reader_markup(self, html: str) -> bool:
        raise NotImplementedError


class WordPressMangaSource(SourceAdapter):
    chapter_url_re = re.compile(r"/chapter-(\d+)(?:[._-]\d+)?/?$", re.IGNORECASE)
    chapter_title_re = re.compile(r"\bchapter\s+(\d+)(?:[._-]\d+)?\b", re.IGNORECASE)
    chapter_anchor_selectors = (
        ".listing-chapters_wrap li.wp-manga-chapter a",
        "a[href*='/chapter-']",
    )
    image_selectors = (
        ".reading-content img.wp-manga-chapter-img, "
        ".reading-content img, "
        "img.wp-manga-chapter-img"
    )
    reader_markup_markers = ("wp-manga-current-chap", "wp-manga-chapter-img", "page-break")

    def normalize_series_url(self, url: str) -> str:
        parsed = urlparse(normalize_url(url))
        parts = [part for part in parsed.path.split("/") if part]
        if "chapter" in parts:
            chapter_index = parts.index("chapter")
            parts = parts[:chapter_index]
        elif parts and parts[-1].startswith("chapter-"):
            parts = parts[:-1]
        if len(parts) < 2:
            raise ValueError("The URL does not look like a valid series or chapter URL.")
        normalized_path = "/" + "/".join(parts) + "/"
        return f"{parsed.scheme or 'https'}://{parsed.netloc}{normalized_path}"

    def extract_title(self, html: str, fallback_url: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        meta_title = soup.find("meta", property="og:title")
        if meta_title and meta_title.get("content"):
            return meta_title["content"].strip()
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            if title:
                return re.sub(r"\s*[-|].*$", "", title).strip() or title
        slug = [part for part in urlparse(fallback_url).path.split("/") if part]
        if len(slug) >= 2:
            return slug[-1].replace("-", " ").strip()
        return "manga-download"

    def parse_chapters(self, html: str, base_url: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        anchors = []
        for selector in self.chapter_anchor_selectors:
            anchors = soup.select(selector)
            if anchors:
                break

        chapters: list[Chapter] = []
        seen: set[str] = set()
        for anchor in anchors:
            href = anchor.get("href")
            if not href:
                continue
            chapter_url = urljoin(base_url, href.strip())
            if chapter_url in seen:
                continue
            seen.add(chapter_url)
            title = " ".join(anchor.get_text(" ", strip=True).split())
            number = self.extract_chapter_number(chapter_url, title)
            chapters.append(Chapter(title=title or chapter_url, url=chapter_url, number=number))

        if chapters and all(chapter.number is not None for chapter in chapters):
            chapters.sort(key=lambda chapter: chapter.number or 0)
        return chapters

    def extract_chapter_number(self, chapter_url: str, title: str) -> int | None:
        path = urlparse(chapter_url).path
        match = self.chapter_url_re.search(path)
        if match:
            return int(match.group(1))

        title_match = self.chapter_title_re.search(title)
        if title_match:
            return int(title_match.group(1))

        return None

    def parse_chapter_images(self, html: str, chapter_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        return normalize_image_sources(
            (
                first_non_empty(
                    image.get("data-src"),
                    image.get("data-lazy-src"),
                    image.get("src"),
                )
                for image in soup.select(self.image_selectors)
            ),
            chapter_url,
        )

    def has_reader_markup(self, html: str) -> bool:
        lowered = html.lower()
        return any(marker in lowered for marker in self.reader_markup_markers)
