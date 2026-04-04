"""MangaSnatcher

Developer: Gerald-H
GitHub: https://github.com/Gerald-Ha
Project: MangaSnatcher
Version: 3.0
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

DEFAULT_TIMEOUT = 30
DEFAULT_CHAPTER_COOLDOWN = 3
RETRY_CHAPTER_COOLDOWN = 8
ERROR_RETRY_DELAY = 60
BROWSER_STARTUP_TIMEOUT = 10
BROWSER_RENDER_TIMEOUT = 20
BROWSER_POLL_INTERVAL = 0.5
CHAPTER_URL_RE = re.compile(r"/chapter-(\d+)(?:[._-]\d+)?/?$", re.IGNORECASE)
CHAPTER_TITLE_RE = re.compile(r"\bchapter\s+(\d+)(?:[._-]\d+)?\b", re.IGNORECASE)
IMAGE_EXTENSION_FALLBACKS = (".jpg", ".jpeg", ".png", ".webp")
CHROMIUM_CANDIDATES = (
    "brave-browser",
    "brave",
    "chromium-browser",
    "chromium",
    "google-chrome",
)
BROWSER_COOKIE_DOMAIN = "www.mangaread.org"
BROWSER_COOKIE_CANDIDATES = ("brave", "chrome", "chromium", "firefox")
RENDERED_IMAGE_QUERY = """
(() => Array.from(
    document.querySelectorAll('.reading-content img.wp-manga-chapter-img, .reading-content img')
).map((img) => img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('src'))
 .filter(Boolean))()
""".strip()

BrowserImageFetcher = Callable[[str], list[str]]


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
        number = extract_chapter_number(chapter_url, title)
        chapters.append(Chapter(title=title or chapter_url, url=chapter_url, number=number))

    if chapters and all(chapter.number is not None for chapter in chapters):
        chapters.sort(key=lambda chapter: chapter.number or 0)
    return chapters


def extract_chapter_number(chapter_url: str, title: str) -> int | None:
    path = urlparse(chapter_url).path
    match = CHAPTER_URL_RE.search(path)
    if match:
        return int(match.group(1))

    title_match = CHAPTER_TITLE_RE.search(title)
    if title_match:
        return int(title_match.group(1))

    return None


def parse_chapter_images(html: str, chapter_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    selectors = (
        ".reading-content img.wp-manga-chapter-img, "
        ".reading-content img, "
        "img.wp-manga-chapter-img"
    )

    return normalize_image_sources(
        (
            first_non_empty(
                image.get("data-src"),
                image.get("data-lazy-src"),
                image.get("src"),
            )
            for image in soup.select(selectors)
        ),
        chapter_url,
    )


def has_reader_markup(html: str) -> bool:
    lowered = html.lower()
    return any(
        marker in lowered
        for marker in ("wp-manga-current-chap", "wp-manga-chapter-img", "page-break")
    )


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


def find_chromium_executable() -> str | None:
    for candidate in CHROMIUM_CANDIDATES:
        executable = shutil.which(candidate)
        if executable:
            return executable
    return None


def import_browser_cookie_module():
    try:
        import browser_cookie3  # type: ignore
    except ModuleNotFoundError:
        return None
    return browser_cookie3


def session_has_domain_cookie(session: requests.Session, domain: str) -> bool:
    normalized_domain = domain.lstrip(".")
    for cookie in session.cookies:
        cookie_domain = cookie.domain.lstrip(".")
        if (
            cookie_domain == normalized_domain
            or cookie_domain.endswith(f".{normalized_domain}")
            or normalized_domain.endswith(f".{cookie_domain}")
        ):
            return True
    return False


def add_cookiejar_to_session(session: requests.Session, cookiejar) -> int:
    added = 0
    for cookie in cookiejar:
        session.cookies.set_cookie(cookie)
        added += 1
    return added


def load_browser_cookies_into_session(
    session: requests.Session,
    browser_name: str = "auto",
    domain_name: str = BROWSER_COOKIE_DOMAIN,
) -> str | None:
    browser_cookie3 = import_browser_cookie_module()
    if browser_cookie3 is None:
        return None

    if browser_name == "none":
        return None

    candidate_names = (
        list(BROWSER_COOKIE_CANDIDATES)
        if browser_name == "auto"
        else [browser_name]
    )

    for candidate_name in candidate_names:
        loader = getattr(browser_cookie3, candidate_name, None)
        if loader is None:
            continue
        try:
            cookiejar = loader(domain_name=domain_name)
        except Exception:
            continue
        added = add_cookiejar_to_session(session, cookiejar)
        if added > 0 and session_has_domain_cookie(session, domain_name):
            return candidate_name

    return None


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


class ChromeDevToolsClient:
    def __init__(self, websocket_url: str, timeout: int) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self._next_id = 0
        self._read_buffer = bytearray()

    def __enter__(self) -> ChromeDevToolsClient:
        self.socket = self._connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.socket is None:
            return
        try:
            self._send_frame("", opcode=0x8)
        except OSError:
            pass
        self.socket.close()
        self.socket = None

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message_id = self._next_id
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._send_json(payload)

        while True:
            message = self._receive_json()
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome DevTools error for {method}: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        return result.get("result", {}).get("value")

    def _connect(self) -> socket.socket:
        parsed = urlparse(self.websocket_url)
        if parsed.scheme != "ws":
            raise RuntimeError(f"Unsupported DevTools websocket scheme: {parsed.scheme}")

        sock = socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 80),
            timeout=self.timeout,
        )
        sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("Chrome DevTools websocket closed during handshake.")
            response += chunk

        header_bytes, remainder = response.split(b"\r\n\r\n", 1)
        self._read_buffer.extend(remainder)
        header_text = header_bytes.decode("utf-8", errors="replace")
        status_line, headers = self._parse_http_headers(header_text)
        status_parts = status_line.split()
        if len(status_parts) < 2 or status_parts[1] != "101":
            raise RuntimeError(f"Chrome DevTools handshake failed: {header_text}")

        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        actual_accept = headers.get("sec-websocket-accept", "")
        if actual_accept != expected_accept:
            raise RuntimeError("Chrome DevTools handshake returned an unexpected accept key.")

        return sock

    @staticmethod
    def _parse_http_headers(header_text: str) -> tuple[str, dict[str, str]]:
        lines = [line for line in header_text.split("\r\n") if line]
        if not lines:
            raise RuntimeError("Chrome DevTools handshake returned an empty response.")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return lines[0], headers

    def _send_json(self, payload: dict) -> None:
        self._send_frame(json.dumps(payload), opcode=0x1)

    def _send_frame(self, payload: str, opcode: int) -> None:
        if self.socket is None:
            raise RuntimeError("Chrome DevTools socket is not connected.")

        encoded = payload.encode("utf-8")
        frame = bytearray()
        frame.append(0x80 | opcode)

        payload_length = len(encoded)
        if payload_length < 126:
            frame.append(0x80 | payload_length)
        elif payload_length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack("!H", payload_length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack("!Q", payload_length))

        mask = os.urandom(4)
        frame.extend(mask)
        frame.extend(byte ^ mask[index % 4] for index, byte in enumerate(encoded))
        self.socket.sendall(frame)

    def _receive_json(self) -> dict:
        while True:
            opcode, payload = self._receive_frame()
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 0x8:
                raise RuntimeError("Chrome DevTools websocket closed unexpectedly.")
            if opcode == 0x9:
                self._send_frame(payload.decode("utf-8", errors="ignore"), opcode=0xA)

    def _receive_frame(self) -> tuple[int, bytes]:
        if self.socket is None:
            raise RuntimeError("Chrome DevTools socket is not connected.")

        first_byte, second_byte = self._read_exactly(2)
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        payload_length = second_byte & 0x7F

        if payload_length == 126:
            payload_length = struct.unpack("!H", self._read_exactly(2))[0]
        elif payload_length == 127:
            payload_length = struct.unpack("!Q", self._read_exactly(8))[0]

        mask = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(payload_length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _read_exactly(self, size: int) -> bytes:
        if self.socket is None:
            raise RuntimeError("Chrome DevTools socket is not connected.")

        chunks = bytearray()
        if self._read_buffer:
            buffered = self._read_buffer[:size]
            chunks.extend(buffered)
            del self._read_buffer[: len(buffered)]
        while len(chunks) < size:
            chunk = self.socket.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("Chrome DevTools websocket closed unexpectedly.")
            chunks.extend(chunk)
        return bytes(chunks)


def wait_for_debugger_target(
    port: int,
    browser_process: subprocess.Popen[str],
    timeout: int,
    target_url: str | None = None,
) -> str:
    debugger_url = f"http://127.0.0.1:{port}/json/list"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if browser_process.poll() is not None:
            stderr_output = ""
            if browser_process.stderr is not None:
                stderr_output = browser_process.stderr.read().strip()
            raise RuntimeError(
                "Chromium exited before the DevTools endpoint became available. "
                f"{stderr_output or 'No stderr output was captured.'}"
            )

        try:
            with urlopen(debugger_url, timeout=1) as response:
                targets = json.load(response)
        except OSError:
            time.sleep(0.2)
            continue

        matching_page_url: str | None = None
        for target in targets:
            websocket_url = target.get("webSocketDebuggerUrl")
            page_url = str(target.get("url") or "")
            if target.get("type") != "page" or not websocket_url:
                continue
            if target_url and page_url.startswith(target_url):
                return str(websocket_url)
            if page_url and page_url != "about:blank" and matching_page_url is None:
                matching_page_url = str(websocket_url)
        if matching_page_url:
            return matching_page_url
        time.sleep(0.2)

    raise RuntimeError("Timed out while waiting for Chromium DevTools to become available.")


def close_browser_process(browser_process: subprocess.Popen[str]) -> None:
    if browser_process.poll() is None:
        browser_process.terminate()
        try:
            browser_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser_process.kill()
            browser_process.wait(timeout=10)
    if browser_process.stderr is not None:
        browser_process.stderr.close()


def cleanup_browser_profile(user_data_dir: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            shutil.rmtree(user_data_dir)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                shutil.rmtree(user_data_dir, ignore_errors=True)
                return
            time.sleep(0.5)


def fetch_chapter_images_with_browser(chapter_url: str) -> list[str]:
    chromium_executable = find_chromium_executable()
    if not chromium_executable:
        raise RuntimeError("Chromium is not installed, so browser fallback is unavailable.")

    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        raise RuntimeError(
            "Browser fallback requires a graphical session on Linux "
            "(missing DISPLAY or WAYLAND_DISPLAY)."
        )

    debug_port = reserve_local_port()
    user_data_dir = tempfile.mkdtemp(prefix="manga_snatcher_chromium_")

    browser_process = subprocess.Popen(
        [
            chromium_executable,
            f"--remote-debugging-port={debug_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={user_data_dir}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            chapter_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        websocket_url = wait_for_debugger_target(
            debug_port,
            browser_process,
            timeout=BROWSER_STARTUP_TIMEOUT,
            target_url=chapter_url,
        )
        with ChromeDevToolsClient(websocket_url, timeout=DEFAULT_TIMEOUT) as client:
            client.call("Page.enable")
            client.call("Runtime.enable")
            client.call("Page.bringToFront")

            deadline = time.monotonic() + BROWSER_RENDER_TIMEOUT
            while time.monotonic() < deadline:
                rendered_sources = client.evaluate(RENDERED_IMAGE_QUERY)
                if isinstance(rendered_sources, list) and rendered_sources:
                    images = normalize_image_sources(rendered_sources, chapter_url)
                    if images:
                        return images

                client.evaluate("window.scrollTo(0, document.body.scrollHeight); true")
                time.sleep(BROWSER_POLL_INTERVAL)
    finally:
        close_browser_process(browser_process)
        cleanup_browser_profile(user_data_dir)

    raise RuntimeError("Chromium rendered the page but no chapter images were exposed in the DOM.")


def resolve_chapter_images(
    chapter_html: str,
    chapter_url: str,
    browser_image_fetcher: BrowserImageFetcher | None = fetch_chapter_images_with_browser,
) -> list[str]:
    image_urls = parse_chapter_images(chapter_html, chapter_url)
    if image_urls:
        return image_urls

    browser_error: RuntimeError | None = None
    if browser_image_fetcher is not None:
        try:
            browser_image_urls = browser_image_fetcher(chapter_url)
        except RuntimeError as exc:
            browser_error = exc
        else:
            if browser_image_urls:
                return normalize_image_sources(browser_image_urls, chapter_url)

    if not has_reader_markup(chapter_html):
        detail = f" Browser fallback failed: {browser_error}" if browser_error else ""
        raise RuntimeError(
            "The fetched HTML does not contain manga reader markup. "
            "The site may be loading the chapter client-side or serving "
            f"different content to automated requests for {chapter_url}.{detail}"
        )

    detail = f" Browser fallback failed: {browser_error}" if browser_error else ""
    raise RuntimeError(f"No images were found in {chapter_url}.{detail}")


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
        "- a chapter range, e.g. '120-160'\n"
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
    return sort_selected_chapters(selected)


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
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start_text = start_text.strip()
            end_text = end_text.strip()
            if start_text.isdigit() and end_text.isdigit():
                start_number = int(start_text)
                end_number = int(end_text)
                lower, upper = sorted((start_number, end_number))
                numbers.update(range(lower, upper + 1))
                continue
        raise ValueError(
            "Invalid selection. Use 'all', one number, a range like '120-160', "
            "or multiple comma-separated numbers."
        )
    if not numbers:
        raise ValueError("No valid chapter number was found.")
    return numbers


def sort_selected_chapters(chapters: list[Chapter]) -> list[Chapter]:
    return sorted(
        chapters,
        key=lambda chapter: (
            chapter.number is None,
            -(chapter.number if chapter.number is not None else -1),
            chapter.title.lower(),
            chapter.url,
        ),
    )


def build_image_url_candidates(image_url: str) -> list[str]:
    parsed = urlparse(image_url)
    path = parsed.path
    lower_path = path.lower()
    for extension in IMAGE_EXTENSION_FALLBACKS:
        if lower_path.endswith(extension):
            base_path = path[: -len(extension)]
            file_name = Path(base_path).name
            parent_path = str(Path(base_path).parent)
            candidates = [image_url]

            def add_candidate(candidate_path: str) -> None:
                candidate_url = parsed._replace(path=candidate_path).geturl()
                if candidate_url not in candidates:
                    candidates.append(candidate_url)

            for candidate_extension in IMAGE_EXTENSION_FALLBACKS:
                add_candidate(f"{base_path}{candidate_extension}")

            if file_name.isdigit() and len(file_name) >= 2:
                repaired_base_path = (
                    f"{parent_path}/{file_name}0"
                    if parent_path not in {"", ".", "/"}
                    else f"/{file_name}0"
                )
                for candidate_extension in IMAGE_EXTENSION_FALLBACKS:
                    add_candidate(f"{repaired_base_path}{candidate_extension}")

            return candidates
    return [image_url]


def download_chapter_to_pdf(
    session: requests.Session,
    chapter: Chapter,
    series_title: str,
    output_dir: Path,
    browser_image_fetcher: BrowserImageFetcher | None = fetch_chapter_images_with_browser,
) -> Path:
    print(f"\nDownloading {chapter.display_name} ...")
    chapter_html = fetch_html(session, chapter.url)
    ensure_not_under_construction(chapter_html)
    image_urls = resolve_chapter_images(
        chapter_html,
        chapter.url,
        browser_image_fetcher=browser_image_fetcher,
    )

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
    last_error: requests.RequestException | None = None
    image_url_candidates = build_image_url_candidates(image_url)

    for index, candidate_url in enumerate(image_url_candidates):
        response = session.get(
            candidate_url,
            timeout=DEFAULT_TIMEOUT,
            headers={"Referer": referer},
            stream=True,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            last_error = exc
            status_code = response.status_code
            if status_code == 404 and index < len(image_url_candidates) - 1:
                response.close()
                continue
            response.close()
            raise

        with destination.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if chunk:
                    file_handle.write(chunk)
        response.close()
        return

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Image download failed for {image_url}.")


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
    parser.add_argument(
        "--no-browser-fallback",
        action="store_true",
        help="Disable the Chromium fallback for chapters that hide images from direct requests.",
    )
    parser.add_argument(
        "--browser-cookies",
        choices=("auto", "brave", "chrome", "chromium", "firefox", "none"),
        default="auto",
        help=(
            "Load mangaread cookies from a local browser profile before fetching chapters "
            "(default: auto)."
        ),
    )
    args = parser.parse_args()

    try:
        input_url = prompt_for_url(args.url)
        series_url = normalize_series_url(input_url)
        session = build_session()
        cookie_source = load_browser_cookies_into_session(session, args.browser_cookies)
        if cookie_source:
            print(f"Loaded browser cookies from: {cookie_source}")

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

        browser_image_fetcher = None if args.no_browser_fallback else fetch_chapter_images_with_browser

        def chapter_downloader(active_session, chapter, active_series_title, active_output_dir):
            return download_chapter_to_pdf(
                active_session,
                chapter,
                active_series_title,
                active_output_dir,
                browser_image_fetcher=browser_image_fetcher,
            )

        download_selected_chapters(
            session,
            selected_chapters,
            series_title,
            output_dir,
            chapter_downloader=chapter_downloader,
        )

        print("\nDone.")
        return 0
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
