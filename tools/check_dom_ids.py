#!/usr/bin/env python3
"""Every $('id') in app.js must exist in index.html.

A missing element makes $() return null, and the first property access throws,
killing the whole render with a blank page. That happened once — pause/resume
shipped referencing #controls before the element existed — and it was invisible
because nothing surfaced the exception.
"""
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def main() -> int:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    referenced = set(re.findall(r"""\$\(\s*['"]([\w-]+)['"]\s*\)""", js))
    # ids created dynamically by the app itself are legitimate
    created = set(re.findall(r"""id=["']([\w-]+)["']""", js))
    present = set(re.findall(r"""\bid=["']([\w-]+)["']""", html))

    missing = sorted(referenced - present - created)
    if missing:
        print("Missing from index.html (and not created by app.js):")
        for name in missing:
            print(f"  #{name}")
        return 1
    print(f"ok: {len(referenced)} referenced ids all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
