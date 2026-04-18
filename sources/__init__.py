from .base import Chapter, SourceAdapter, WordPressMangaSource, normalize_image_sources
from .mangaread import MangaReadSource
from .manhuaus import ManhuaUSSource

SUPPORTED_SOURCES: tuple[SourceAdapter, ...] = (
    MangaReadSource(),
    ManhuaUSSource(),
)


def get_source_adapter(url: str) -> SourceAdapter:
    for source in SUPPORTED_SOURCES:
        if source.supports_url(url):
            return source
    supported_domains = ", ".join(
        domain
        for source in SUPPORTED_SOURCES
        for domain in source.domains
    )
    raise ValueError(
        "Unsupported source website. Supported domains: "
        f"{supported_domains}."
    )
