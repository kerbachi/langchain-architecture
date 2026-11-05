#!/usr/bin/env python3
"""
docgen.py - outline-driven multi-agent document generator built on LangGraph.

Three inputs, kept separate so the same engine serves any similar request:
  --template  reusable    : document title, OUTLINE (exact section titles in order), each section's
                            directives and sources, consistency checks
  --request   per request : global context (Markdown) + YAML front-matter variables
  --config    per env     : LLM endpoints (any OpenAI-compatible /v1 API), output folder

The script prints every heading itself, from the template. The agents only write section bodies.

  python docgen.py -t templates/component_standard.yaml -r requests/s3_files_aegon_spain.md
  python docgen.py -t ... -r ... --dry-run   # no LLM / network calls: tests the orchestration
  python docgen.py -t ... -r ... --graph     # print the LangGraph as a Mermaid diagram and exit
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import operator
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict
from urllib.parse import urlparse

import httpx
import yaml
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, ValidationError, field_validator

from docs_library import Collection, DocsLibrary, DocsSettings, query_terms

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}", file=sys.stderr)


# =============================================================================
# 1. INPUTS: template (reusable), request (global context + variables), config (environment)
# =============================================================================
class SourceSpec(BaseModel):
    type: Literal["docs", "file", "url", "web", "m365"]
    paths: list[str] = []                  # file
    urls: list[str] = []                   # docs, url
    recursive: bool = False                # docs: true = also collect linked pages under the same prefix
    under: list[str] = []                  # docs: explicit prefixes for recursive (default: derived from url)
    max_pages: Optional[int] = None        # docs: page limit per url when recursive (default: config docs.max_pages)
    query: Optional[str] = None            # web, m365 (m365: one sentence, <= 1,500 chars)
    allow_domains: list[str] = []          # web
    max_results: int = 5                   # web, m365 (m365 max 25)
    data_source: str = "sharePoint"        # m365: sharePoint | oneDriveBusiness | externalItem
    filter_expression: Optional[str] = None  # m365 KQL, e.g. path:"https://contoso.sharepoint.com/sites/x/"


class SectionSpec(BaseModel):
    id: str                                # machine id: node names, depends_on, file names
    title: str                             # printed in the document exactly as written
    directives: str | list[str]            # one item per line (or a YAML list); each item is checked
    sources: list[SourceSpec] = []         # extra sources of truth on top of the request context
    use_request_context: bool = True       # give the global context [R1] to this agent
    depends_on: list[str] = []             # empty = starts immediately, in parallel with the others
    guidelines: str = ""                   # section-specific writing guidance
    table_columns: list[str] = []          # set = the section is a table with exactly these columns
    retrieval_keywords: str = ""           # docs: extra search words for picking relevant passages

    @property
    def directive_list(self) -> list[tuple[str, str]]:
        items = self.directives if isinstance(self.directives, list) else self.directives.splitlines()
        items = [str(x).strip() for x in items if str(x).strip()]
        return [(f"{self.id}:d{i}", text) for i, text in enumerate(items, 1)]


class Template(BaseModel):
    document_title: str
    numbering: bool = True
    purpose: str
    required_vars: list[str] = []
    defaults: dict[str, Any] = {}
    writer_guidelines: str = ""
    key_facts: list[str] = []
    source_precedence: list[str] = ["request", "file", "docs", "m365", "url", "web"]
    max_revision_rounds: int = 1
    sections: list[SectionSpec]            # the OUTLINE: list order = document order

    def heading(self, spec: SectionSpec) -> str:
        n = [s.id for s in self.sections].index(spec.id) + 1
        return f"{n}. {spec.title}" if self.numbering else spec.title

    def section(self, sid: str) -> SectionSpec:
        return next(s for s in self.sections if s.id == sid)

    def outline(self) -> str:
        return "\n".join(self.heading(s) for s in self.sections)


class LLMRole(BaseModel):
    base_url: str
    model: str
    api_key_env: Optional[str] = None
    temperature: float = 0.2
    max_concurrency: int = 4
    # false: plain text + tolerant JSON parsing (works everywhere)
    # true: response_format=json_object | "schema": response_format=json_schema (LM Studio, vLLM, OpenAI)
    json_mode: bool | Literal["schema"] = False
    max_tokens: Optional[int] = None       # cap output length (avoids truncated JSON on small defaults)
    extra_body: dict[str, Any] = {}        # server-specific options, e.g. vLLM chat_template_kwargs
    timeout_s: float = 300


class Config(BaseModel):
    llm: dict[str, LLMRole] = {}
    output_dir: str = "out"
    max_evidence_chars: int = 24000
    docs: DocsSettings = DocsSettings()


VAR = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")


def render(obj: Any, variables: dict) -> Any:
    """Replace {{var}} everywhere. A string that is exactly "{{var}}" takes the variable's raw value
    (so lists such as reference_urls stay lists); otherwise values are inserted as text."""
    if isinstance(obj, str):
        whole = VAR.fullmatch(obj.strip())
        if whole and whole.group(1) in variables:
            return variables[whole.group(1)]

        def sub(mo):
            key = mo.group(1)
            if key not in variables:
                raise KeyError(key)
            val = variables[key]
            return ", ".join(map(str, val)) if isinstance(val, list) else str(val)
        return VAR.sub(sub, obj)
    if isinstance(obj, list):
        out: list = []
        for item in obj:
            r = render(item, variables)
            if isinstance(item, str) and isinstance(r, list) and VAR.fullmatch(item.strip()):
                out.extend(r)  # ["{{reference_urls}}"] -> flat list of urls
            else:
                out.append(r)
        return out
    if isinstance(obj, dict):
        return {k: render(v, variables) for k, v in obj.items()}
    return obj


def load_request(path: str) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.S)
    if m:
        return (yaml.safe_load(m.group(1)) or {}), m.group(2).strip()
    return {}, text.strip()


def load_inputs(template_path: str, request_path: str, config_path: Optional[str]):
    raw = yaml.safe_load(Path(template_path).read_text(encoding="utf-8"))
    req_vars, context = load_request(request_path)
    variables = {"today": dt.date.today().isoformat(), **raw.get("defaults", {}), **req_vars}
    missing = [v for v in raw.get("required_vars", []) if v not in variables]
    if missing:
        raise SystemExit(f"request front matter is missing required variable(s): {missing}")
    try:
        tpl = Template.model_validate(render(raw, variables))
    except KeyError as e:
        raise SystemExit(f"template uses {{{{{e.args[0]}}}}} but neither the request front matter "
                         f"nor the template defaults define it")
    cfg = Config()
    if config_path and Path(config_path).exists():
        cfg = Config.model_validate(yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {})
    return tpl, variables, context, cfg


def execution_order(tpl: Template) -> list[list[str]]:
    """Validates dependencies and returns the execution waves (agents in a wave run in parallel)."""
    ids = [s.id for s in tpl.sections]
    if len(ids) != len(set(ids)):
        raise SystemExit("section ids must be unique")
    deps = {s.id: set(s.depends_on) for s in tpl.sections}
    for sid, d in deps.items():
        if unknown := d - set(ids):
            raise SystemExit(f"section '{sid}' depends on unknown section(s) {sorted(unknown)}")
    waves: list[list[str]] = []
    while deps:
        ready = [k for k, v in deps.items() if not v]
        if not ready:
            raise SystemExit(f"dependency cycle among sections {sorted(deps)}")
        waves.append(ready)
        for k in ready:
            deps.pop(k)
        for v in deps.values():
            v.difference_update(ready)
    return waves


# =============================================================================
# 2. WHAT THE LLMs MUST RETURN (validated with Pydantic)
# =============================================================================
class Claim(BaseModel):
    statement: str
    evidence: list[str] = []


class SectionDraft(BaseModel):
    body: str
    claims: list[Claim] = []
    key_facts: dict[str, str] = {}
    assumptions: list[str] = []
    open_questions: list[str] = []
    directives_covered: list[str] = []
    rows: list[list[str]] = []             # table sections only: one list of cell values per row

    @field_validator("rows", mode="before")
    @classmethod
    def _rows(cls, v):
        out = []
        for r in v or []:
            r = list(r.values()) if isinstance(r, dict) else [r] if isinstance(r, str) else r
            out.append([str(c) for c in r])
        return out

    @field_validator("key_facts", mode="before")
    @classmethod
    def _flatten(cls, v):
        return {k: ", ".join(map(str, x)) if isinstance(x, list) else str(x) for k, x in (v or {}).items()}


class Conflict(BaseModel):
    sections: list[str]
    description: str
    severity: Literal["high", "medium", "low"] = "medium"
    recommended_resolution: str = ""


class Revision(BaseModel):
    section_id: str
    instructions: str


class Review(BaseModel):
    verdict: Literal["pass", "revise"]
    conflicts: list[Conflict] = []
    gaps: list[str] = []
    overlaps: list[str] = []
    revisions: list[Revision] = []
    summary: str = ""


# =============================================================================
# 3. LLM CLIENT (OpenAI-compatible: Ollama, vLLM, llama.cpp, LM Studio, OpenRouter, OpenAI ...)
# =============================================================================
def extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start:end + 1], strict=False)  # tolerate raw newlines inside strings


def inline_schema(model_cls: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $refs inlined (grammar-based engines handle flat schemas best)."""
    schema = model_cls.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items()}
        return [resolve(x) for x in node] if isinstance(node, list) else node
    return resolve(schema)


class LLM:
    def __init__(self, role: LLMRole, name: str):
        from openai import AsyncOpenAI
        key = os.environ.get(role.api_key_env, "") if role.api_key_env else ""
        self.client = AsyncOpenAI(base_url=role.base_url, api_key=key or "not-needed", timeout=role.timeout_s)
        self.role, self.name = role, name
        self.sem = asyncio.Semaphore(role.max_concurrency)

    async def json(self, system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra: dict[str, Any] = {}
        if self.role.json_mode == "schema":
            extra["response_format"] = {"type": "json_schema", "json_schema": {
                "name": model_cls.__name__, "schema": inline_schema(model_cls)}}
        elif self.role.json_mode is True:
            extra["response_format"] = {"type": "json_object"}
        if self.role.max_tokens:
            extra["max_tokens"] = self.role.max_tokens
        if self.role.extra_body:
            extra["extra_body"] = self.role.extra_body
        last: Exception | None = None
        for _ in range(3):
            async with self.sem:
                resp = await self.client.chat.completions.create(
                    model=self.role.model, messages=messages, temperature=self.role.temperature, **extra)
            text = resp.choices[0].message.content or ""
            try:
                return model_cls.model_validate(extract_json(text))
            except (ValueError, ValidationError) as e:
                last = e
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"Invalid JSON for the required schema: {str(e)[:400]}. "
                                                         "Reply with the corrected JSON object only."}]
        raise RuntimeError(f"{self.name}: no valid JSON after 3 attempts: {last}")

    async def preflight(self) -> None:
        """Warn early if the endpoint is unreachable or does not serve the configured model id."""
        try:
            ids = [m.id async for m in self.client.models.list()]
        except Exception as e:
            log(f"WARNING {self.name}: cannot list models at {self.role.base_url} ({err_text(e)})")
            return
        if ids and self.role.model not in ids:
            log(f"WARNING {self.name}: model '{self.role.model}' not served at {self.role.base_url}; "
                f"available: {ids[:10]}{' ...' if len(ids) > 10 else ''}")
        else:
            log(f"{self.name}: {self.role.model} @ {self.role.base_url} OK")


# =============================================================================
# 4. SOURCES OF TRUTH
# =============================================================================
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return "\n".join(p.parts)


def _ev(source: str, locator: str, text: str, **extra) -> dict:
    return {"source": source, "locator": locator, "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()[:16], **extra}


TEXT_SUFFIXES = {".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv", ".adoc"}


def _parse_rich_file(path: Path) -> str:
    try:
        from docling.document_converter import DocumentConverter  # optional: PDF / DOCX / PPTX
    except ImportError as e:
        raise RuntimeError(f"{path}: pip install docling to parse {path.suffix} files") from e
    return DocumentConverter().convert(str(path)).document.export_to_markdown()


async def load_file(src: SourceSpec, http) -> list[dict]:
    out = []
    for p in src.paths:
        path = Path(p)
        text = path.read_text(encoding="utf-8", errors="replace") if path.suffix.lower() in TEXT_SUFFIXES \
            else await asyncio.to_thread(_parse_rich_file, path)
        out.append(_ev("file", str(path), text))
    return out


async def load_url(src: SourceSpec, http) -> list[dict]:
    out = []
    for u in src.urls:
        r = await http.get(u, follow_redirects=True)
        r.raise_for_status()
        out.append(_ev("url", u, html_to_text(r.text) if "html" in r.headers.get("content-type", "") else r.text))
    return out


def _domain_ok(url: str, allow: list[str]) -> bool:
    host = urlparse(url).hostname or ""
    return not allow or any(host == d or host.endswith("." + d) for d in allow)


async def load_web(src: SourceSpec, http) -> list[dict]:
    base = os.environ.get("SEARXNG_URL")
    if not base:
        raise RuntimeError("web source needs SEARXNG_URL (self-hosted SearXNG with the json format enabled)")
    r = await http.get(f"{base.rstrip('/')}/search", params={"q": src.query, "format": "json"})
    r.raise_for_status()
    hits = [h for h in r.json().get("results", []) if _domain_ok(h.get("url", ""), src.allow_domains)]
    out = []
    for h in hits[: src.max_results]:
        try:
            page = await http.get(h["url"], follow_redirects=True)
            page.raise_for_status()
            text = html_to_text(page.text)
        except httpx.HTTPError:
            text = h.get("content", "")
        out.append(_ev("web", h["url"], text))
    return out


GRAPH_TOKEN: dict[str, Optional[str]] = {"token": None}


def acquire_graph_token() -> str:
    """Delegated (user) token: the M365 Copilot Retrieval API has no app-only permission."""
    if tok := os.environ.get("GRAPH_TOKEN"):
        return tok
    from azure.identity import DeviceCodeCredential  # pip install azure-identity
    cred = DeviceCodeCredential(client_id=os.environ["M365_CLIENT_ID"], tenant_id=os.environ["M365_TENANT_ID"])
    return cred.get_token("https://graph.microsoft.com/Files.Read.All",
                          "https://graph.microsoft.com/Sites.Read.All").token


async def load_m365(src: SourceSpec, http) -> list[dict]:
    body: dict = {"queryString": src.query, "dataSource": src.data_source,
                  "maximumNumberOfResults": min(src.max_results, 25), "resourceMetadata": ["title"]}
    if src.filter_expression:
        body["filterExpression"] = src.filter_expression
    r = await http.post("https://graph.microsoft.com/v1.0/copilot/retrieval", json=body,
                        headers={"Authorization": f"Bearer {GRAPH_TOKEN['token']}"})
    r.raise_for_status()
    return [_ev("m365", h.get("webUrl", ""), "\n".join(x.get("text", "") for x in h.get("extracts", [])),
                sensitivity=(h.get("sensitivityLabel") or {}).get("displayName"))
            for h in r.json().get("retrievalHits", [])]


LOADERS = {"file": load_file, "url": load_url, "web": load_web, "m365": load_m365}


def docs_collections(tpl: Template, settings: DocsSettings) -> dict[str, list[Collection]]:
    """Section id -> the documentation collections its docs sources define."""
    return {s.id: [Collection(url=u, recursive=src.recursive, under=src.under,
                              max_pages=src.max_pages or settings.max_pages)
                   for src in s.sources if src.type == "docs" for u in src.urls]
            for s in tpl.sections}


async def gather_evidence(spec: SectionSpec, context: dict, http, loaders, max_chars: int,
                          docs=None, docs_keys: list[str] | None = None) -> list[dict]:
    items: list[dict] = []
    for src in spec.sources:
        if src.type != "docs":
            items += await loaders[src.type](src, http)
    if docs and docs_keys:  # local search in the collected snapshot: no network
        terms = query_terms(spec.retrieval_keywords, spec.title, *(t for _, t in spec.directive_list))
        items += docs.retrieve(docs_keys, terms)
    budget = max_chars // max(len(items), 1)
    for i, it in enumerate(items, 1):
        it["id"] = f"E{i}"
        if len(it["text"]) > budget:
            it["text"] = it["text"][:budget] + "\n[...truncated]"
    if spec.use_request_context:
        items.insert(0, {**context, "id": "R1"})
    return items


# =============================================================================
# 5. PROMPTS (generic; all request-specific text comes from the template and request files)
# =============================================================================
WRITER_SYSTEM = """You are an enterprise architect writing the BODY of ONE section of an architecture document.
The document template already provides every heading, so do not write any headings (no lines starting with #).
Write well-structured paragraphs; use bullet lists only where they make the content clearer.
Ground every statement in the REQUEST CONTEXT [R1] or the EVIDENCE [E1], [E2] ... and cite them inline like [R1].
Other sections of the OUTLINE are written by other agents: cover only your own directives, not their content.
Upstream sections, when given, are for consistency only.
Never invent figures, costs, dates, product capabilities or decisions. If something your directives need is
missing from the sources, say so in the text and add it to open_questions.
Return ONLY a JSON object with this shape:
{"body": "the section body in Markdown, no headings, with [R1]/[E1] citations",
 "claims": [{"statement": "a key claim made in the body", "evidence": ["R1", "E1"]}],
 "key_facts": {"<key fact name>": "<short canonical value>"},
 "assumptions": ["..."],
 "open_questions": ["..."],
 "directives_covered": ["<directive id>"],
 "rows": [["cell 1", "cell 2"]]}
"rows" is used only when the section is a table (see FORMAT); otherwise return an empty list."""

REVIEWER_SYSTEM = """You are the final reviewer of an architecture document whose sections were written by separate
agents, following a fixed OUTLINE. Check every section against the REQUEST CONTEXT and the document PURPOSE:
1. Contradictions between sections (component identity, what it replaces, platforms, alternatives, constraints,
   dates, numbers, terminology).
2. Directives not covered, or covered weakly.
3. Statements that conflict with the request's requirements, constraints or requested outcome, or drift out of scope.
4. Claims, figures or capabilities not supported by the cited sources.
5. Content placed in the wrong section, or heavily duplicated across sections.
Automated checks are listed in <findings>: confirm or dismiss them and add what they missed. When sections
disagree, prefer the value supported by the sources, then the section whose sources rank higher in SOURCE
PRECEDENCE. Request revisions only for sections that must change, with concrete instructions. Use verdict
"pass" only when no revision is needed. Refer to sections by their id.
Return ONLY a JSON object:
{"verdict": "pass" | "revise",
 "conflicts": [{"sections": ["id_a", "id_b"], "description": "...", "severity": "high|medium|low",
                "recommended_resolution": "..."}],
 "gaps": ["..."], "overlaps": ["..."],
 "revisions": [{"section_id": "...", "instructions": "..."}],
 "summary": "..."}"""


def writer_prompt(tpl: Template, spec: SectionSpec, evidence: list[dict], upstream: dict,
                  revision: str | None = None, previous: str | None = None) -> str:
    ctx = next((e for e in evidence if e["id"] == "R1"), None)
    ev = "\n\n".join(f"[{e['id']}] ({e['source']}: {e['locator']})\n{e['text']}" for e in evidence if e["id"] != "R1")
    up = "\n\n".join(f"--- {tpl.heading(tpl.section(sid))} ---\n{section_markdown(tpl.section(sid), d)[:6000]}\n"
                     f"Key facts: {json.dumps(d['key_facts'])}" for sid, d in upstream.items())
    directives = "\n".join(f"{did}: {text}" for did, text in spec.directive_list)
    parts = [f"DOCUMENT: {tpl.document_title}", f"PURPOSE: {tpl.purpose}",
             "OUTLINE (each section is written by a different agent):\n" + tpl.outline()]
    if ctx:
        parts.append(f'<request_context id="R1">\n{ctx["text"]}\n</request_context>')
    parts += [f"YOUR SECTION: {tpl.heading(spec)} (id: {spec.id})",
              "DIRECTIVES (cover all of them, in this order):\n" + directives,
              *([f"FORMAT: this section is a table. TABLE COLUMNS: {' | '.join(spec.table_columns)}\n"
                 f'Return one row per item in "rows", each row a list of {len(spec.table_columns)} cell values '
                 'in that column order. The script draws the table, so do not write a table in "body"; '
                 '"body" may be empty or one short introductory sentence.'] if spec.table_columns else []),
              "WRITING GUIDELINES:\n" + "\n".join(x for x in (tpl.writer_guidelines, spec.guidelines) if x.strip()),
              f"KEY FACTS to report (only those this section takes a position on): {', '.join(tpl.key_facts) or '(none)'}",
              "UPSTREAM SECTIONS (context only):\n" + (up or "(none)"),
              "<evidence>\n" + (ev or "(no extra evidence - use the request context)") + "\n</evidence>"]
    if revision:
        parts += [f"PREVIOUS DRAFT:\n{previous}",
                  f"REVISION REQUEST FROM THE REVIEWER:\n{revision}\nFix what was flagged, keep the rest, stay grounded."]
    return "\n\n".join(parts)


def reviewer_prompt(tpl: Template, context: dict, sections: dict, evidence: dict, findings: list) -> str:
    parts = [f"DOCUMENT: {tpl.document_title}", f"PURPOSE: {tpl.purpose}", "OUTLINE:\n" + tpl.outline(),
             f'<request_context id="R1">\n{context["text"][:20000]}\n</request_context>',
             f"SOURCE PRECEDENCE (highest first): {' > '.join(tpl.source_precedence)}",
             f"<findings>\n{json.dumps(findings, indent=1)}\n</findings>"]
    for s in tpl.sections:  # in document order
        d = sections[s.id]
        srcs = ", ".join(f"{e['id']}={e['source']}:{e['locator']}" for e in evidence.get(s.id, []))
        directives = "\n".join(f"{did}: {text}" for did, text in s.directive_list)
        claims = "\n".join(f"- {c['statement']} {c['evidence']}" for c in d["claims"][:40])
        parts.append(f"""<section id="{s.id}" heading="{tpl.heading(s)}">
Sources: {srcs or 'none'}
Directives:
{directives}
Key facts: {json.dumps(d['key_facts'])}
Claims:
{claims or '(none)'}
Open questions: {d['open_questions']}
Content:
{section_markdown(s, d)[:6000]}
</section>""")
    return "\n\n".join(parts)


# =============================================================================
# 6. DETERMINISTIC CHECKS (run before the reviewer LLM, cheap and reliable)
# =============================================================================
def norm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip()


def norm_fact(v: Any) -> str:  # order-insensitive for list-like values: "a, b" == "b and a"
    return " | ".join(sorted(p for p in (norm(x) for x in re.split(r"[,;/]|\band\b", str(v).lower())) if p))


FIGURE = re.compile(r"(?:[$€£]\s?\d[\d.,]*\s?[kKmM]?\b|\b\d+(?:[.,]\d+)?\s?(?:%|percent\b|ms\b|seconds?\b|minutes?\b|"
                    r"hours?\b|days?\b|weeks?\b|months?\b|years?\b|[KMGTP]i?B\b|USD\b|EUR\b|x\b))")


def deterministic_checks(tpl: Template, sections: dict, evidence: dict) -> list[dict]:
    findings: list[dict] = []
    rank = {t: i for i, t in enumerate(tpl.source_precedence)}
    order = {s.id: i for i, s in enumerate(tpl.sections)}
    src_text = {sid: "\n".join(e["text"] for e in evs) for sid, evs in evidence.items()}

    def best_rank(sid):
        return min((rank.get(e["source"], 99) for e in evidence.get(sid, [])), default=99)

    # (a) the same key fact stated differently by different sections
    by_key: dict[str, list[tuple[str, str]]] = {}
    for sid, d in sections.items():
        for k, v in d["key_facts"].items():
            by_key.setdefault(k, []).append((sid, v))
    for k, vals in sorted(by_key.items()):
        groups: dict[str, list[tuple[str, str]]] = {}
        for sid, v in vals:
            groups.setdefault(norm_fact(v), []).append((sid, v))
        if len(groups) < 2:
            continue
        found = {sid: norm(v) in norm(src_text.get(sid, "")) for sid, v in vals}
        best = min(groups.values(), key=lambda g: (not any(found[s] for s, _ in g), min(best_rank(s) for s, _ in g),
                                                    -len(g), min(order[s] for s, _ in g)))
        findings.append({"type": "key_fact_mismatch", "key": k,
                         "values": {sid: {"value": v, "found_in_sources": found[sid]} for sid, v in vals},
                         "preferred_value": best[0][1],
                         "preferred_because": "found in sources, then source precedence, then majority"})

    for s in tpl.sections:
        d = sections[s.id]
        # (b) directives the agent did not cover
        missing = [did for did, _ in s.directive_list if did not in d["directives_covered"]]
        if missing:
            findings.append({"type": "directives_not_covered", "section": s.id, "directives": missing})
        # (c) claims citing nothing, or citing sources that do not exist
        valid = {e["id"] for e in evidence.get(s.id, [])}
        bad = [c["statement"][:140] for c in d["claims"] if not c["evidence"] or not set(c["evidence"]) <= valid]
        if bad:
            findings.append({"type": "unsupported_claims", "section": s.id, "count": len(bad), "examples": bad[:5]})
        # (d) figures (%, money, durations, sizes) that appear in no source
        compact_src = re.sub(r"\s+", "", src_text.get(s.id, "")).lower()
        if s.table_columns and not d.get("rows"):
            findings.append({"type": "empty_table", "section": s.id, "columns": s.table_columns})
        figs = sorted({f.strip() for f in FIGURE.findall(section_markdown(s, d))
                       if re.sub(r"\s+", "", f).lower() not in compact_src})
        if figs:
            findings.append({"type": "ungrounded_figures", "section": s.id, "figures": figs[:10]})
    return findings


HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", flags=re.M)


def clean_draft(draft: SectionDraft, tpl: Template, spec: SectionSpec) -> dict:
    d = draft.model_dump()
    d["body"] = HEADING_LINE.sub(r"**\1**", d["body"]).strip()  # headings belong to the template only
    if tpl.key_facts:
        d["key_facts"] = {k: v for k, v in d["key_facts"].items() if k in tpl.key_facts}
    valid = {did for did, _ in spec.directive_list}
    d["directives_covered"] = [x for x in d["directives_covered"] if x in valid]
    n, rows = len(spec.table_columns), []
    for r in d["rows"] if n else []:
        r = [c.strip() for c in r]
        r = r[:n - 1] + [" ".join(r[n - 1:])] if len(r) > n else r + [""] * (n - len(r))
        if any(r):
            rows.append(r)
    d["rows"] = rows
    return d


def err_text(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def failed_draft(spec: SectionSpec, e: Exception) -> dict:
    return {"body": f"> **Section generation failed:** `{err_text(e)}`", "claims": [], "key_facts": {},
            "assumptions": [], "open_questions": [f"'{spec.title}' could not be generated: {err_text(e)}"],
            "directives_covered": [], "rows": []}


# =============================================================================
# 7. THE LANGGRAPH
# =============================================================================
def merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    sections: Annotated[dict, merge]       # reducer: parallel agents each add their own key
    evidence: Annotated[dict, merge]
    errors: Annotated[dict, merge]
    history: Annotated[list, operator.add]
    refreshed: Annotated[list, operator.add]
    docs_summary: list
    findings: list
    review: dict
    round: int


def build_graph(tpl: Template, context: dict, writer, reviewer, http, loaders, max_chars: int, out_dir: Path,
                docs=None, docs_keys: dict[str, list[str]] | None = None, refresh_docs: bool = False):
    by_id = {s.id: s for s in tpl.sections}
    node = lambda sid: f"sec_{sid}"  # noqa: E731

    def section_agent(spec: SectionSpec):
        async def run(state: State):
            log(f"start  {tpl.heading(spec)}")
            upstream = {d: state["sections"][d] for d in spec.depends_on}
            try:
                evidence = await gather_evidence(spec, context, http, loaders, max_chars,
                                                 docs, (docs_keys or {}).get(spec.id))
                draft = await writer.json(WRITER_SYSTEM, writer_prompt(tpl, spec, evidence, upstream), SectionDraft)
                result, err = clean_draft(draft, tpl, spec), {}
            except Exception as e:  # one failing source must not sink the run
                evidence, result, err = [], failed_draft(spec, e), {spec.id: err_text(e)}
            log(f"done   {tpl.heading(spec)}  sources={[e['id'] for e in evidence]}")
            return {"sections": {spec.id: result}, "evidence": {spec.id: evidence}, "errors": err}
        return run

    async def reconcile(state: State):
        rnd = state.get("round", 0) + 1
        findings = deterministic_checks(tpl, state["sections"], state["evidence"])
        log(f"review round {rnd}: {len(findings)} automated finding(s)")
        try:
            prompt = reviewer_prompt(tpl, context, state["sections"], state["evidence"], findings)
            review = (await reviewer.json(REVIEWER_SYSTEM, prompt, Review)).model_dump()
        except Exception as e:
            review = {"verdict": "error", "conflicts": [], "gaps": [], "overlaps": [], "revisions": [],
                      "summary": f"Reviewer failed ({err_text(e)}); only automated findings are available."}
        review["revisions"] = [r for r in review["revisions"] if r["section_id"] in by_id]
        log(f"review round {rnd}: verdict={review['verdict']} revise={[r['section_id'] for r in review['revisions']]}")
        return {"findings": findings, "review": review, "round": rnd,
                "history": [{"round": rnd, "verdict": review["verdict"], "conflicts": len(review["conflicts"]),
                             "revised": [r["section_id"] for r in review["revisions"]]}]}

    def route(state: State):
        rv = state["review"]
        if rv["verdict"] == "revise" and rv["revisions"] and state["round"] <= tpl.max_revision_rounds:
            return [Send("revise_section", {"section_id": r["section_id"], "instructions": r["instructions"],
                                            "sections": state["sections"], "evidence": state["evidence"]})
                    for r in rv["revisions"]]  # flagged sections are revised in parallel
        return "finalize"

    async def revise_section(payload: dict):
        spec = by_id[payload["section_id"]]
        log(f"revise {tpl.heading(spec)}")
        upstream = {d: payload["sections"][d] for d in spec.depends_on}
        prompt = writer_prompt(tpl, spec, payload["evidence"].get(spec.id, []), upstream,
                               revision=payload["instructions"],
                               previous=section_markdown(spec, payload["sections"][spec.id]))
        try:  # same cached evidence: same sources of truth, reproducible
            draft = await writer.json(WRITER_SYSTEM, prompt, SectionDraft)
            return {"sections": {spec.id: clean_draft(draft, tpl, spec)}}
        except Exception as e:
            return {"errors": {spec.id: f"revision failed: {err_text(e)}"}}

    dependents = {s.id: [t.id for t in tpl.sections if s.id in t.depends_on] for s in tpl.sections}

    def downstream(ids: set[str]) -> set[str]:
        seen: set[str] = set()
        stack = list(ids)
        while stack:
            for nxt in dependents[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    async def refresh_dependents(state: State):
        """A revised section can make the sections built on it stale (e.g. a summary written last):
        re-run those dependents, wave by wave, so the next review sees a consistent document."""
        revised = {r["section_id"] for r in state["review"]["revisions"]}
        stale = downstream(revised) - revised
        if not stale:
            return {}
        sections, updates = dict(state["sections"]), {}

        async def rerun(spec: SectionSpec):
            log(f"refresh {tpl.heading(spec)} (upstream revised)")
            prompt = writer_prompt(tpl, spec, state["evidence"].get(spec.id, []),
                                   {d: sections[d] for d in spec.depends_on},
                                   revision="Upstream sections were revised. Update this section so it stays "
                                            "consistent with them.", previous=section_markdown(spec, sections[spec.id]))
            try:
                return clean_draft(await writer.json(WRITER_SYSTEM, prompt, SectionDraft), tpl, spec)
            except Exception as e:
                log(f"refresh failed for {spec.id}: {err_text(e)}")
                return None
        for wave in execution_order(tpl):
            todo = [sid for sid in wave if sid in stale]
            for sid, d in zip(todo, await asyncio.gather(*(rerun(by_id[x]) for x in todo))):
                if d:
                    sections[sid] = updates[sid] = d
        return {"sections": updates, "refreshed": sorted(updates)}

    async def finalize(state: State):
        write_outputs(tpl, state, out_dir)
        return {}

    async def collect_docs(state: State):
        try:
            return {"docs_summary": await docs.collect(refresh=refresh_docs)}
        except Exception as e:  # agents still run, with the requirements and their other sources
            return {"docs_summary": [], "errors": {"official docs": err_text(e)}}

    g = StateGraph(State)
    first = START
    if docs:
        g.add_node("collect_docs", collect_docs)                      # runs once, before every agent
        g.add_edge(START, "collect_docs")
        first = "collect_docs"
    for s in tpl.sections:
        g.add_node(node(s.id), section_agent(s))
    for s in tpl.sections:
        if not s.depends_on:
            g.add_edge(first, node(s.id))                              # fan-out: starts immediately
        elif len(s.depends_on) == 1:
            g.add_edge(node(s.depends_on[0]), node(s.id))              # sequence
        else:
            g.add_edge([node(d) for d in s.depends_on], node(s.id))    # join: waits for all
    g.add_node("reconcile", reconcile)
    g.add_node("revise_section", revise_section)
    g.add_node("refresh_dependents", refresh_dependents)
    g.add_node("finalize", finalize)
    all_sections = [node(s.id) for s in tpl.sections]
    g.add_edge(all_sections if len(all_sections) > 1 else all_sections[0], "reconcile")  # fan-in
    g.add_conditional_edges("reconcile", route, ["revise_section", "finalize"])
    g.add_edge("revise_section", "refresh_dependents")
    g.add_edge("refresh_dependents", "reconcile")
    g.add_edge("finalize", END)
    return g.compile()


# =============================================================================
# 8. OUTPUTS - headings come from the template, in template order
# =============================================================================
CITATION = re.compile(r"\s?\[(?:R|E)\d+(?:\s*[,;]\s*(?:R|E)\d+)*\]")


def _cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_table(columns: list[str], rows: list[list[str]], strip_citations: bool) -> str:
    def cell(x: str) -> str:
        return (CITATION.sub("", x) if strip_citations else x).replace("|", "\\|").replace("\n", " ").strip()
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    return "\n".join(lines + ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows])


def section_markdown(spec: SectionSpec, d: dict, annotated: bool = True) -> str:
    """The text printed under a section heading: the body, then the table for table sections."""
    body = (d["body"] if annotated else CITATION.sub("", d["body"])).strip()
    parts = [body] if body else []
    if spec.table_columns:
        parts.append(render_table(spec.table_columns, d.get("rows", []), strip_citations=not annotated))
    return "\n\n".join(parts)


def render_document(tpl: Template, state: State, annotated: bool) -> str:
    lines = [f"# {tpl.document_title}", ""]
    for s in tpl.sections:                                   # template order, never execution order
        lines += [f"## {tpl.heading(s)}", "", section_markdown(s, state["sections"][s.id], annotated), ""]
        if annotated:
            lines += ["*Sources:*", ""] + [f"- [{e['id']}] {e['source']}: {e['locator']} (sha256 {e['sha256']})"
                                          for e in state["evidence"].get(s.id, [])] + [""]
    return "\n".join(lines)


def render_report(tpl: Template, state: State) -> str:
    rv = state.get("review", {})
    by_id = {s.id: s for s in tpl.sections}
    status = {"pass": "PASS", "revise": "UNRESOLVED - human decision needed (revision rounds exhausted)"} \
        .get(rv.get("verdict"), "REVIEWER ERROR - human review needed")
    L = [f"# Reconciliation report - {tpl.document_title}", "", f"**Status:** {status}  ",
         f"**Review rounds:** {state.get('round', 0)}", "", rv.get("summary", ""), "", "## Conflicts", ""]
    if rv.get("conflicts"):
        L += ["| Severity | Sections | Description | Recommended resolution |", "|---|---|---|---|"]
        L += [f"| {c['severity']} | {', '.join(tpl.heading(by_id[x]) if x in by_id else x for x in c['sections'])} | "
              f"{_cell(c['description'])} | {_cell(c['recommended_resolution'])} |" for c in rv["conflicts"]]
    else:
        L.append("None remaining.")
    L += ["", "## Gaps against the request", ""] + ([f"- {g}" for g in rv.get("gaps", [])] or ["None."])
    L += ["", "## Overlaps between sections", ""] + ([f"- {o}" for o in rv.get("overlaps", [])] or ["None."])
    L += ["", "## Automated findings (final pass)", "", "```json", json.dumps(state.get("findings", []), indent=2),
          "```", "", "## Revision history", ""]
    L += [f"- Round {h['round']}: {h['verdict']}, {h['conflicts']} conflict(s); revised: {', '.join(h['revised']) or '-'}"
          for h in state.get("history", [])]
    if state.get("refreshed"):
        L.append(f"- Dependent sections refreshed after revisions: {', '.join(state['refreshed'])}")
    oq = [(tpl.heading(s), q) for s in tpl.sections for q in state["sections"][s.id]["open_questions"]]
    L += ["", "## Open questions", ""] + ([f"- **{h}:** {q}" for h, q in oq] or ["None."])
    if state.get("docs_summary"):
        L += ["", "## Official documentation used", "",
              "| Start URL | Recursive | Pages | Skipped | Collected | Snapshot folder |", "|---|---|---|---|---|---|"]
        L += [f"| {d['url']} | {'yes' if d['recursive'] else 'no'} | {d['pages']} | {d['skipped']} | "
              f"{d['collected_at'][:10]} | {d['folder']} |" for d in state["docs_summary"]]
    if state.get("errors"):
        L += ["", "## Errors", ""] + [f"- **{k}:** {v}" for k, v in state["errors"].items()]
    return "\n".join(L) + "\n"


def write_outputs(tpl: Template, state: State, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "document.md").write_text(render_document(tpl, state, annotated=False), encoding="utf-8")
    (out / "document_annotated.md").write_text(render_document(tpl, state, annotated=True), encoding="utf-8")
    (out / "reconciliation_report.md").write_text(render_report(tpl, state), encoding="utf-8")
    keep = ("sections", "evidence", "errors", "findings", "review", "history", "refreshed", "docs_summary")
    (out / "run.json").write_text(json.dumps({k: state.get(k) for k in keep}, indent=2), encoding="utf-8")
    log(f"wrote {out}/document.md, document_annotated.md, reconciliation_report.md, run.json")


# =============================================================================
# 9. DRY RUN (offline stand-ins for the LLMs and sources)
# =============================================================================
class FakeLLM:
    """Plants a wrong key fact, an invented figure and a stray heading in one section, so the dry run
    shows heading clean-up, the automated checks, the reviewer and the targeted revision loop."""

    def __init__(self, planted_section_id: str):
        self.planted = planted_section_id

    async def json(self, system: str, user: str, model_cls):
        await asyncio.sleep(0.5)
        if model_cls is SectionDraft:
            sid = re.search(r"\(id: ([\w-]+)\)", user).group(1)
            dids = re.findall(r"^(\S+:d\d+): ", user, flags=re.M)
            wrong = sid == self.planted and "REVISION REQUEST" not in user
            body = "\n\n".join(f"Dry-run paragraph for {d} [R1]." for d in dids)
            if wrong:
                body = "### A heading the model should not write\n\n" + body + "\n\nIt cuts operating cost by 40%."
            facts = {"component_name": "Amazon S3 Files",
                     "target_platform": "Amazon Linux 2" if wrong else "Amazon Linux 2023"}
            cols = re.search(r"TABLE COLUMNS: (.*)", user)
            rows = [[f"Sample {c.strip().lower()} {i} [R1]" for c in cols.group(1).split("|")] for i in (1, 2, 3)] \
                if cols else []
            if cols:
                body = "Accepted use cases [R1]."
            return SectionDraft(body=body, key_facts=facts, directives_covered=dids, rows=rows,
                                claims=[Claim(statement=f"{sid} claim", evidence=["R1"])])
        findings = json.loads(re.search(r"<findings>\n(.*?)\n</findings>", user, flags=re.S).group(1))
        revs: dict[str, str] = {}
        for f in findings:
            if f["type"] == "key_fact_mismatch":
                for sid, v in f["values"].items():
                    if norm_fact(v["value"]) != norm_fact(f["preferred_value"]):
                        revs[sid] = revs.get(sid, "") + f"Use {f['key']} = {f['preferred_value']}. "
            if f["type"] == "ungrounded_figures":
                revs[f["section"]] = revs.get(f["section"], "") + f"Remove unsourced figures {f['figures']}. "
        return Review(verdict="revise" if revs else "pass",
                      conflicts=[Conflict(sections=list(f["values"]), severity="high",
                                          description=f"{f['key']} differs between sections",
                                          recommended_resolution=f"Use {f['preferred_value']}")
                                 for f in findings if f["type"] == "key_fact_mismatch"],
                      revisions=[Revision(section_id=k, instructions=v) for k, v in revs.items()],
                      summary="Dry-run review.")


class FakeDocs:
    """Dry-run stand-in for DocsLibrary: nothing is downloaded."""

    def __init__(self, collections: list[Collection]):
        self.collections = collections

    async def collect(self, refresh: bool = False) -> list[dict]:
        log("docs   dry run: nothing downloaded")
        return [{"url": c.url, "recursive": c.recursive, "prefixes": c.prefixes, "pages": 0, "skipped": 0,
                 "collected_at": "dry-run", "folder": "dry-run"} for c in self.collections]

    def retrieve(self, keys: list[str], terms: list[str]) -> list[dict]:
        return [_ev("docs", f"dry-run:{k} › passage", "Sample official documentation passage.") for k in keys[:1]]


async def fake_loader(src: SourceSpec, http) -> list[dict]:
    locs = src.urls or src.paths or ([src.query] if src.query else [])
    return [_ev(src.type, f"dry-run:{loc}", f"Sample {src.type} evidence.") for loc in locs]


# =============================================================================
# 10. MAIN
# =============================================================================
async def amain() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--template", required=True, help="reusable template YAML (outline + directives)")
    ap.add_argument("-r", "--request", required=True, help="request Markdown (global context + front matter)")
    ap.add_argument("-c", "--config", default="config.yaml", help="environment config YAML")
    ap.add_argument("-o", "--out", help="output folder (default: <output_dir>/<request file name>)")
    ap.add_argument("--dry-run", action="store_true", help="no LLM or network calls")
    ap.add_argument("--graph", action="store_true", help="print the graph as Mermaid and exit")
    ap.add_argument("--collect-only", action="store_true",
                    help="only collect the official documentation (for review), then exit")
    ap.add_argument("--refresh-docs", action="store_true", help="download the documentation again, ignoring the cache")
    args = ap.parse_args()

    tpl, variables, context_text, cfg = load_inputs(args.template, args.request, args.config)
    waves = execution_order(tpl)
    context = _ev("request", args.request, context_text)
    out_dir = Path(args.out or Path(cfg.output_dir) / Path(args.request).stem)
    log(f"document: {tpl.document_title}")
    log("outline:  " + " | ".join(tpl.heading(s) for s in tpl.sections))
    log("run order: " + " -> ".join("[" + ", ".join(w) + "]" for w in waves))
    by_section = docs_collections(tpl, cfg.docs)
    docs_keys = {sid: [c.key for c in cs] for sid, cs in by_section.items()}
    all_collections = list({c.key: c for cs in by_section.values() for c in cs}.values())
    docs = None
    if all_collections:
        docs = FakeDocs(all_collections) if (args.dry_run or args.graph) else DocsLibrary(cfg.docs, all_collections)

    if args.collect_only:
        if not docs:
            raise SystemExit("the template has no docs sources")
        for d in await docs.collect(refresh=args.refresh_docs):
            print(f"{d['url']}\n  recursive: {d['recursive']}  pages: {d['pages']}  skipped: {d['skipped']}"
                  f"  collected: {d['collected_at']}\n  review: {d['folder']}/pages  (details: manifest.json)")
        print("\nDelete any page file you do not want used, then run without --collect-only.")
        return

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "docgen/0.6"}) as http:
        if args.dry_run or args.graph:
            writer = reviewer = FakeLLM(tpl.sections[-1].id)
            loaders = {k: fake_loader for k in LOADERS}
        else:
            if not {"writer", "reviewer"} <= set(cfg.llm):
                raise SystemExit(f"{args.config} must define llm.writer and llm.reviewer")
            writer, reviewer = LLM(cfg.llm["writer"], "writer"), LLM(cfg.llm["reviewer"], "reviewer")
            loaders = LOADERS
            await asyncio.gather(writer.preflight(), reviewer.preflight())
            if any(src.type == "m365" for s in tpl.sections for src in s.sources):
                GRAPH_TOKEN["token"] = await asyncio.to_thread(acquire_graph_token)  # one SSO prompt per run
        graph = build_graph(tpl, context, writer, reviewer, http, loaders, cfg.max_evidence_chars, out_dir,
                            docs, docs_keys, args.refresh_docs)
        if args.graph:
            print(graph.get_graph().draw_mermaid())
            return
        await graph.ainvoke({"sections": {}, "evidence": {}, "errors": {}, "history": [], "refreshed": [], "round": 0},
                            {"recursion_limit": 100})


if __name__ == "__main__":
    asyncio.run(amain())
