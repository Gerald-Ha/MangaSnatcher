"""MangaSnatcher

Developer: Gerald-H
GitHub: https://github.com/Gerald-Ha
Project: MangaSnatcher
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.request import urlopen

import requests
from PIL import Image, ImageFile

from sources import Chapter, MangaReadSource, SourceAdapter, get_source_adapter, normalize_image_sources

ImageFile.LOAD_TRUNCATED_IMAGES = True

APP_VERSION = os.environ.get("APP_VERSION", "4.0.0")
UPDATE_SERVER_URL = os.environ.get(
    "UPDATE_SERVER_URL",
    "https://update.gerald-hasani.com",
)
UPDATE_PROJECT_ID = os.environ.get("UPDATE_PROJECT_ID", "mangasnatcher")
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "stable")
UPDATE_API_KEY_ENV_NAMES = ("MANGASNATCHER_UPDATE_API_KEY", "UPDATE_API_KEY")
DEFAULT_UPDATE_API_KEY = "upd_f75197c5cce29eac237ae3024d15375ba18cba751d061a9e573daa328a31792b"
UPDATE_CHECK_TIMEOUT = 5
DEFAULT_TIMEOUT = 30
DEFAULT_CHAPTER_COOLDOWN = 3
RETRY_CHAPTER_COOLDOWN = 8
ERROR_RETRY_DELAY = 60
BROWSER_STARTUP_TIMEOUT = 10
BROWSER_RENDER_TIMEOUT = 20
BROWSER_POLL_INTERVAL = 0.5
CLOUDFLARE_STATUS_CODES = {403, 429, 503}
CLOUDFLARE_MARKERS = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "attention required",
    "cf-browser-verification",
    "__cf_chl",
    "cf_chl",
    "challenge-platform",
    "cloudflare",
)
IMAGE_EXTENSION_FALLBACKS = (".jpg", ".jpeg", ".png", ".webp")
CHROMIUM_CANDIDATES = (
    "brave-browser",
    "brave",
    "chromium-browser",
    "chromium",
    "google-chrome",
)
BROWSER_COOKIE_CANDIDATES = (
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "vivaldi",
)
DEFAULT_SOURCE_ADAPTER = MangaReadSource()
RENDERED_IMAGE_QUERY = """
(() => Array.from(
    document.querySelectorAll('.reading-content img.wp-manga-chapter-img, .reading-content img')
).map((img) => img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('src'))
 .filter(Boolean))()
""".strip()

BrowserImageFetcher = Callable[[str], list[str]]
HtmlFetcher = Callable[[requests.Session, str], str]


@dataclass
class DownloadRunResult:
    successful_chapters: list[Chapter] = field(default_factory=list)
    failed_chapters: list[Chapter] = field(default_factory=list)


@dataclass(frozen=True)
class UpdateCheckResult:
    status: str
    current_version: str
    latest_version: str | None = None
    minimum_supported: str | None = None
    critical: bool | None = None
    released_at: str | None = None
    update_link: str | None = None
    notes_url: str | None = None
    message: str | None = None


class CloudflareProtectionError(RuntimeError):
    def __init__(self, url: str, status_code: int | None = None) -> None:
        self.url = url
        self.status_code = status_code
        status_detail = f" (HTTP {status_code})" if status_code else ""
        super().__init__(
            "Cloudflare or anti-bot protection blocked the request"
            f"{status_detail}: {url}"
        )


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


def get_update_api_key() -> str | None:
    for env_name in UPDATE_API_KEY_ENV_NAMES:
        api_key = os.environ.get(env_name)
        if api_key:
            return api_key
    return DEFAULT_UPDATE_API_KEY


def remove_none_values(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            cleaned_item = remove_none_values(item)
            if cleaned_item is not None:
                cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, list):
        return [item for item in (remove_none_values(item) for item in value) if item is not None]
    return value


def build_update_check_payload(
    project_id: str,
    current_version: str,
    channel: str,
) -> dict:
    return remove_none_values({
        "project": {
            "id": project_id,
            "instance_id": str(uuid.uuid4()),
        },
        "current": {
            "version": current_version,
            "build": os.environ.get("BUILD_NUMBER"),
            "commit": os.environ.get("GIT_COMMIT"),
            "image_digest": os.environ.get("DOCKER_IMAGE_DIGEST"),
        },
        "channel": channel,
        "platform": {
            "os": platform.system().lower(),
            "distro": platform.platform(),
            "arch": platform.machine(),
            "container": os.environ.get("DOCKER_CONTAINER"),
        },
        "capabilities": {
            "accept_prerelease": False,
            "supports_delta": False,
        },
    })


def parse_update_check_response(data: dict, current_version: str) -> UpdateCheckResult:
    update_info = data.get("update") if isinstance(data.get("update"), dict) else {}
    current_info = data.get("current") if isinstance(data.get("current"), dict) else {}
    return UpdateCheckResult(
        status=str(data.get("status") or "unknown"),
        current_version=str(current_info.get("version") or current_version),
        latest_version=update_info.get("latest_version"),
        minimum_supported=update_info.get("minimum_supported"),
        critical=update_info.get("critical"),
        released_at=update_info.get("released_at"),
        update_link=update_info.get("update_link"),
        notes_url=update_info.get("notes_url"),
        message=data.get("message") or update_info.get("message"),
    )


def check_for_updates(
    api_key: str,
    project_id: str = UPDATE_PROJECT_ID,
    current_version: str = APP_VERSION,
    update_server_url: str = UPDATE_SERVER_URL,
    channel: str = UPDATE_CHANNEL,
    request_post: Callable[..., requests.Response] = requests.post,
) -> UpdateCheckResult:
    endpoint = f"{update_server_url.rstrip('/')}/api/updates/v1/updates/check"
    payload = build_update_check_payload(project_id, current_version, channel)
    request_id = str(uuid.uuid4())

    try:
        response = request_post(
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Request-ID": request_id,
            },
            timeout=UPDATE_CHECK_TIMEOUT,
        )
        response_data = response.json()
        response.raise_for_status()
        return parse_update_check_response(response_data, current_version)
    except requests.HTTPError as exc:
        response = exc.response
        response_data = {}
        if response is not None:
            try:
                response_data = response.json()
            except ValueError:
                response_data = {}
        message = response_data.get("message") or str(exc)
        status = str(response_data.get("status") or "error")
        if status == "unknown_project":
            status = "unknown"
        return UpdateCheckResult(
            status=status,
            current_version=current_version,
            message=message,
        )
    except (requests.RequestException, ValueError) as exc:
        return UpdateCheckResult(
            status="error",
            current_version=current_version,
            message=str(exc),
        )


def check_mangasnatcher_updates() -> UpdateCheckResult:
    api_key = get_update_api_key()
    if not api_key:
        return UpdateCheckResult(
            status="skipped",
            current_version=APP_VERSION,
            message=(
                "No update API key configured. Set MANGASNATCHER_UPDATE_API_KEY "
                "or UPDATE_API_KEY to enable startup update checks."
            ),
        )

    return check_for_updates(
        api_key=api_key,
        project_id=UPDATE_PROJECT_ID,
        current_version=APP_VERSION,
        update_server_url=UPDATE_SERVER_URL,
        channel=UPDATE_CHANNEL,
    )


def print_update_check_result(result: UpdateCheckResult) -> None:
    if result.status == "up_to_date":
        print(f"Update check: MangaSnatcher is up to date (version {result.current_version}).")
        return

    if result.status == "update_available":
        latest = result.latest_version or "unknown"
        print(
            "Update available: "
            f"MangaSnatcher {latest} is available "
            f"(installed: {result.current_version})."
        )
        if result.critical:
            print("This update is marked as critical.")
        if result.update_link:
            print(f"Update link: {result.update_link}")
        if result.notes_url:
            print(f"Release notes: {result.notes_url}")
        return

    if result.status == "blocked":
        latest = result.latest_version or "unknown"
        minimum = result.minimum_supported or "unknown"
        print(
            "Update required: "
            f"MangaSnatcher {result.current_version} is no longer supported. "
            f"Latest version: {latest}. Minimum supported: {minimum}."
        )
        if result.message:
            print(result.message)
        return

    if result.status == "skipped":
        print(f"Update check: skipped ({result.message})")
        return

    detail = f" ({result.message})" if result.message else ""
    print(f"Update check: unavailable{detail}")


def normalize_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("The URL cannot be empty.")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned


def normalize_series_url(url: str) -> str:
    return DEFAULT_SOURCE_ADAPTER.normalize_series_url(url)


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return value or "download"


def extract_title(html: str, fallback_url: str) -> str:
    return DEFAULT_SOURCE_ADAPTER.extract_title(html, fallback_url)


def ensure_not_under_construction(html: str) -> None:
    lowered = html.lower()
    if "under-construction-page" in lowered:
        raise RuntimeError(
            "The site is currently returning an under-construction page. "
            "Once the real chapter pages are reachable again, "
            "the downloader should work with the same parser logic."
        )


def is_cloudflare_challenge_html(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in CLOUDFLARE_MARKERS)


def response_has_cloudflare_headers(response: requests.Response) -> bool:
    server = response.headers.get("server", "").lower()
    if "cloudflare" in server:
        return True
    return any(header_name.lower().startswith("cf-") for header_name in response.headers)


def is_cloudflare_response(response: requests.Response) -> bool:
    if is_cloudflare_challenge_html(response.text):
        return True
    if response.status_code not in CLOUDFLARE_STATUS_CODES:
        return False
    return response.status_code == 403 or response_has_cloudflare_headers(response)


def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=DEFAULT_TIMEOUT)
    if is_cloudflare_response(response):
        raise CloudflareProtectionError(url, response.status_code)
    response.raise_for_status()
    return response.text


def parse_chapters(html: str, base_url: str) -> list[Chapter]:
    return DEFAULT_SOURCE_ADAPTER.parse_chapters(html, base_url)


def extract_chapter_number(chapter_url: str, title: str) -> int | None:
    return DEFAULT_SOURCE_ADAPTER.extract_chapter_number(chapter_url, title)


def parse_chapter_images(html: str, chapter_url: str) -> list[str]:
    return DEFAULT_SOURCE_ADAPTER.parse_chapter_images(html, chapter_url)


def has_reader_markup(html: str) -> bool:
    return DEFAULT_SOURCE_ADAPTER.has_reader_markup(html)


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


def cookie_domain_matches(cookie_domain: str, domain: str) -> bool:
    normalized_cookie_domain = cookie_domain.lstrip(".")
    normalized_domain = domain.lstrip(".")
    return (
        normalized_cookie_domain == normalized_domain
        or normalized_cookie_domain.endswith(f".{normalized_domain}")
        or normalized_domain.endswith(f".{normalized_cookie_domain}")
    )


def add_cookiejar_to_session(session: requests.Session, cookiejar) -> int:
    added = 0
    for cookie in cookiejar:
        session.cookies.set_cookie(cookie)
        added += 1
    return added


def add_browser_cookies_to_session(
    session: requests.Session,
    cookies: Iterable[dict],
    domain_name: str,
) -> int:
    added = 0
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        cookie_domain = cookie.get("domain") or domain_name
        if not name or value is None:
            continue
        if not cookie_domain_matches(str(cookie_domain), domain_name):
            continue

        session.cookies.set(
            str(name),
            str(value),
            domain=str(cookie_domain),
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure")),
        )
        added += 1
    return added


def load_browser_cookies_into_session(
    session: requests.Session,
    browser_name: str = "auto",
    domain_name: str = DEFAULT_SOURCE_ADAPTER.cookie_domain or DEFAULT_SOURCE_ADAPTER.download_folder,
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


def prompt_for_cloudflare_browser_retry(
    url: str,
    input_func: Callable[[str], str] = input,
    browser_opener: Callable[..., bool] = webbrowser.open,
) -> bool:
    print()
    print("Cloudflare or anti-bot protection appears to be blocking this request.")
    print(
        "MangaSnatcher cannot solve that challenge automatically, but it can "
        "retry after you open the page in your normal browser."
    )
    print(
        "Your system default browser will be used. After the page loads, "
        "MangaSnatcher will try to reuse browser cookies when possible."
    )

    try:
        answer = input_func("Open the page in your default browser now? [y/N]: ").strip().lower()
    except EOFError:
        return False

    if answer not in {"y", "yes", "j", "ja"}:
        return False

    try:
        opened = browser_opener(url, new=2)
    except Exception as exc:
        opened = False
        print(f"Could not open the browser automatically: {exc}")

    if opened:
        print("Browser opened. Complete the challenge or make sure the page loads normally.")
    else:
        print(f"Please open this URL manually in your browser: {url}")

    try:
        input_func("Press Enter here after the page has loaded successfully, then I will retry.")
    except EOFError:
        return False
    return True


def fetch_series_html_with_cloudflare_retry(
    session: requests.Session,
    series_url: str,
    source_adapter: SourceAdapter,
    browser_name: str = "auto",
    input_func: Callable[[str], str] = input,
    browser_opener: Callable[..., bool] = webbrowser.open,
    interactive_browser_fetcher: Callable[
        [requests.Session, str, SourceAdapter, Callable[[str], str]],
        str,
    ] | None = None,
) -> str:
    try:
        return fetch_html(session, series_url)
    except CloudflareProtectionError as original_error:
        print()
        print("Cloudflare or anti-bot protection appears to be blocking this request.")
        print(
            "The recommended path is a temporary private Chromium window controlled "
            "by MangaSnatcher."
        )
        print(
            "Complete the check there, then MangaSnatcher can read the page from "
            "that same browser context."
        )

        active_browser_fetcher = interactive_browser_fetcher or fetch_html_with_interactive_browser
        try:
            answer = input_func(
                "Open the temporary private Chromium window now? [Y/n]: "
            ).strip().lower()
        except EOFError:
            answer = "n"

        if answer in {"", "y", "yes", "j", "ja"}:
            try:
                return active_browser_fetcher(
                    session,
                    series_url,
                    source_adapter,
                    input_func,
                )
            except RuntimeError as exc:
                print(f"Temporary private Chromium fallback unavailable: {exc}")
            except CloudflareProtectionError as exc:
                print(f"Temporary private Chromium is still blocked: {exc}")

        if not prompt_for_cloudflare_browser_retry(
            series_url,
            input_func=input_func,
            browser_opener=browser_opener,
        ):
            raise original_error

        cookie_source = load_browser_cookies_into_session(
            session,
            browser_name,
            domain_name=source_adapter.cookie_domain or source_adapter.download_folder,
        )
        if cookie_source:
            print(f"Reloaded browser cookies from: {cookie_source}")
        elif browser_name == "none":
            print("Browser cookie loading is disabled; retrying without new cookies.")
        else:
            print("No matching browser cookies could be loaded automatically; retrying anyway.")

        print("Retrying series page after browser check...")
        try:
            return fetch_html(session, series_url)
        except CloudflareProtectionError:
            print()
            print(
                "The request is still blocked after reloading browser cookies. "
                "This usually means the cookies could not be copied from the browser profile."
            )
            try:
                answer = input_func(
                    "Open the temporary private Chromium window controlled by MangaSnatcher and retry from there? [y/N]: "
                ).strip().lower()
            except EOFError:
                raise

            if answer not in {"y", "yes", "j", "ja"}:
                raise

            return active_browser_fetcher(
                session,
                series_url,
                source_adapter,
                input_func,
            )


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


def ensure_graphical_browser_available() -> None:
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        raise RuntimeError(
            "A graphical browser session is required on Linux "
            "(missing DISPLAY or WAYLAND_DISPLAY)."
        )


class ControlledBrowserHtmlFetcher:
    def __init__(self) -> None:
        self.browser_process: subprocess.Popen[str] | None = None
        self.client: ChromeDevToolsClient | None = None
        self.user_data_dir: str | None = None

    @property
    def is_running(self) -> bool:
        return self.client is not None and self.browser_process is not None

    def __call__(
        self,
        session: requests.Session,
        url: str,
        source_adapter: SourceAdapter,
        input_func: Callable[[str], str] = input,
    ) -> str:
        first_open = not self.is_running
        if first_open:
            self._start(url)
            print()
            print("A temporary private Chromium window has been opened for this protected page.")
            print(
                "Complete the Cloudflare check there and make sure the manga page "
                "is visible before continuing."
            )
            input_func("Press Enter here after the page has loaded successfully.")
        else:
            print(f"Loading protected page through the temporary private Chromium window: {url}")
            self._navigate(url)

        html = self._read_ready_html(url)
        if is_cloudflare_challenge_html(html):
            print()
            print("The temporary Chromium window is still showing a protection page.")
            print("Complete the check there and make sure the requested page is visible.")
            input_func("Press Enter here after the page has loaded successfully.")
            html = self._read_ready_html(url)

        cookie_count = self._import_cookies(session, source_adapter)
        if cookie_count:
            print(f"Imported {cookie_count} cookies from the temporary Chromium session.")

        if not html or is_cloudflare_challenge_html(html):
            raise CloudflareProtectionError(url)

        return html

    def close(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None
        if self.browser_process is not None:
            close_browser_process(self.browser_process)
            self.browser_process = None
        if self.user_data_dir is not None:
            cleanup_browser_profile(self.user_data_dir)
            self.user_data_dir = None

    def _start(self, url: str) -> None:
        chromium_executable = find_chromium_executable()
        if not chromium_executable:
            raise RuntimeError(
                "Chromium is not installed, so the interactive Cloudflare fallback is unavailable."
            )

        ensure_graphical_browser_available()

        debug_port = reserve_local_port()
        self.user_data_dir = tempfile.mkdtemp(prefix="manga_snatcher_cloudflare_")

        self.browser_process = subprocess.Popen(
            [
                chromium_executable,
                f"--remote-debugging-port={debug_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.user_data_dir}",
                "--incognito",
                "--new-window",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        websocket_url = wait_for_debugger_target(
            debug_port,
            self.browser_process,
            timeout=BROWSER_STARTUP_TIMEOUT,
            target_url=url,
        )
        self.client = ChromeDevToolsClient(websocket_url, timeout=DEFAULT_TIMEOUT)
        self.client.__enter__()
        self.client.call("Page.enable")
        self.client.call("Runtime.enable")
        self.client.call("Network.enable")
        self.client.call("Page.bringToFront")

    def _navigate(self, url: str) -> None:
        if self.client is None:
            raise RuntimeError("Controlled Chromium is not running.")
        self.client.call("Page.navigate", {"url": url})

    def _read_ready_html(self, expected_url: str) -> str:
        if self.client is None:
            raise RuntimeError("Controlled Chromium is not running.")

        html = ""
        deadline = time.monotonic() + BROWSER_RENDER_TIMEOUT
        normalized_expected_url = expected_url.rstrip("/")
        while time.monotonic() < deadline:
            current_url = str(self.client.evaluate("window.location.href") or "")
            ready_state = self.client.evaluate("document.readyState")
            html_result = self.client.evaluate(
                "document.documentElement ? document.documentElement.outerHTML : ''"
            )
            if html_result:
                html = str(html_result)
                normalized_current_url = current_url.rstrip("/")
                url_matches = (
                    normalized_current_url == normalized_expected_url
                    or normalized_current_url.startswith(f"{normalized_expected_url}?")
                    or is_cloudflare_challenge_html(html)
                )
                if ready_state in {"interactive", "complete"} and url_matches:
                    return html
            time.sleep(BROWSER_POLL_INTERVAL)
        return html

    def _import_cookies(
        self,
        session: requests.Session,
        source_adapter: SourceAdapter,
    ) -> int:
        if self.client is None:
            raise RuntimeError("Controlled Chromium is not running.")

        cookie_result = self.client.call("Network.getAllCookies")
        cookies = cookie_result.get("cookies", [])
        return add_browser_cookies_to_session(
            session,
            cookies if isinstance(cookies, list) else [],
            domain_name=source_adapter.cookie_domain or source_adapter.download_folder,
        )


def fetch_html_with_interactive_browser(
    session: requests.Session,
    url: str,
    source_adapter: SourceAdapter,
    input_func: Callable[[str], str] = input,
) -> str:
    browser_fetcher = ControlledBrowserHtmlFetcher()
    try:
        return browser_fetcher(session, url, source_adapter, input_func)
    finally:
        browser_fetcher.close()


def fetch_chapter_images_with_browser(chapter_url: str) -> list[str]:
    chromium_executable = find_chromium_executable()
    if not chromium_executable:
        raise RuntimeError("Chromium is not installed, so browser fallback is unavailable.")

    ensure_graphical_browser_available()

    debug_port = reserve_local_port()
    user_data_dir = tempfile.mkdtemp(prefix="manga_snatcher_chromium_")

    browser_process = subprocess.Popen(
        [
            chromium_executable,
            f"--remote-debugging-port={debug_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={user_data_dir}",
            "--incognito",
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
    source_adapter: SourceAdapter = DEFAULT_SOURCE_ADAPTER,
) -> list[str]:
    image_urls = source_adapter.parse_chapter_images(chapter_html, chapter_url)
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

    if not source_adapter.has_reader_markup(chapter_html):
        detail = f" Browser fallback failed: {browser_error}" if browser_error else ""
        raise RuntimeError(
            "The fetched HTML does not contain manga reader markup. "
            "The site may be loading the chapter client-side or serving "
            f"different content to automated requests for {chapter_url}.{detail}"
        )

    detail = f" Browser fallback failed: {browser_error}" if browser_error else ""
    raise RuntimeError(f"No images were found in {chapter_url}.{detail}")


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


def find_skipped_chapter_numbers(chapters: Iterable[Chapter]) -> list[int]:
    chapter_numbers = sorted(
        {
            int(chapter.number)
            for chapter in chapters
            if chapter.number is not None
        }
    )
    if len(chapter_numbers) < 2:
        return []

    present_numbers = set(chapter_numbers)
    return [
        chapter_number
        for chapter_number in range(chapter_numbers[0], chapter_numbers[-1] + 1)
        if chapter_number not in present_numbers
    ]


def format_skipped_chapter_numbers(chapter_numbers: Iterable[int]) -> str:
    numbers = list(chapter_numbers)
    if not numbers:
        return "None"
    return ", ".join(str(chapter_number) for chapter_number in numbers)


def format_failed_chapters(chapters: Iterable[Chapter]) -> str:
    failed_chapters = list(chapters)
    if not failed_chapters:
        return "None"
    return ", ".join(chapter.display_name for chapter in failed_chapters)


def print_download_summary(
    chapters: Iterable[Chapter],
    result: DownloadRunResult,
) -> None:
    chapter_list = list(chapters)
    skipped_chapter_numbers = find_skipped_chapter_numbers(chapter_list)

    print("\n" + "=" * 50)
    print("DOWNLOAD SUMMARY")
    print("================")
    print()
    print(f"Total chapters found: {len(chapter_list)}")
    print(f"Downloaded successfully: {len(result.successful_chapters)}")
    print(f"Failed downloads: {format_failed_chapters(result.failed_chapters)}")
    print(
        "Skipped chapter numbers in sequence: "
        f"{format_skipped_chapter_numbers(skipped_chapter_numbers)}"
    )

    if skipped_chapter_numbers:
        print(
            "Notice: These skipped chapter numbers may simply not exist on the "
            "source website."
        )

    if result.failed_chapters:
        print("Some downloads failed after retrying.")
    elif not skipped_chapter_numbers:
        print("Download completed successfully.")
    else:
        print("Downloads completed; skipped chapter numbers are informational only.")

    print("=" * 50)


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


def build_chapter_pdf_path(
    output_dir: Path,
    source_adapter: SourceAdapter,
    series_title: str,
    chapter: Chapter,
) -> Path:
    chapter_folder = output_dir / source_adapter.download_folder / slugify(series_title)
    pdf_name = f"{slugify(series_title)}-{slugify(chapter.display_name)}.pdf"
    return chapter_folder / pdf_name


def download_chapter_to_pdf(
    session: requests.Session,
    chapter: Chapter,
    series_title: str,
    output_dir: Path,
    browser_image_fetcher: BrowserImageFetcher | None = fetch_chapter_images_with_browser,
    source_adapter: SourceAdapter = DEFAULT_SOURCE_ADAPTER,
    html_fetcher: HtmlFetcher = fetch_html,
) -> Path:
    print(f"\nDownloading {chapter.display_name} ...")
    chapter_html = html_fetcher(session, chapter.url)
    ensure_not_under_construction(chapter_html)
    image_urls = resolve_chapter_images(
        chapter_html,
        chapter.url,
        browser_image_fetcher=browser_image_fetcher,
        source_adapter=source_adapter,
    )

    pdf_path = build_chapter_pdf_path(output_dir, source_adapter, series_title, chapter)
    chapter_folder = pdf_path.parent
    chapter_folder.mkdir(parents=True, exist_ok=True)

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
    result: DownloadRunResult | None = None,
) -> DownloadRunResult:
    if result is None:
        result = DownloadRunResult()
    current_cooldown = DEFAULT_CHAPTER_COOLDOWN

    for index, chapter in enumerate(chapters):
        try:
            chapter_downloader(session, chapter, series_title, output_dir)
        except (requests.RequestException, RuntimeError, OSError) as exc:
            print(f"Error while downloading {chapter.display_name}: {exc}")
            retry_waiter(ERROR_RETRY_DELAY, chapter)
            print(f"Retrying {chapter.display_name}...")
            try:
                chapter_downloader(session, chapter, series_title, output_dir)
            except (requests.RequestException, RuntimeError, OSError):
                result.failed_chapters.append(chapter)
                raise
            result.successful_chapters.append(chapter)
            current_cooldown = RETRY_CHAPTER_COOLDOWN
        else:
            result.successful_chapters.append(chapter)

        if len(chapters) > 1 and index < len(chapters) - 1:
            chapter_waiter(current_cooldown)

    return result


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
        choices=(
            "auto",
            "brave",
            "chrome",
            "chromium",
            "edge",
            "firefox",
            "opera",
            "vivaldi",
            "none",
        ),
        default="auto",
        help=(
            "Load source website cookies from a local browser profile before fetching chapters "
            "(default: auto)."
        ),
    )
    args = parser.parse_args()

    controlled_browser_fetcher = ControlledBrowserHtmlFetcher()
    try:
        try:
            print_update_check_result(check_mangasnatcher_updates())
        except Exception as exc:
            print(f"Update check: unavailable ({exc})")

        input_url = prompt_for_url(args.url)
        source_adapter = get_source_adapter(input_url)
        series_url = source_adapter.normalize_series_url(input_url)
        session = build_session()
        cookie_source = load_browser_cookies_into_session(
            session,
            args.browser_cookies,
            domain_name=source_adapter.cookie_domain or source_adapter.download_folder,
        )
        if cookie_source:
            print(f"Loaded browser cookies from: {cookie_source}")

        print(f"Source website: {source_adapter.name} ({source_adapter.download_folder})")
        print(f"Fetching series page: {series_url}")
        series_html = fetch_series_html_with_cloudflare_retry(
            session,
            series_url,
            source_adapter,
            browser_name=args.browser_cookies,
            interactive_browser_fetcher=controlled_browser_fetcher,
        )
        ensure_not_under_construction(series_html)
        series_title = source_adapter.extract_title(series_html, series_url)
        chapters = source_adapter.parse_chapters(series_html, series_url)
        if not chapters:
            raise RuntimeError("No chapters were found on the series page.")

        selected_chapters = choose_chapters(chapters)
        output_dir = Path(args.output).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        browser_image_fetcher = None if args.no_browser_fallback else fetch_chapter_images_with_browser

        def chapter_html_fetcher(active_session, chapter_url):
            try:
                return fetch_html(active_session, chapter_url)
            except CloudflareProtectionError:
                if controlled_browser_fetcher.is_running:
                    print(
                        "Direct chapter request is still protected; "
                        "loading the chapter through the temporary private Chromium window."
                    )
                    return controlled_browser_fetcher(
                        active_session,
                        chapter_url,
                        source_adapter,
                    )

                print()
                print(
                    "This chapter page is protected by Cloudflare or anti-bot checks. "
                    "MangaSnatcher can load it through a temporary private Chromium window."
                )
                answer = input("Open the temporary private Chromium window now? [y/N]: ").strip().lower()
                if answer not in {"y", "yes", "j", "ja"}:
                    raise
                return controlled_browser_fetcher(
                    active_session,
                    chapter_url,
                    source_adapter,
                )

        def chapter_downloader(active_session, chapter, active_series_title, active_output_dir):
            return download_chapter_to_pdf(
                active_session,
                chapter,
                active_series_title,
                active_output_dir,
                browser_image_fetcher=browser_image_fetcher,
                source_adapter=source_adapter,
                html_fetcher=chapter_html_fetcher,
            )

        result = DownloadRunResult()
        download_error: requests.RequestException | RuntimeError | OSError | None = None
        try:
            download_selected_chapters(
                session,
                selected_chapters,
                series_title,
                output_dir,
                chapter_downloader=chapter_downloader,
                result=result,
            )
        except (requests.RequestException, RuntimeError, OSError) as exc:
            download_error = exc
            print(f"\nError: {exc}", file=sys.stderr)

        print_download_summary(chapters, result)
        return 1 if download_error else 0
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    finally:
        controlled_browser_fetcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
