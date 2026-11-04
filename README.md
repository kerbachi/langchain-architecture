# docgen - outline-driven multi-agent document generator (LangGraph)

    docgen.py                  engine (generic, no request-specific text)
    component_standard.yaml    reusable template: title, outline (exact headings in order), directives, sources
    <request>.md               one request: front-matter variables + global context
    config.yaml                environment: LLM endpoints, output folder
    tests/test_dry_run.py      smoke test (no LLM needed)

## Commands

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python docgen.py -t component_standard.yaml -r my_request.md --graph     # show the LangGraph
    python docgen.py -t component_standard.yaml -r my_request.md --dry-run   # offline test
    python docgen.py -t component_standard.yaml -r my_request.md             # real run
    pip install pytest && pytest -q                                          # smoke test

Results in `out/<request name>/`:

- `document.md`: the deliverable. Template headings only, in template order, no citation markers.
- `document_annotated.md`: same content with [R1]/[E1] citations and the sources of each section.
- `reconciliation_report.md`: conflicts, gaps, overlaps, automated findings, revision history.
- `run.json`: full state, including a content hash of every source, for audit.

## The outline

The `sections` list in the template is the document outline. Its order is the document order,
each `title` is printed exactly as written (numbered when `numbering: true`), and agents write
only the body under it; any heading a model writes is turned into bold text.
`depends_on` changes when an agent runs, never where its section appears. When the final review
revises a section, every section that depends on it is re-run before the next review.

## Request file

    ---
    component: "Amazon S3 Files"            # used as {{component}} in the template
    reference_urls: [https://...]           # used as {{reference_urls}}
    attachments: []                         # used as {{attachments}}
    ---
    Paste the request text here. Every agent receives it as [R1].

## Reusing it

| You want to...                               | Change                                                        |
|----------------------------------------------|---------------------------------------------------------------|
| Same outline, different component/request    | New request `.md` file                                        |
| Different headings, order or directives      | Copy the template, edit `sections` (order = document order)   |
| Run an agent after others                    | Add `depends_on: [ids]` to it (its position does not change)  |
| Ground an agent in more sources              | Add `sources` (file, url, web, m365) to that section          |
| Different models / providers                 | `config.yaml` (any OpenAI-compatible `/v1` endpoint)          |

## LLM settings (per role in config.yaml)

| Key           | Meaning                                                                          |
|---------------|----------------------------------------------------------------------------------|
| base_url      | Ollama `:11434/v1`, vLLM `:8000/v1`, LM Studio `:1234/v1`, OpenRouter, OpenAI     |
| model         | Must match an id from `GET <base_url>/models`; the run warns at start if not      |
| json_mode     | `false` text + tolerant parsing, `true` json_object, `schema` enforced JSON schema |
| max_tokens    | Output cap; set it high enough for a full section in JSON                        |
| extra_body    | Server-specific options, e.g. vLLM `{chat_template_kwargs: {enable_thinking: false}}` |
| timeout_s     | Per request; raise for large local models                                        |

## Source types

`file` (PDF/DOCX/PPTX need `pip install docling`), `url`, `web` (needs `SEARXNG_URL`),
`m365` (M365 Copilot Retrieval API; needs `M365_CLIENT_ID` and `M365_TENANT_ID` for an Entra app
with public client flow and delegated Files.Read.All + Sites.Read.All, or a token in `GRAPH_TOKEN`).

## Final reconciliation

Automated checks (key-fact mismatches, directives not covered, claims without valid citations,
figures not found in any source), then the reviewer LLM (conflicts, gaps vs. the request, overlaps),
then targeted revision of flagged sections and refresh of their dependents, up to `max_revision_rounds`.
