"""
docs_library.py - collects official documentation from approved URLs only, keeps a dated local
snapshot, and gives each agent the passages relevant to its section.

Network access goes through ONE httpx client. A request hook runs before every request, redirects
included, and refuses any URL that is not approved. Approved means: a configured URL, a page under
its prefix when recursive: true, or /robots.txt on the same site. Nothing else can be fetched.

  collect()   download (or reuse the cached snapshot), write Markdown pages + manifest.json
  retrieve()  local keyword search (SQLite FTS5, BM25 ranking) over the snapshot; no network
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import posixpath
import re
import sqlite3
import sys
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel


def log(msg: str) -> None:
    print(f"[docs] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------- settings
class DocsSettings(BaseModel):
    """config.yaml -> docs: (environment settings, shared by all agents)."""
    cache_dir: str = "docs_cache"
    user_agent: str = "docgen-docs-collector/1.0"
    delay_s: float = 1.0                  # minimum pause between requests to the same site
    timeout_s: float = 30
    max_pages: int = 25                   # default page limit per URL when recursive: true
    passages_per_agent: int = 8
    max_passages_per_page: int = 3
    passage_chars: int = 1500


class Collection(BaseModel):
    """One configured URL with its recursion settings."""
    url: str
    recursive: bool = False
    under: list[str] = []                 # recursive: explicit prefixes; default = derived from url
    max_pages: int = 25

    @property
    def start(self) -> str:
        return normalize(self.url)

    @property
    def prefixes(self) -> list[str]:
        if not self.recursive:
            return [self.start]           # the page itself (and e.g. its trailing-slash redirect)
        return [normalize(u) for u in self.under] or [default_prefix(self.url)]

    @property
    def key(self) -> str:
        raw = json.dumps([self.start, self.recursive, sorted(self.prefixes), self.max_pages])
        p = urlsplit(self.start)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{p.netloc}{p.path}").strip("-")[-60:]
        return f"{slug}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"

    def contains(self, url: str) -> bool:
        return normalize(url).startswith(tuple(self.prefixes))


# ----------------------------------------------------------------------------- URLs
def normalize(url: str) -> str:
    """Absolute URL without #fragment or ?query, lower-case host, resolved ./ and ../ segments."""
    u, _ = urldefrag(url.strip())
    p = urlsplit(u)
    path = p.path or "/"
    if "/." in path:
        trailing = path.endswith("/")
        path = posixpath.normpath(path) + ("/" if trailing else "")
        path = path.replace("//", "/")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def default_prefix(url: str) -> str:
    """https://site/guide/s3-files.html -> https://site/guide/s3-files (matches s3-files-quotas.html)."""
    p = urlsplit(normalize(url))
    path = p.path
    if not path.endswith("/"):
        head, last = path.rsplit("/", 1)
        path = f"{head}/{re.sub(r'(?i)[.](html?|md|php|aspx?)$', '', last)}"
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def site_of(url: str) -> str:
    p = urlsplit(normalize(url))
    return f"{p.scheme}://{p.netloc}"


class BlockedURL(PermissionError):
    def __init__(self, url: str):
        super().__init__(f"blocked, not an approved source: {url}")
        self.url = url


class Allowlist:
    """The single gate for all network access."""

    def __init__(self, collections: list[Collection]):
        self.prefixes = tuple(p for c in collections for p in c.prefixes)
        self.robots = {f"{site_of(c.url)}/robots.txt" for c in collections}

    def allows(self, url: str) -> bool:
        n = normalize(url)
        return n in self.robots or n.startswith(self.prefixes)

    async def hook(self, request: httpx.Request) -> None:  # httpx calls this before EVERY request
        if not self.allows(str(request.url)):
            raise BlockedURL(str(request.url))


# ----------------------------------------------------------------------------- pages
class _PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.title = ""
        self.text: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def html_page(html: str, url: str) -> tuple[str, str, list[str]]:
    """Returns (title, markdown, links) for an HTML page."""
    p = _PageParser()
    p.feed(html)
    md = None
    try:
        import trafilatura  # Apache-2.0: main content as Markdown, without menus and footers
        md = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                                 include_links=False, include_comments=False)
    except ImportError:
        pass
    return re.sub(r"\s+", " ", p.title).strip(), md or "\n\n".join(p.text), p.links


MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")


def markdown_page(text: str) -> tuple[str, str, list[str]]:
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "")
    return title, text, MD_LINK.findall(text)


# ----------------------------------------------------------------------------- robots.txt
class Robots:
    """robots.txt per site, downloaded through the guarded client, read with Protego."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self.client, self.ua, self.rules = client, user_agent, {}

    async def get(self, site: str):
        if site not in self.rules:
            from protego import Protego  # BSD-3: understands * and $ wildcards, unlike urllib.robotparser
            try:
                r = await self.client.get(f"{site}/robots.txt")
                if r.status_code in (404, 410):
                    self.rules[site] = Protego.parse("")      # no robots.txt: no restrictions
                elif r.status_code >= 400:
                    self.rules[site] = None                   # unreadable: skip the site (safe default)
                else:
                    self.rules[site] = Protego.parse(r.text)
            except (httpx.HTTPError, BlockedURL) as e:
                log(f"robots.txt unavailable for {site}: {e}")
                self.rules[site] = None
        return self.rules[site]

    async def check(self, url: str) -> Optional[str]:
        """None if allowed, else the reason for skipping."""
        rules = await self.get(site_of(url))
        if rules is None:
            return "robots.txt could not be read: site skipped"
        if not rules.can_fetch(url, self.ua):
            return "disallowed by robots.txt"
        return None

    def delay(self, url: str) -> float:
        rules = self.rules.get(site_of(url))
        return float(rules.crawl_delay(self.ua) or 0) if rules else 0.0


# ----------------------------------------------------------------------------- passages
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _cut(text: str, max_chars: int) -> list[str]:
    """Group paragraphs into chunks of at most max_chars; very long paragraphs are split."""
    chunks, buf = [], ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", text)):
        for piece in (para[i:i + max_chars] for i in range(0, len(para), max_chars)):
            if buf and len(buf) + len(piece) + 2 > max_chars:
                chunks.append(buf)
                buf = ""
            buf = f"{buf}\n\n{piece}" if buf else piece
    return chunks + ([buf] if buf else [])


def split_passages(markdown: str, title: str, max_chars: int) -> list[tuple[str, str]]:
    """[(heading path, text)]: split at Markdown headings, then by size."""
    out: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            names = [h for _, h in stack]
            path = " › ".join(([title] if title and (not names or names[0] != title) else []) + names)
            out.extend((path or title, chunk) for chunk in _cut(text, max_chars))

    for line in markdown.splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
        else:
            buf.append(line)
    flush()
    return out


STOPWORDS = set("""a about above after again all also an and any are as at be because been before being both but by can
could did do does doing for from further had has have having how if in into is it its itself just more most no nor not
of off on once only or other our out over own same should so some such than that the their them then there these they
this those through to too under until up very was we were what when where which while who whom why will with would you
your describe explain list state identify section component components within others other evaluated enterprise
organization corporate based keep only should must""".split())


def query_terms(*texts: str, limit: int = 40) -> list[str]:
    terms: list[str] = []
    for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", " ".join(texts).lower()):
        if w not in STOPWORDS and w not in terms:
            terms.append(w)
    return terms[:limit]


# ----------------------------------------------------------------------------- the library
class DocsLibrary:
    def __init__(self, settings: DocsSettings, collections: list[Collection],
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.s = settings
        self.collections = {c.key: c for c in collections}
        self.root = Path(settings.cache_dir)
        self.transport = transport              # tests only; production uses the default transport (proxy-aware)
        keys = "".join(sorted(self.collections))
        self.index_path = self.root / f"index-{hashlib.sha256(keys.encode()).hexdigest()[:10]}.sqlite"
        self.summary: list[dict] = []

    # ---- collection
    def _client(self, allow: Allowlist) -> httpx.AsyncClient:
        kw = {"transport": self.transport} if self.transport else {}
        return httpx.AsyncClient(follow_redirects=True, timeout=self.s.timeout_s,
                                 headers={"User-Agent": self.s.user_agent},
                                 event_hooks={"request": [allow.hook]}, **kw)

    def _cached(self, c: Collection) -> Optional[dict]:
        f = self.root / c.key / "manifest.json"
        if not f.exists():
            return None
        m = json.loads(f.read_text(encoding="utf-8"))
        same = (m.get("start_url"), m.get("recursive"), sorted(m.get("prefixes", [])), m.get("max_pages")) == \
               (c.start, c.recursive, sorted(c.prefixes), c.max_pages)
        return m if same else None

    async def collect(self, refresh: bool = False) -> list[dict]:
        """Download what is not cached (or everything with refresh), then rebuild the local index."""
        allow = Allowlist(list(self.collections.values()))
        self.summary = []
        async with self._client(allow) as client:
            robots = Robots(client, self.s.user_agent)
            for c in self.collections.values():
                manifest = None if refresh else self._cached(c)
                if manifest:
                    log(f"cached   {c.url}: {len(manifest['pages'])} page(s) from {manifest['collected_at'][:10]}")
                else:
                    manifest = await self._crawl(c, client, robots)
                    log(f"fetched  {c.url}: {len(manifest['pages'])} page(s), {len(manifest['skipped'])} skipped")
                self.summary.append({"url": c.url, "recursive": c.recursive, "prefixes": c.prefixes,
                                     "pages": len(manifest["pages"]), "skipped": len(manifest["skipped"]),
                                     "collected_at": manifest["collected_at"], "folder": str(self.root / c.key)})
        self._build_index()
        return self.summary

    async def _crawl(self, c: Collection, client: httpx.AsyncClient, robots: Robots) -> dict:
        queue, seen = deque([c.start]), {c.start}
        pages, skipped, last = [], [], 0.0
        while queue and len(pages) < c.max_pages:
            url = queue.popleft()
            if reason := await robots.check(url):
                skipped.append({"url": url, "reason": reason})
                continue
            await asyncio.sleep(max(0.0, max(self.s.delay_s, robots.delay(url)) - (time.monotonic() - last)))
            last = time.monotonic()
            try:
                r = await client.get(url)
            except BlockedURL as e:
                skipped.append({"url": url, "reason": f"redirected outside approved sources: {e.url}"})
                continue
            except httpx.HTTPError as e:
                skipped.append({"url": url, "reason": f"download failed: {type(e).__name__}"})
                continue
            final = normalize(str(r.url))
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200:
                skipped.append({"url": url, "reason": f"HTTP {r.status_code}"})
                continue
            if not c.contains(final):
                skipped.append({"url": url, "reason": f"redirected outside this source: {final}"})
                continue
            if "html" in ctype:
                title, md, links = html_page(r.text, final)
            elif "markdown" in ctype or "text/plain" in ctype:
                title, md, links = markdown_page(r.text)
            else:
                skipped.append({"url": final, "reason": f"unsupported content type: {ctype.split(';')[0]}"})
                continue
            if not md.strip():
                skipped.append({"url": final, "reason": "no readable text"})
                continue
            seen.add(final)
            pages.append({"url": final, "title": title or final, "markdown": md,
                          "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
            if c.recursive:
                for href in links:
                    link = normalize(urljoin(final, href))
                    if link.startswith(("http://", "https://")) and link not in seen and c.contains(link):
                        seen.add(link)
                        queue.append(link)
        if queue:
            skipped.append({"url": c.url, "reason": f"page limit {c.max_pages} reached: {len(queue)} link(s) not followed"})
        return self._save(c, pages, skipped)

    def _save(self, c: Collection, pages: list[dict], skipped: list[dict]) -> dict:
        folder = self.root / c.key
        pages_dir = folder / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        for old in pages_dir.glob("*.md"):
            old.unlink()
        entries, names = [], set()
        for p in pages:
            stem = re.sub(r"[^A-Za-z0-9]+", "-", urlsplit(p["url"]).path).strip("-")[-80:] or "index"
            name, i = f"{stem}.md", 2
            while name in names:
                name, i = f"{stem}-{i}.md", i + 1
            names.add(name)
            sha = hashlib.sha256(p["markdown"].encode()).hexdigest()
            header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in
                               (("url", p["url"]), ("title", p["title"]), ("fetched", p["fetched_at"]), ("sha256", sha)))
            (pages_dir / name).write_text(f"---\n{header}\n---\n{p['markdown']}\n", encoding="utf-8")
            entries.append({"url": p["url"], "title": p["title"], "file": f"pages/{name}",
                            "fetched_at": p["fetched_at"], "sha256": sha, "chars": len(p["markdown"])})
        manifest = {"start_url": c.start, "recursive": c.recursive, "prefixes": c.prefixes, "max_pages": c.max_pages,
                    "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "user_agent": self.s.user_agent, "pages": entries, "skipped": skipped}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return manifest

    # ---- local index
    def _build_index(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.unlink(missing_ok=True)
        db = sqlite3.connect(self.index_path)
        try:
            db.execute("CREATE VIRTUAL TABLE passages USING fts5(collection UNINDEXED, url UNINDEXED, "
                       "title UNINDEXED, heading, text, tokenize='porter unicode61')")
        except sqlite3.OperationalError as e:
            raise RuntimeError("this Python's SQLite has no FTS5 full-text search; use a python.org, "
                               "Homebrew or conda Python") from e
        for key in self.collections:
            for f in sorted((self.root / key / "pages").glob("*.md")):  # deleted page files are excluded
                head, _, body = f.read_text(encoding="utf-8").partition("\n---\n")
                meta = {k: json.loads(v) for k, v in (ln.split(": ", 1) for ln in head.splitlines()[1:] if ": " in ln)}
                rows = [(key, meta.get("url", ""), meta.get("title", ""), h, t)
                        for h, t in split_passages(body, meta.get("title", ""), self.s.passage_chars)]
                db.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?)", rows)
        db.commit()
        db.close()

    def retrieve(self, keys: list[str], terms: list[str]) -> list[dict]:
        """Best passages for these search terms, from these collections only. Local, no network."""
        if not terms or not keys or not self.index_path.exists():
            return []
        db = sqlite3.connect(self.index_path)
        q = " OR ".join(f'"{t}"' for t in terms)          # quoted terms: no FTS query syntax surprises
        marks = ",".join("?" * len(keys))
        rows = db.execute(f"SELECT url, title, heading, text FROM passages WHERE passages MATCH ? "
                          f"AND collection IN ({marks}) ORDER BY bm25(passages) LIMIT 200", (q, *keys)).fetchall()
        db.close()
        out, per_page, seen = [], {}, set()
        for url, title, heading, text in rows:
            fp = hashlib.sha256(text.encode()).hexdigest()
            if fp in seen or per_page.get(url, 0) >= self.s.max_passages_per_page:
                continue
            seen.add(fp)
            per_page[url] = per_page.get(url, 0) + 1
            out.append({"source": "docs", "locator": f"{url} › {heading}", "url": url, "heading": heading,
                        "text": text, "sha256": fp[:16]})
            if len(out) >= self.s.passages_per_agent:
                break
        return out
