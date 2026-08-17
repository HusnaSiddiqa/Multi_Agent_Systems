"""Renders app.yaml (real, gitignored) from app.yaml.example + system_files/org_config.local.yaml.

Run before deploying:
    python scripts/render_app_yaml.py
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "app.yaml.example"
CONFIG_PATH = ROOT / "system_files" / "org_config.local.yaml"
OUTPUT_PATH = ROOT / "app.yaml"

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def main():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Missing {CONFIG_PATH}. Copy system_files/org_config.example.yaml to "
            "org_config.local.yaml and fill in real values first."
        )

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def replace(match):
        key = match.group(1)
        if key not in config:
            raise SystemExit(f"org_config.local.yaml is missing key: {key}")
        return str(config[key])

    rendered = PLACEHOLDER_RE.sub(replace, template)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
