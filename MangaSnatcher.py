"""MangaSnatcher

Developer: Gerald-H
GitHub: https://github.com/Gerald-Ha
Project: MangaSnatcher
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

DEFAULT_TIMEOUT = 30
DEFAULT_CHAPTER_COOLDOWN = 3
RETRY_CHAPTER_COOLDOWN = 8
ERROR_RETRY_DELAY = 60
CHAPTER_URL_RE = re.compile(r"/chapter-(\d+)/?$", re.IGNORECASE)


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


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def normalize_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("The URL cannot be empty.")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned


def normalize_series_url(url: str) -> str:
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


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return value or "download"


def extract_title(html: str, fallback_url: str) -> str:
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


def ensure_not_under_construction(html: str) -> None:
    lowered = html.lower()
    if "under-construction-page" in lowered:
        raise RuntimeError(
            "The site is currently returning an under-construction page. "
            "Once the real chapter pages are reachable again, "
            "the downloader should work with the same parser logic."
        )


def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.text


def parse_chapters(html: str, base_url: str) -> list[Chapter]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select(".listing-chapters_wrap li.wp-manga-chapter a")
    if not anchors:
        anchors = soup.select("a[href*='/chapter-']")

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
        match = CHAPTER_URL_RE.search(urlparse(chapter_url).path)
        number = int(match.group(1)) if match else None
        chapters.append(Chapter(title=title or chapter_url, url=chapter_url, number=number))

    if chapters and all(chapter.number is not None for chapter in chapters):
        chapters.sort(key=lambda chapter: chapter.number or 0)
    return chapters


def parse_chapter_images(html: str, chapter_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    selectors = (
        ".reading-content img.wp-manga-chapter-img, "
        ".reading-content img, "
        "img.wp-manga-chapter-img"
    )

    images: list[str] = []
    seen: set[str] = set()
    for image in soup.select(selectors):
        source = first_non_empty(
            image.get("data-src"),
            image.get("data-lazy-src"),
            image.get("src"),
        )
        if not source:
            continue
        source = re.sub(r"\s+", "", source)
        absolute_source = urljoin(chapter_url, source)
        if absolute_source in seen:
            continue
        seen.add(absolute_source)
        images.append(absolute_source)
    return images


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def print_chapter_overview(chapters: Iterable[Chapter]) -> None:
    chapter_list = list(chapters)
    print(f"\nFound chapters: {len(chapter_list)}")
    for chapter in chapter_list[:10]:
        print(f"- {chapter.display_name}: {chapter.url}")
    if len(chapter_list) > 10:
        print(f"- ... and {len(chapter_list) - 10} more")


def prompt_for_url(argv_url: str | None) -> str:
    if argv_url:
        return argv_url
    typed_url = input("Enter a series or chapter URL: ").strip()
    if not typed_url:
        raise ValueError("No URL was provided.")
    return typed_url


def choose_chapters(chapters: list[Chapter]) -> list[Chapter]:
    print_chapter_overview(chapters)
    prompt = (
        "\nWhich chapters should be downloaded?\n"
        "- 'all' for all chapters\n"
        "- a chapter number, e.g. '12'\n"
        "- multiple numbers, e.g. '1,2,5'\n"
        "Selection: "
    )
    choice = input(prompt).strip().lower()
    if choice == "all":
        return chapters

    requested_numbers = parse_requested_numbers(choice)
    selected = [chapter for chapter in chapters if chapter.number in requested_numbers]
    if not selected:
        raise ValueError("No matching chapter was found for the selected input.")
    return selected


def parse_requested_numbers(choice: str) -> set[int]:
    if not choice:
        raise ValueError("The selection cannot be empty.")

    numbers: set[int] = set()
    for part in choice.split(","):
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            numbers.add(int(token))
            continue
        raise ValueError(
            "Invalid selection. Use 'all', one number, or multiple comma-separated numbers."
        )
    if not numbers:
        raise ValueError("No valid chapter number was found.")
    return numbers


def download_chapter_to_pdf(
    session: requests.Session,
    chapter: Chapter,
    series_title: str,
    output_dir: Path,
) -> Path:
    print(f"\nDownloading {chapter.display_name} ...")
    chapter_html = fetch_html(session, chapter.url)
    ensure_not_under_construction(chapter_html)
    image_urls = parse_chapter_images(chapter_html, chapter.url)
    if not image_urls:
        raise RuntimeError(f"No images were found in {chapter.url}.")

    chapter_folder = output_dir / slugify(series_title)
    chapter_folder.mkdir(parents=True, exist_ok=True)
    pdf_name = f"{slugify(series_title)}-{slugify(chapter.display_name)}.pdf"
    pdf_path = chapter_folder / pdf_name

    with tempfile.TemporaryDirectory(prefix="manga_snatcher_") as temp_dir:
        temp_path = Path(temp_dir)
        image_paths: list[Path] = []
        for index, image_url in enumerate(image_urls, start=1):
            image_path = temp_path / f"{index:03d}.jpg"
            download_image(session, image_url, image_path, referer=chapter.url)
            image_paths.append(image_path)

        export_images_to_pdf(image_paths, pdf_path)

    print(f"PDF created: {pdf_path}")
    return pdf_path


def wait_for_retry(sleep_seconds: int, chapter: Chapter) -> None:
    print(
        f"Download failed for {chapter.display_name}. "
        f"Waiting {sleep_seconds} seconds before retrying..."
    )
    time.sleep(sleep_seconds)


def wait_between_chapters(sleep_seconds: int) -> None:
    print(f"Waiting {sleep_seconds} seconds before the next chapter...")
    time.sleep(sleep_seconds)


def download_selected_chapters(
    session: requests.Session,
    chapters: list[Chapter],
    series_title: str,
    output_dir: Path,
    chapter_downloader=download_chapter_to_pdf,
    retry_waiter=wait_for_retry,
    chapter_waiter=wait_between_chapters,
) -> None:
    current_cooldown = DEFAULT_CHAPTER_COOLDOWN

    for index, chapter in enumerate(chapters):
        try:
            chapter_downloader(session, chapter, series_title, output_dir)
        except (requests.RequestException, RuntimeError, OSError) as exc:
            print(f"Error while downloading {chapter.display_name}: {exc}")
            retry_waiter(ERROR_RETRY_DELAY, chapter)
            print(f"Retrying {chapter.display_name}...")
            chapter_downloader(session, chapter, series_title, output_dir)
            current_cooldown = RETRY_CHAPTER_COOLDOWN

        if len(chapters) > 1 and index < len(chapters) - 1:
            chapter_waiter(current_cooldown)


def download_image(
    session: requests.Session, image_url: str, destination: Path, referer: str
) -> None:
    response = session.get(
        image_url,
        timeout=DEFAULT_TIMEOUT,
        headers={"Referer": referer},
        stream=True,
    )
    response.raise_for_status()
    with destination.open("wb") as file_handle:
        for chunk in response.iter_content(chunk_size=1024 * 64):
            if chunk:
                file_handle.write(chunk)


def export_images_to_pdf(image_paths: list[Path], pdf_path: Path) -> None:
    converted_images: list[Image.Image] = []
    for image_path in image_paths:
        with Image.open(image_path) as image:
            converted_images.append(normalize_image_for_pdf(image))

    if not converted_images:
        raise RuntimeError("No images could be loaded for the PDF.")

    first_image, remaining_images = converted_images[0], converted_images[1:]
    try:
        first_image.save(
            pdf_path,
            save_all=True,
            append_images=remaining_images,
            resolution=100.0,
        )
    finally:
        for image in converted_images:
            image.close()


def normalize_image_for_pdf(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()

    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, "white")
        alpha = image.getchannel("A")
        background.paste(image.convert("RGBA"), mask=alpha)
        return background

    return image.convert("RGB")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download manga chapter images and export them as a PDF."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Series or chapter URL",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="downloads",
        help="Output directory for generated PDFs (default: downloads)",
    )
    args = parser.parse_args()

    try:
        input_url = prompt_for_url(args.url)
        series_url = normalize_series_url(input_url)
        session = build_session()

        print(f"Fetching series page: {series_url}")
        series_html = fetch_html(session, series_url)
        ensure_not_under_construction(series_html)
        series_title = extract_title(series_html, series_url)
        chapters = parse_chapters(series_html, series_url)
        if not chapters:
            raise RuntimeError("No chapters were found on the series page.")

        selected_chapters = choose_chapters(chapters)
        output_dir = Path(args.output).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        download_selected_chapters(session, selected_chapters, series_title, output_dir)

        print("\nDone.")
        return 0
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
