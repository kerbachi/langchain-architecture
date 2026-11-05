# docgen

Generates a design document from a template. Several LLM agents write the sections (in parallel
where possible), then a final review checks the sections against each other and against the
requirements and scope agreed with the requesters.

## Files

| File | What it contains | Who edits it |
|---|---|---|
| `component_standard.yaml` | **Template**: the document title, the section titles in order, and the instructions (prompts) for each section | You, once per document type |
| `s3_files_aegon_spain.md` | **Requirements and scope** for the document, as expressed and shared with the requesters | You, one file per request |
| `config.yaml` | **LLM settings**: which models write and review | You, once per environment |
| `docgen.py` | The engine. Contains no request-specific text | Nobody, normally |
| `out/<requirements file name>/document.md` | **The final document** | Generated |

## Quick start

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

    python docgen.py -t component_standard.yaml -r s3_files_aegon_spain.md --dry-run   # test, no LLM needed
    python docgen.py -t component_standard.yaml -r s3_files_aegon_spain.md             # real run

`-t` is the template, `-r` the requirements and scope file, `-c` the LLM settings (default `config.yaml`).
Add `--graph` to print the processing graph instead of running it.

## 1. Template: titles and prompts (`component_standard.yaml`)

Each entry under `sections` is one section of the final document and one agent.
The order of the list is the order of the document.

```yaml
document_title: "Component standard request: {{component}}"
numbering: true

sections:
  - id: drivers_motivation                       # internal name, never printed
    title: "Overview: Drivers and Motivation"    # printed exactly as written
    directives: |                                # the prompt for this section's agent
      Describe what the component is.
      Describe the motivation for using the component.
    guidelines: |                                # optional, extra instructions for this section only
      - Keep it under 400 words.
    sources:                                     # optional, extra material this agent reads
      - type: url
        urls: "{{reference_urls}}"
    depends_on: []                               # optional, sections that must be written first
```

| Field | Effect |
|---|---|
| `document_title` | Main title of the document |
| `numbering` | `true` prints "1. Title", "2. Title" ...; `false` prints titles exactly as written |
| `writer_guidelines` | Instructions applied to every section (audience, tone, length) |
| `title` | Section heading, printed exactly as written |
| `directives` | What the agent must write; one item per line, each item is checked in the final review |
| `guidelines` | Extra instructions for this section only |
| `sources` | Extra material for this agent: `url`, `file`, `web`, `m365` (see below) |
| `depends_on` | Changes **when** a section is written, never **where** it appears |
| `table_columns` | Makes the section a table with exactly these columns, e.g. `["Title", "Description"]`. The agent supplies the rows; the script draws the table |
| `key_facts` | Facts every agent must state the same way; differences are flagged in the review |
| `max_revision_rounds` | How many times the final review may send sections back for correction |

`{{component}}` and other `{{...}}` values are filled from the top block of the requirements and scope file.

## 2. Requirements and scope (`s3_files_aegon_spain.md`)

This file holds the requirements and scope for the document, as expressed and shared with the
requesters. `docgen.py` reads it once at the start and splits it in two:

- **The top block** (between the `---` lines) is never sent to an agent. It only fills the
  `{{...}}` values in the template: the document title, the pages and files agents should read.
- **The text below it** is sent, in full, to every section agent, and sent again when a section is
  rewritten. The final reviewer receives its first 20,000 characters and checks the whole document
  against it. To keep it from one section, add `use_request_context: false` to that section.

```markdown
---
component: "Amazon S3 Files"         # fills {{component}} in the template
reference_urls:                      # fills {{reference_urls}}: pages the agents may read
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.md
attachments: []                      # fills {{attachments}}: local files (PDF, DOCX, MD ...)
policy_documents: []                 # fills {{policy_documents}}: corporate policy and governance files
---
Paste the requesters' text here, as it was shared.
```

This file usually contains internal information (names, business units, plans).
Do not commit real ones to a public repository.

## 3. Output (`out/<requirements file name>/`)

| File | Use |
|---|---|
| `document.md` | **The deliverable.** Template titles, in template order, with the agents' text |
| `document_annotated.md` | Same text with source references under each section, to check where statements come from |
| `reconciliation_report.md` | Result of the final review: conflicts, gaps, open questions, revisions made |
| `run.json` | Technical record of the run, for audit |

## How each section is written

For each section, `docgen.py` builds the prompt automatically from:

1. Fixed role instructions (`WRITER_SYSTEM` in `docgen.py`)
2. The document title and the list of all section titles
3. The requirements and scope file
4. The section's `title`, `directives` and guidelines
5. The content of the section's `sources`
6. Already written sections listed in `depends_on`

The agent returns only the body text. The script adds the heading, so titles never change.
For a section with `table_columns`, the agent returns the rows as data and the script draws the
table, so the columns are always exactly the ones in the template.

## Who does the orchestration and consolidation?

There is no "manager" AI deciding who runs when. **LangGraph** runs a plan that `docgen.py` builds
from the template (`build_graph` in `docgen.py`).

| Job | Done by | AI involved? |
|---|---|---|
| Deciding which sections run, in what order, and which run at the same time | LangGraph, from the order of `sections` and each `depends_on` | No |
| Collecting the results of agents that ran in parallel | LangGraph's shared state: each agent files its text under its own section `id` | No |
| Checking that sections agree with each other and with the requirements and scope | The final review: automatic checks, then the `reviewer` model from `config.yaml` | Yes, the reviewer |
| Deciding which sections must be corrected | The reviewer names them; the script sends them back, up to `max_revision_rounds` | Partly |
| Building the final document | The script puts each section's text under its template title, in template order | No |

With the current template, a run goes like this:

1. **Purpose**, **Overview: Drivers and Motivation** and **Business Value** start at the same time
   (no `depends_on`).
2. **Use Cases** starts as soon as the Overview is finished, and **Component Summary** as soon as
   Purpose, Overview and Business Value are finished. Both receive the finished text they depend on.
3. **The final review** runs once, after every section is finished.
4. If problems are found, only the flagged sections are rewritten, the sections that depend on them
   are refreshed, and the review runs again.
5. `document.md` is written.

Agents running at the same time never see each other's text. Each one receives the list of all
section titles, so it knows what the others cover. Only sections listed in `depends_on` pass their
text forward. `max_concurrency` in `config.yaml` limits how many agents call the same model at once;
extra agents wait for a free slot.

## Final review

1. Automatic checks: facts stated differently by two sections, directives not covered,
   statements without a source, numbers that appear in no source.
2. A reviewer LLM (instructions in `REVIEWER_SYSTEM` in `docgen.py`) compares all sections with
   each other and with the requirements and scope.
3. Sections with problems are rewritten, sections that depend on them are refreshed, and the
   review runs again, up to `max_revision_rounds`. Anything still unresolved is listed in
   `reconciliation_report.md` for a human decision.

## LLM settings (`config.yaml`)

`writer` is used for the sections, `reviewer` for the final review. Any OpenAI-compatible server works.

| Key | Meaning |
|---|---|
| `base_url` | Ollama `http://localhost:11434/v1`, vLLM `:8000/v1`, LM Studio `:1234/v1`, `https://openrouter.ai/api/v1`, `https://api.openai.com/v1` |
| `model` | Must match an id returned by `<base_url>/models`; the run warns at start if not |
| `api_key_env` | Name of the environment variable holding the API key (never the key itself) |
| `json_mode` | `false` plain text, `true` JSON mode, `schema` enforced JSON schema (LM Studio, vLLM, OpenAI) |
| `max_tokens` | Maximum output length; set high enough for a full section |
| `timeout_s` | Seconds per request; raise it for large local models |
| `max_concurrency` | How many agents may call this model at the same time |
| `extra_body` | Server-specific options, e.g. vLLM `{chat_template_kwargs: {enable_thinking: false}}` |

## Source types

| `type` | Reads | Needs |
|---|---|---|
| `url` | Web pages listed in `urls` | Nothing |
| `file` | Local files listed in `paths` | `pip install docling` for PDF, DOCX, PPTX |
| `web` | Web search results for `query` | A SearXNG server in `SEARXNG_URL` |
| `m365` | SharePoint/OneDrive through the Microsoft 365 Copilot Retrieval API | Copilot licence; `M365_CLIENT_ID` and `M365_TENANT_ID` (Entra app, delegated Files.Read.All and Sites.Read.All), or a token in `GRAPH_TOKEN` |

## Reusing it

| You want to... | Change |
|---|---|
| Same document type, new request | Create a new requirements and scope file |
| Different titles, order or instructions | Edit `sections` in a copy of the template |
| Write a section after others | Add `depends_on` to it (its position does not change) |
| Give a section more material | Add `sources` to it |
| Use other models | Edit `config.yaml` |

## Test

    pip install pytest && pytest -q

Runs the pipeline offline with a sample requirements file and checks that the document follows the template.
