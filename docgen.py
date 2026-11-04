#!/usr/bin/env python3
"""
docgen.py - template-driven multi-agent document generator built on LangGraph.

Three inputs, kept separate so the same engine serves any similar request:
  --template  reusable    : document purpose, sections (agents), their questions, sources, checks
  --request   per request : global context (Markdown) + YAML front-matter variables
  --config    per env     : LLM endpoints (any OpenAI-compatible /v1 API), output folder

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

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}", file=sys.stderr)


# =============================================================================
# 1. INPUTS: template (reusable), request (global context + variables), config (environment)
# =============================================================================
class SourceSpec(BaseModel):
    type: Literal["file", "url", "web", "m365"]
    paths: list[str] = []                  # file
    urls: list[str] = []                   # url
    query: Optional[str] = None            # web, m365 (m365: one sentence, <= 1,500 chars)
    allow_domains: list[str] = []          # web
    max_results: int = 5                   # web, m365 (m365 max 25)
    data_source: str = "sharePoint"        # m365: sharePoint | oneDriveBusiness | externalItem
    filter_expression: Optional[str] = None  # m365 KQL, e.g. path:"https://contoso.sharepoint.com/sites/x/"


class SectionSpec(BaseModel):
    id: str                                # machine id, used for node names and dependencies
    name: str                              # display name, e.g. "Drivers & motivation"
    questions: str                         # free text; one question per non-empty line
    sources: list[SourceSpec] = []         # extra sources of truth on top of the request context
    use_request_context: bool = True       # global context [R1] given to this agent
    depends_on: list[str] = []             # empty = runs in parallel from the start
    guidelines: str = ""                   # section-specific writing guidance

    @property
    def question_list(self) -> list[tuple[str, str]]:
        lines = [ln.strip() for ln in self.questions.splitlines() if ln.strip()]
        return [(f"{self.id}:q{i}", q) for i, q in enumerate(lines, 1)]


class Template(BaseModel):
    document_title: str
    purpose: str
    required_vars: list[str] = []
    defaults: dict[str, Any] = {}
    writer_guidelines: str = ""
    key_facts: list[str] = []
    source_precedence: list[str] = ["request", "file", "m365", "url", "web"]
    max_revision_rounds: int = 1
    sections: list[SectionSpec]


class LLMRole(BaseModel):
    base_url: str
    model: str
    api_key_env: Optional[str] = None
    temperature: float = 0.2
    max_concurrency: int = 4
    json_mode: bool = False                # true only if the endpoint supports response_format=json_object
    timeout_s: float = 300


class Config(BaseModel):
    llm: dict[str, LLMRole] = {}
    output_dir: str = "out"
    max_evidence_chars: int = 24000


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


def topo_order(tpl: Template) -> list[str]:
    ids = [s.id for s in tpl.sections]
    if len(ids) != len(set(ids)):
        raise SystemExit("section ids must be unique")
    deps = {s.id: set(s.depends_on) for s in tpl.sections}
    for sid, d in deps.items():
        if unknown := d - set(ids):
            raise SystemExit(f"section '{sid}' depends on unknown section(s) {sorted(unknown)}")
    order: list[str] = []
    while deps:
        ready = [k for k, v in deps.items() if not v]
        if not ready:
            raise SystemExit(f"dependency cycle among sections {sorted(deps)}")
        order += ready
        for k in ready:
            deps.pop(k)
        for v in deps.values():
            v.difference_update(ready)
    return order


# =============================================================================
# 2. WHAT THE LLMs MUST RETURN (validated with Pydantic)
# =============================================================================
class Claim(BaseModel):
    statement: str
    evidence: list[str] = []


class SectionDraft(BaseModel):
    markdown: str
    claims: list[Claim] = []
    key_facts: dict[str, str] = {}
    assumptions: list[str] = []
    open_questions: list[str] = []
    questions_answered: list[str] = []

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


class LLM:
    def __init__(self, role: LLMRole, name: str):
        from openai import AsyncOpenAI
        key = os.environ.get(role.api_key_env, "") if role.api_key_env else ""
        self.client = AsyncOpenAI(base_url=role.base_url, api_key=key or "not-needed", timeout=role.timeout_s)
        self.role, self.name = role, name
        self.sem = asyncio.Semaphore(role.max_concurrency)

    async def json(self, system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra = {"response_format": {"type": "json_object"}} if self.role.json_mode else {}
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


async def gather_evidence(spec: SectionSpec, context: dict, http, loaders, max_chars: int) -> list[dict]:
    items: list[dict] = []
    for src in spec.sources:
        items += await loaders[src.type](src, http)
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
WRITER_SYSTEM = """You are an enterprise architect writing ONE section of an architecture document.
Ground every statement in the REQUEST CONTEXT [R1] or the EVIDENCE [E1], [E2] ... and cite them inline.
Upstream sections are context for consistency, not evidence.
Never invent figures, costs, dates, product capabilities or decisions. If something the questions need is
missing from the sources, say so in the text and add it to open_questions.
Return ONLY a JSON object with this shape:
{"markdown": "the section body in Markdown, one '### ' heading per question, with [R1]/[E1] citations",
 "claims": [{"statement": "a key claim made in the text", "evidence": ["R1", "E1"]}],
 "key_facts": {"<key fact name>": "<short canonical value>"},
 "assumptions": ["..."],
 "open_questions": ["..."],
 "questions_answered": ["<question id>"]}"""

REVIEWER_SYSTEM = """You are the final reviewer of an architecture document whose sections were written in parallel
by separate agents. Check every section against the REQUEST CONTEXT and the document PURPOSE:
1. Contradictions between sections (component identity, what it replaces, platforms, alternatives, constraints,
   dates, numbers, terminology).
2. Questions not answered, or answered weakly.
3. Statements that conflict with the request's requirements, constraints or requested outcome, or drift out of scope.
4. Claims, figures or capabilities not supported by the cited sources.
5. Heavy duplication between sections (each section should own its own questions).
Automated checks are listed in <findings>: confirm or dismiss them and add what they missed. When sections
disagree, prefer the value supported by the sources, then the section whose sources rank higher in SOURCE
PRECEDENCE. Request revisions only for sections that must change, with concrete instructions. Use verdict
"pass" only when no revision is needed.
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
    up = "\n\n".join(f"### {sid}\n{d['markdown'][:6000]}\nKey facts: {json.dumps(d['key_facts'])}"
                     for sid, d in upstream.items())
    qs = "\n".join(f"{qid}: {q}" for qid, q in spec.question_list)
    parts = [f"DOCUMENT: {tpl.document_title}", f"PURPOSE: {tpl.purpose}"]
    if ctx:
        parts.append(f'<request_context id="R1">\n{ctx["text"]}\n</request_context>')
    parts += [f"SECTION: {spec.name} (id: {spec.id})",
              "Answer each question under its own '### ' heading, in this order:\n" + qs,
              "WRITING GUIDELINES:\n" + "\n".join(x for x in (tpl.writer_guidelines, spec.guidelines) if x.strip()),
              f"KEY FACTS to report (only those this section takes a position on): {', '.join(tpl.key_facts) or '(none)'}",
              "UPSTREAM SECTIONS (context only):\n" + (up or "(none)"),
              "<evidence>\n" + (ev or "(no extra evidence - use the request context)") + "\n</evidence>"]
    if revision:
        parts += [f"PREVIOUS DRAFT:\n{previous}",
                  f"REVISION REQUEST FROM THE REVIEWER:\n{revision}\nFix what was flagged, keep the rest, stay grounded."]
    return "\n\n".join(parts)


def reviewer_prompt(tpl: Template, context: dict, sections: dict, evidence: dict, findings: list, by_id: dict) -> str:
    parts = [f"DOCUMENT: {tpl.document_title}", f"PURPOSE: {tpl.purpose}",
             f'<request_context id="R1">\n{context["text"][:20000]}\n</request_context>',
             f"SOURCE PRECEDENCE (highest first): {' > '.join(tpl.source_precedence)}",
             f"<findings>\n{json.dumps(findings, indent=1)}\n</findings>"]
    for sid, d in sections.items():
        s = by_id[sid]
        srcs = ", ".join(f"{e['id']}={e['source']}:{e['locator']}" for e in evidence.get(sid, []))
        qs = "\n".join(f"{qid}: {q}" for qid, q in s.question_list)
        claims = "\n".join(f"- {c['statement']} {c['evidence']}" for c in d["claims"][:40])
        parts.append(f"""<section id="{sid}" name="{s.name}">
Sources: {srcs or 'none'}
Questions:
{qs}
Key facts: {json.dumps(d['key_facts'])}
Claims:
{claims or '(none)'}
Open questions: {d['open_questions']}
Body:
{d['markdown'][:6000]}
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


def deterministic_checks(tpl: Template, sections: dict, evidence: dict, by_id: dict) -> list[dict]:
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

    for sid, d in sections.items():
        # (b) questions the agent did not answer
        missing = [qid for qid, _ in by_id[sid].question_list if qid not in d["questions_answered"]]
        if missing:
            findings.append({"type": "unanswered_questions", "section": sid, "questions": missing})
        # (c) claims citing nothing, or citing sources that do not exist
        valid = {e["id"] for e in evidence.get(sid, [])}
        bad = [c["statement"][:140] for c in d["claims"] if not c["evidence"] or not set(c["evidence"]) <= valid]
        if bad:
            findings.append({"type": "unsupported_claims", "section": sid, "count": len(bad), "examples": bad[:5]})
        # (d) figures (%, money, durations, sizes) that appear in no source
        compact_src = re.sub(r"\s+", "", src_text.get(sid, "")).lower()
        figs = sorted({f.strip() for f in FIGURE.findall(d["markdown"])
                       if re.sub(r"\s+", "", f).lower() not in compact_src})
        if figs:
            findings.append({"type": "ungrounded_figures", "section": sid, "figures": figs[:10]})
    return findings


def clean_draft(draft: SectionDraft, tpl: Template, spec: SectionSpec) -> dict:
    d = draft.model_dump()
    if tpl.key_facts:
        d["key_facts"] = {k: v for k, v in d["key_facts"].items() if k in tpl.key_facts}
    valid_q = {qid for qid, _ in spec.question_list}
    d["questions_answered"] = [q for q in d["questions_answered"] if q in valid_q]
    return d


def err_text(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def failed_draft(spec: SectionSpec, e: Exception) -> dict:
    return {"markdown": f"> **Section generation failed:** `{err_text(e)}`", "claims": [], "key_facts": {},
            "assumptions": [], "open_questions": [f"'{spec.name}' could not be generated: {err_text(e)}"],
            "questions_answered": []}


# =============================================================================
# 7. THE LANGGRAPH
# =============================================================================
def merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    sections: Annotated[dict, merge]       # reducer: parallel nodes each add their own key
    evidence: Annotated[dict, merge]
    errors: Annotated[dict, merge]
    history: Annotated[list, operator.add]
    findings: list
    review: dict
    round: int


def build_graph(tpl: Template, context: dict, writer, reviewer, http, loaders, max_chars: int, out_dir: Path):
    by_id = {s.id: s for s in tpl.sections}
    node = lambda sid: f"sec_{sid}"  # noqa: E731

    def section_agent(spec: SectionSpec):
        async def run(state: State):
            log(f"start  {spec.name}")
            upstream = {d: state["sections"][d] for d in spec.depends_on}
            try:
                evidence = await gather_evidence(spec, context, http, loaders, max_chars)
                draft = await writer.json(WRITER_SYSTEM, writer_prompt(tpl, spec, evidence, upstream), SectionDraft)
                result, err = clean_draft(draft, tpl, spec), {}
            except Exception as e:  # one failing source must not sink the run
                evidence, result, err = [], failed_draft(spec, e), {spec.id: err_text(e)}
            log(f"done   {spec.name}  sources={[e['id'] for e in evidence]} key_facts={result['key_facts']}")
            return {"sections": {spec.id: result}, "evidence": {spec.id: evidence}, "errors": err}
        return run

    async def reconcile(state: State):
        rnd = state.get("round", 0) + 1
        findings = deterministic_checks(tpl, state["sections"], state["evidence"], by_id)
        log(f"review round {rnd}: {len(findings)} automated finding(s)")
        try:
            prompt = reviewer_prompt(tpl, context, state["sections"], state["evidence"], findings, by_id)
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
        sid = payload["section_id"]
        spec = by_id[sid]
        log(f"revise {spec.name}")
        upstream = {d: payload["sections"][d] for d in spec.depends_on}
        prompt = writer_prompt(tpl, spec, payload["evidence"].get(sid, []), upstream,
                               revision=payload["instructions"], previous=payload["sections"][sid]["markdown"])
        try:  # same cached evidence: same sources of truth, reproducible
            return {"sections": {sid: clean_draft(await writer.json(WRITER_SYSTEM, prompt, SectionDraft), tpl, spec)}}
        except Exception as e:
            return {"errors": {sid: f"revision failed: {err_text(e)}"}}

    async def finalize(state: State):
        write_outputs(tpl, state, by_id, out_dir)
        return {}

    g = StateGraph(State)
    for s in tpl.sections:
        g.add_node(node(s.id), section_agent(s))
    for s in tpl.sections:
        if not s.depends_on:
            g.add_edge(START, node(s.id))                              # fan-out: all start together
        elif len(s.depends_on) == 1:
            g.add_edge(node(s.depends_on[0]), node(s.id))              # sequence
        else:
            g.add_edge([node(d) for d in s.depends_on], node(s.id))    # join: waits for all
    g.add_node("reconcile", reconcile)
    g.add_node("revise_section", revise_section)
    g.add_node("finalize", finalize)
    all_sections = [node(s.id) for s in tpl.sections]
    g.add_edge(all_sections if len(all_sections) > 1 else all_sections[0], "reconcile")  # fan-in
    g.add_conditional_edges("reconcile", route, ["revise_section", "finalize"])
    g.add_edge("revise_section", "reconcile")
    g.add_edge("finalize", END)
    return g.compile()


# =============================================================================
# 8. OUTPUTS
# =============================================================================
def _cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_report(tpl: Template, state: State, by_id: dict) -> str:
    rv = state.get("review", {})
    status = {"pass": "PASS", "revise": "UNRESOLVED - human decision needed (revision rounds exhausted)"} \
        .get(rv.get("verdict"), "REVIEWER ERROR - human review needed")
    L = [f"# Reconciliation report - {tpl.document_title}", "", f"**Status:** {status}  ",
         f"**Review rounds:** {state.get('round', 0)}", "", rv.get("summary", ""), "", "## Conflicts", ""]
    if rv.get("conflicts"):
        L += ["| Severity | Sections | Description | Recommended resolution |", "|---|---|---|---|"]
        L += [f"| {c['severity']} | {', '.join(c['sections'])} | {_cell(c['description'])} | "
              f"{_cell(c['recommended_resolution'])} |" for c in rv["conflicts"]]
    else:
        L.append("None remaining.")
    L += ["", "## Gaps against the request", ""] + ([f"- {g}" for g in rv.get("gaps", [])] or ["None."])
    L += ["", "## Overlaps between sections", ""] + ([f"- {o}" for o in rv.get("overlaps", [])] or ["None."])
    L += ["", "## Automated findings (final pass)", "", "```json", json.dumps(state.get("findings", []), indent=2),
          "```", "", "## Revision history", ""]
    L += [f"- Round {h['round']}: {h['verdict']}, {h['conflicts']} conflict(s); revised: {', '.join(h['revised']) or '-'}"
          for h in state.get("history", [])]
    oq = [(by_id[sid].name, q) for sid, d in state["sections"].items() for q in d["open_questions"]]
    L += ["", "## Open questions", ""] + ([f"- **{n}:** {q}" for n, q in oq] or ["None."])
    if state.get("errors"):
        L += ["", "## Errors", ""] + [f"- **{k}:** {v}" for k, v in state["errors"].items()]
    return "\n".join(L) + "\n"


def write_outputs(tpl: Template, state: State, by_id: dict, out: Path) -> None:
    (out / "sections").mkdir(parents=True, exist_ok=True)
    doc = [f"# {tpl.document_title}", ""]
    for sid in topo_order(tpl):
        spec, d = by_id[sid], state["sections"][sid]
        srcs = "\n".join(f"- [{e['id']}] {e['source']}: {e['locator']} (sha256 {e['sha256']})"
                         for e in state["evidence"].get(sid, [])) or "- none"
        body = f"## {spec.name}\n\n{d['markdown']}\n\n**Sources**\n\n{srcs}\n"
        (out / "sections" / f"{sid}.md").write_text(body, encoding="utf-8")
        doc.append(body)
    (out / "document.md").write_text("\n".join(doc), encoding="utf-8")
    (out / "reconciliation_report.md").write_text(render_report(tpl, state, by_id), encoding="utf-8")
    keep = ("sections", "evidence", "errors", "findings", "review", "history")
    (out / "run.json").write_text(json.dumps({k: state.get(k) for k in keep}, indent=2), encoding="utf-8")
    log(f"wrote {out}/document.md, reconciliation_report.md, run.json")


# =============================================================================
# 9. DRY RUN (offline stand-ins for the LLMs and sources)
# =============================================================================
class FakeLLM:
    """Plants a wrong key fact and an invented figure in the second section, so the dry run shows
    the automated checks, the reviewer, and the targeted revision loop at work."""

    def __init__(self, first_section_id: str):
        self.first_id = first_section_id

    async def json(self, system: str, user: str, model_cls):
        await asyncio.sleep(0.5)
        if model_cls is SectionDraft:
            sid = re.search(r"\(id: ([\w-]+)\)", user).group(1)
            qids = re.findall(r"^(\S+:q\d+): ", user, flags=re.M)
            first = sid == self.first_id
            revising = "REVISION REQUEST" in user
            facts = {"component_name": "Amazon S3 Files",
                     "target_platform": "Amazon Linux 2023" if first or revising else "Amazon Linux 2"}
            extra = "" if first or revising else "\n\nIt cuts operating cost by 40%."
            body = "\n\n".join(f"### {q}\nDry-run answer [R1]." for q in qids) + extra
            return SectionDraft(markdown=body, key_facts=facts, questions_answered=qids,
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


async def fake_loader(src: SourceSpec, http) -> list[dict]:
    locs = src.urls or src.paths or ([src.query] if src.query else [])
    return [_ev(src.type, f"dry-run:{loc}", f"Sample {src.type} evidence.") for loc in locs]


# =============================================================================
# 10. MAIN
# =============================================================================
async def amain() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--template", required=True, help="reusable template YAML")
    ap.add_argument("-r", "--request", required=True, help="request Markdown (global context + front matter)")
    ap.add_argument("-c", "--config", default="config.yaml", help="environment config YAML")
    ap.add_argument("-o", "--out", help="output folder (default: <output_dir>/<request file name>)")
    ap.add_argument("--dry-run", action="store_true", help="no LLM or network calls")
    ap.add_argument("--graph", action="store_true", help="print the graph as Mermaid and exit")
    args = ap.parse_args()

    tpl, variables, context_text, cfg = load_inputs(args.template, args.request, args.config)
    order = topo_order(tpl)
    context = _ev("request", args.request, context_text)
    out_dir = Path(args.out or Path(cfg.output_dir) / Path(args.request).stem)
    log(f"'{tpl.document_title}': {len(tpl.sections)} agents, order {' -> '.join(order)}")

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "docgen/0.2"}) as http:
        if args.dry_run or args.graph:
            writer = reviewer = FakeLLM(tpl.sections[0].id)
            loaders = {k: fake_loader for k in LOADERS}
        else:
            if not {"writer", "reviewer"} <= set(cfg.llm):
                raise SystemExit(f"{args.config} must define llm.writer and llm.reviewer")
            writer, reviewer = LLM(cfg.llm["writer"], "writer"), LLM(cfg.llm["reviewer"], "reviewer")
            loaders = LOADERS
            if any(src.type == "m365" for s in tpl.sections for src in s.sources):
                GRAPH_TOKEN["token"] = await asyncio.to_thread(acquire_graph_token)  # one SSO prompt per run
        graph = build_graph(tpl, context, writer, reviewer, http, loaders, cfg.max_evidence_chars, out_dir)
        if args.graph:
            print(graph.get_graph().draw_mermaid())
            return
        await graph.ainvoke({"sections": {}, "evidence": {}, "errors": {}, "history": [], "round": 0},
                            {"recursion_limit": 100})


if __name__ == "__main__":
    asyncio.run(amain())
