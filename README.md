# MangaSnatcher

MangaSnatcher is a small Python command-line tool that downloads manga chapters from `https://www.mangaread.org/` style pages and saves them as PDF files.

It can:

- Accept a series URL or a chapter URL
- Detect the available chapters on the series page
- Let you choose one chapter, multiple chapters, or all chapters
- Download the chapter images
- Combine the images into PDF files
- Apply a built-in cooldown between chapter downloads
- Retry failed chapter downloads automatically

## Credits

- Developer: Gerald-H
- GitHub: https://github.com/Gerald-Ha
- Project: MangaSnatcher

## Requirements

- Python 3.10 or newer
- Internet connection

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

You can also run it without an argument and paste the URL interactively:

```bash
python MangaSnatcher.py
```

After the series page is loaded, the program shows the detected chapters and asks what to download.

Valid selections:

- `all` to download every chapter
- `12` to download a single chapter
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
  example-series/
    example-series-chapter-1.pdf
    example-series-chapter-2.pdf
```

You can set a custom output folder with:

```bash
python MangaSnatcher.py "https://www.mangaread.org/manga/example-series/" --output my_pdfs
```

## How It Works

1. MangaSnatcher loads the provided series or chapter URL.
2. It normalizes the URL to the series page.
3. It scans the page for chapter links.
4. It loads the selected chapters and collects the page images.
5. It converts the images into PDF files.


## Disclaimer

Use this project only for content you are legally allowed to access and download. Respect the website's terms of service and the rights of the content owners.
