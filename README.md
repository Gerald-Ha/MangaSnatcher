# MangaSnatcher

MangaSnatcher is a small Python command-line tool that downloads manga chapters from supported manga/manhua websites and saves them as PDF files.

It can:

- Accept a series URL or a chapter URL
- Detect the available chapters on the series page
- Let you choose one chapter, multiple chapters, or all chapters
- Download the chapter images
- Combine the images into PDF files
- Apply a built-in cooldown between chapter downloads
- Retry failed chapter downloads automatically
- Fall back to Chromium for chapters that hide reader images from direct requests
- Use source adapters so new websites can be added without changing the downloader core

Supported sources:

- `mangaread.org`
- `manhuaus.org`

## Credits

- Developer: Gerald-H
- GitHub: https://github.com/Gerald-Ha
- Project: MangaSnatcher

## Requirements

- Python 3.10 or newer
- Internet connection
- `chromium-browser`, `chromium`, or `google-chrome` if a site only exposes chapter images after real browser rendering
- `browser-cookie3` support is included via `requirements.txt` so MangaSnatcher can reuse login cookies from common local browser profiles

Python dependencies:

- `requests`
- `beautifulsoup4`
- `Pillow`

## Installation

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Usage

Run the downloader with a manga series URL or a chapter URL:

```bash
python MangaSnatcher.py "https://www.mangaread.org/manga/example-series/"
```

For ManhuaUS:

```bash
python MangaSnatcher.py "https://manhuaus.org/manga/the-reincarnated-assassin-is-a-genius-swordsman/"
```

You can also run it without an argument and paste the URL interactively:

```bash
python MangaSnatcher.py
```

After the series page is loaded, the program shows the detected chapters and asks what to download.

Valid selections:

- `all` to download every chapter
- `12` to download a single chapter
- `120-160` to download a continuous chapter range
- `1,2,5` to download multiple chapters

## Cooldown and Retry Behavior

- When downloading multiple chapters, MangaSnatcher waits `3` seconds between chapters.
- If a chapter download fails, the tool waits `60` seconds before retrying that chapter once.
- After a retry happens, the chapter-to-chapter cooldown is increased from `3` seconds to `8` seconds for the remaining downloads.
- This cooldown is meant to reduce the chance of being rate-limited or blocked by the site.

## Output

Generated PDFs are stored in the `downloads/` directory by default.

Example output structure:

```text
downloads/
  mangaread.org/
    example-series/
      example-series-chapter-1.pdf
      example-series-chapter-2.pdf
  manhuaus.org/
    the-reincarnated-assassin-is-a-genius-swordsman/
      the-reincarnated-assassin-is-a-genius-swordsman-chapter-1.pdf
```

You can set a custom output folder with:

```bash
python MangaSnatcher.py "https://www.mangaread.org/manga/example-series/" --output my_pdfs
```

If a chapter page hides its reader images from normal HTTP requests, MangaSnatcher automatically falls back to Chromium and reads the rendered DOM instead. On Linux, this requires a graphical session. You can disable that behavior with:

```bash
python MangaSnatcher.py "https://www.mangaread.org/manga/example-series/" --no-browser-fallback
```

If the site only serves pages or chapter images to your logged-in browser session, MangaSnatcher can also import cookies from your local browser profile:

```bash
python MangaSnatcher.py "https://www.mangaread.org/manga/example-series/" --browser-cookies brave
```

Supported values are `auto`, `brave`, `chrome`, `chromium`, `edge`, `firefox`, `opera`, `vivaldi`, and `none`.

Some sources, including `manhuaus.org`, may show a Cloudflare or anti-bot challenge to direct HTTP requests. When MangaSnatcher detects this while loading the series page, the recommended fallback is a temporary private Chromium window controlled by MangaSnatcher. Complete the challenge in that window and press Enter in the terminal. MangaSnatcher then reads the rendered page HTML and imports cookies from that same Chromium session. The temporary Chromium window stays available during the run, so protected chapter pages can be loaded through the same browser context.

If temporary Chromium is unavailable or declined, MangaSnatcher can still offer the older system-default-browser cookie retry.

This does not bypass Cloudflare automatically. It only gives you a manual browser step and a cleaner explanation instead of a raw `403 Forbidden` error.

## Startup Update Check

MangaSnatcher can check your Update Center at startup and print whether the
installed version is current or whether an update is available.

Register the project in the Update Center with the project ID `mangasnatcher`,
then start the downloader:

```bash
python MangaSnatcher.py "https://www.mangaread.org/manga/example-series/"
```

Optional environment variables:

- `MANGASNATCHER_UPDATE_API_KEY` overrides the built-in MangaSnatcher update API key
- `UPDATE_SERVER_URL` defaults to `https://update.gerald-hasani.com`
- `UPDATE_PROJECT_ID` defaults to `mangasnatcher`
- `UPDATE_CHANNEL` defaults to `stable`
- `APP_VERSION` defaults to `4.0.0`
- `BUILD_NUMBER`, `GIT_COMMIT`, and `DOCKER_IMAGE_DIGEST` are sent when present

If the update server is unreachable, MangaSnatcher prints a short notice and
continues normally.

## How It Works

1. MangaSnatcher loads the provided series or chapter URL.
2. It selects the matching source adapter for the website.
3. It normalizes the URL to the series page.
4. It scans the page for chapter links using the source adapter.
5. It loads the selected chapters and collects the page images.
6. It converts the images into PDF files.


## Disclaimer

Use this project only for content you are legally allowed to access and download. Respect the website's terms of service and the rights of the content owners.
