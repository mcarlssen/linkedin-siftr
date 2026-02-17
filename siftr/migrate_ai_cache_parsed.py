from __future__ import annotations

import argparse
import json
from pathlib import Path

from siftr.raw_text_parser import parse_raw_text_to_parsed


def migrate_dir(dir_path: Path) -> tuple[int, int]:
    updated = 0
    total = 0

    for p in sorted(dir_path.glob("*.json")):
        total += 1
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        out = doc.get("output")
        if not isinstance(out, dict):
            continue

        raw_text = out.get("raw_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue

        # Build parsed object from raw_text
        res = parse_raw_text_to_parsed(raw_text)
        out["parsed"] = {
            "_meta": {
                "method": res.method,
                "truncated": bool(res.truncated),
            },
            **res.parsed,
        }

        # Remove older readability helper if present
        out.pop("raw_excerpt", None)

        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        updated += 1

    return updated, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dir",
        default="out/cache/ai",
        help="Directory containing AI cache JSON files (default: out/cache/ai)",
    )
    args = ap.parse_args()

    d = Path(args.dir)
    if not d.exists() or not d.is_dir():
        raise SystemExit(f"Not a directory: {d}")

    updated, total = migrate_dir(d)
    print(f"Updated {updated} / {total} cache files in {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

