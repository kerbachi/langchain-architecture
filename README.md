# docgen - template-driven multi-agent document generator (LangGraph)

    docgen/
      docgen.py                         engine (generic, no request-specific text)
      config.yaml                       per environment: LLM endpoints, output folder
      templates/component_standard.yaml reusable: purpose, agents, questions, sources, checks
      requests/s3_files_aegon_spain.md  per request: front-matter variables + global context

## Commands

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    T=templates/component_standard.yaml
    R=requests/s3_files_aegon_spain.md
    python docgen.py -t $T -r $R --graph      # show the LangGraph (Mermaid)
    python docgen.py -t $T -r $R --dry-run    # offline test of the orchestration
    python docgen.py -t $T -r $R              # real run (uses config.yaml)

Results: `out/<request name>/document.md`, `sections/*.md`, `reconciliation_report.md`, `run.json`.

## Reusing it

| You want to...                               | Change                                                        |
|----------------------------------------------|---------------------------------------------------------------|
| Same questions, different component/request  | New file in `requests/` (front matter + pasted request text)  |
| Different questions or agents                | Copy the template, edit `sections`                            |
| Run an agent after others                    | Add `depends_on: [ids]` to it                                 |
| Ground an agent in more sources              | Add `sources` (file, url, web, m365) to that agent            |
| Different models / providers                 | `config.yaml` (any OpenAI-compatible `/v1` endpoint)          |

Template text can use `{{variable}}` from the request front matter, template `defaults`, or `{{today}}`.
A value written exactly as `"{{reference_urls}}"` keeps its list form.

## LLM endpoints

| Provider    | base_url                      | api_key_env        |
|-------------|-------------------------------|--------------------|
| Ollama      | http://localhost:11434/v1     | -                  |
| vLLM        | http://localhost:8000/v1      | optional           |
| LM Studio   | http://localhost:1234/v1      | -                  |
| OpenRouter  | https://openrouter.ai/api/v1  | OPENROUTER_API_KEY |
| OpenAI      | https://api.openai.com/v1     | OPENAI_API_KEY     |

## Source types

`file` (PDF/DOCX/PPTX need `pip install docling`), `url`, `web` (needs `SEARXNG_URL`),
`m365` (M365 Copilot Retrieval API; needs `M365_CLIENT_ID` and `M365_TENANT_ID` for an Entra app
with public client flow and delegated Files.Read.All + Sites.Read.All, or a token in `GRAPH_TOKEN`).
The request text is always given to every agent as `[R1]` unless `use_request_context: false`.

## Final reconciliation

Automated checks (key-fact mismatches, unanswered questions, claims without valid citations,
figures not found in any source), then the reviewer LLM (conflicts, gaps vs. the request, overlaps),
then targeted revision of flagged sections, up to `max_revision_rounds`.
