"""Smoke test: the dry run must produce the template's exact outline, in order, with no citation markers."""
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "component_standard.yaml"


def test_dry_run_follows_outline(tmp_path):
    request = tmp_path / "request.md"
    request.write_text('---\ncomponent: "Demo Component"\n---\nSample request text.\n', encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(ROOT / "docgen.py"), "-t", str(TEMPLATE), "-r", str(request),
                    "-o", str(out), "--dry-run"], check=True, cwd=ROOT)

    tpl = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    doc = (out / "document.md").read_text(encoding="utf-8")
    assert doc.splitlines()[0] == "# Component standard request: Demo Component"
    headings = [line[3:] for line in doc.splitlines() if line.startswith("## ")]
    assert headings == [f"{i}. {s['title']}" for i, s in enumerate(tpl["sections"], 1)]
    assert "[R1]" not in doc and "###" not in doc
    use_cases = doc.split("## ")[[h.split(". ", 1)[-1] for h in headings].index("Use Cases") + 1]
    assert "| Title | Description |" in use_cases and use_cases.count("\n| ") >= 3  # header + rows
    assert "**Status:** PASS" in (out / "reconciliation_report.md").read_text(encoding="utf-8")
