"""Offline tests for docs_library: a simulated documentation site, no real network.

The simulated transport records every request that actually reaches the network layer, so the tests
prove that unapproved URLs are refused BEFORE any connection is made."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docs_library import Allowlist, BlockedURL, Collection, DocsLibrary, DocsSettings, default_prefix  # noqa: E402

SITE = "https://docs.example.com"
START = f"{SITE}/guide/s3-files.html"
FILLER = "Additional explanation of the feature for readers. " * 8


def page(title, body):
    return f"<html><head><title>{title}</title></head><body><nav>Menu</nav><main>{body}</main></body></html>"


PAGES = {
    "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /guide/*-private.html\n"),
    "/guide/s3-files.html": (200, "text/html", page("S3 Files", f"""
        <h1>Working with S3 Files</h1><p>S3 Files gives file access to S3 buckets. {FILLER}</p>
        <a href="s3-files-quotas.html">Quotas</a> <a href="s3-files-quotas.html?lang=en#top">Quotas again</a>
        <a href="s3-files-private.html">Private</a> <a href="../other/page.html">Outside the prefix</a>
        <a href="https://evil.example.org/x">External</a> <a href="s3-files-moved.html">Moved</a>
        <a href="s3-files-guide.pdf">PDF</a>""")),
    "/guide/s3-files-quotas.html": (200, "text/html", page("Quotas", f"""
        <h1>Unsupported features, limits, and quotas</h1><p>Limits that apply to file systems. {FILLER}</p>
        <h2>File operations</h2><p>Rename and move operations are supported for files and directories. {FILLER}</p>""")),
    "/guide/s3-files-private.html": (200, "text/html", page("Private", "<p>Should never be fetched.</p>")),
    "/guide/s3-files-guide.pdf": (200, "application/pdf", "%PDF-1.7"),
    "/other/page.html": (200, "text/html", page("Other", "<p>Outside the approved prefix.</p>")),
}


class FakeSite(httpx.AsyncBaseTransport):
    def __init__(self, robots_status=200):
        self.requests: list[str] = []
        self.robots_status = robots_status

    async def handle_async_request(self, request):
        self.requests.append(str(request.url))
        path = request.url.path
        if request.url.host != "docs.example.com":
            return httpx.Response(200, text="evil")
        if path == "/guide/s3-files-moved.html":
            return httpx.Response(301, headers={"location": "https://evil.example.org/landing"})
        if path == "/robots.txt" and self.robots_status != 200:
            return httpx.Response(self.robots_status)
        status, ctype, body = PAGES.get(path, (404, "text/plain", "not found"))
        return httpx.Response(status, headers={"content-type": ctype}, text=body)


def library(tmp_path, site, **coll):
    settings = DocsSettings(cache_dir=str(tmp_path / "cache"), delay_s=0, passages_per_agent=4)
    return DocsLibrary(settings, [Collection(url=START, max_pages=coll.pop("max_pages", 25), **coll)], transport=site)


def collect(lib, refresh=False):
    return asyncio.run(lib.collect(refresh=refresh))


def manifest(lib):
    import json
    key = next(iter(lib.collections))
    return json.loads((lib.root / key / "manifest.json").read_text())


def test_recursive_collects_only_approved_pages(tmp_path):
    site = FakeSite()
    lib = library(tmp_path, site, recursive=True)
    collect(lib)
    m = manifest(lib)
    assert [p["url"] for p in m["pages"]] == [START, f"{SITE}/guide/s3-files-quotas.html"]
    reasons = {s["url"].rsplit("/", 1)[-1]: s["reason"] for s in m["skipped"]}
    assert reasons["s3-files-private.html"] == "disallowed by robots.txt"
    assert reasons["s3-files-moved.html"].startswith("redirected outside approved sources")
    assert reasons["s3-files-guide.pdf"].startswith("unsupported content type")
    # nothing outside the approved prefix (or robots.txt) ever reached the network
    assert all(u.startswith(f"{SITE}/guide/s3-files") or u == f"{SITE}/robots.txt" for u in site.requests)
    assert not any("evil" in u or "/other/" in u or "private" in u for u in site.requests)
    # snapshot: one Markdown file per page, with its source URL in the header
    files = sorted((lib.root / next(iter(lib.collections)) / "pages").glob("*.md"))
    assert len(files) == 2 and f'url: "{START}"' in files[0].read_text() + files[1].read_text()


def test_not_recursive_fetches_only_the_page(tmp_path):
    site = FakeSite()
    lib = library(tmp_path, site, recursive=False)
    collect(lib)
    assert [p["url"] for p in manifest(lib)["pages"]] == [START]
    assert site.requests == [f"{SITE}/robots.txt", START]


def test_page_limit(tmp_path):
    lib = library(tmp_path, FakeSite(), recursive=True, max_pages=1)
    collect(lib)
    m = manifest(lib)
    assert len(m["pages"]) == 1 and "page limit 1 reached" in m["skipped"][-1]["reason"]


def test_unreadable_robots_txt_skips_the_site(tmp_path):
    site = FakeSite(robots_status=503)
    lib = library(tmp_path, site, recursive=True)
    collect(lib)
    m = manifest(lib)
    assert m["pages"] == [] and m["skipped"][0]["reason"].startswith("robots.txt could not be read")
    assert site.requests == [f"{SITE}/robots.txt"]


def test_guard_refuses_unapproved_urls():
    allow = Allowlist([Collection(url=START, recursive=True)])
    assert allow.allows(f"{SITE}/guide/s3-files-quotas.html")
    assert allow.allows(f"{SITE}/robots.txt")                         # robots.txt of an approved site
    assert not allow.allows(f"{SITE}/guide/other.html")
    assert not allow.allows("https://docs.example.com.evil.org/guide/s3-files.html")  # look-alike host
    with pytest.raises(BlockedURL):
        asyncio.run(allow.hook(httpx.Request("GET", "https://evil.example.org/")))


def test_default_prefix():
    assert default_prefix(START) == f"{SITE}/guide/s3-files"
    assert default_prefix(f"{SITE}/guide/") == f"{SITE}/guide/"


def test_cache_is_reused_without_network(tmp_path):
    site = FakeSite()
    lib = library(tmp_path, site, recursive=True)
    collect(lib)
    site.requests.clear()
    collect(lib)
    assert site.requests == []                                       # second run: snapshot reused
    collect(lib, refresh=True)
    assert START in site.requests                                    # --refresh-docs downloads again


def test_retrieval_returns_relevant_passages(tmp_path):
    lib = library(tmp_path, FakeSite(), recursive=True)
    collect(lib)
    hits = lib.retrieve(list(lib.collections), ["rename", "move", "operations"])
    assert hits and hits[0]["url"].endswith("s3-files-quotas.html")
    assert "File operations" in hits[0]["heading"] and "Rename and move" in hits[0]["text"]
    assert all(h["source"] == "docs" for h in hits)


def test_deleted_page_is_excluded(tmp_path):
    lib = library(tmp_path, FakeSite(), recursive=True)
    collect(lib)
    folder = lib.root / next(iter(lib.collections)) / "pages"
    next(f for f in folder.glob("*.md") if "quotas" in f.name).unlink()   # reviewer removes a page
    collect(lib)                                                          # cached: index rebuilt from files
    assert not any(h["url"].endswith("quotas.html") for h in lib.retrieve(list(lib.collections), ["rename"]))
