from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ParsedResult:
    parsed: dict[str, Any]
    truncated: bool
    method: str


_FENCE_RE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)


def _strip_code_fences(raw_text: str) -> str:
    """
    If the model wrapped output in ```json fences, extract the fenced body.
    Otherwise return the original string.
    """
    if not raw_text:
        return raw_text
    m = _FENCE_RE.search(raw_text)
    if m:
        return m.group(1).strip()
    return raw_text.strip()


def _find_balanced_end(s: str, start: int) -> int | None:
    """
    Find the index (inclusive) of the closing brace that balances the opening
    brace at s[start]. Returns None if not found (likely truncated).
    """
    if start < 0 or start >= len(s) or s[start] != "{":
        return None

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return i
            continue
    return None


def _scan_json_string(s: str, i: int) -> tuple[str, int] | None:
    """Scan a JSON string starting at index i (which must be a quote)."""
    if i >= len(s) or s[i] != '"':
        return None
    j = i + 1
    esc = False
    out = []
    while j < len(s):
        ch = s[j]
        if esc:
            out.append(ch)
            esc = False
            j += 1
            continue
        if ch == "\\":
            esc = True
            out.append(ch)
            j += 1
            continue
        if ch == '"':
            raw = '"' + "".join(out) + '"'
            try:
                return json.loads(raw), j + 1
            except Exception:
                # best-effort: return unescaped content
                return "".join(out).replace('\\"', '"'), j + 1
        out.append(ch)
        j += 1
    return None


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _scan_balanced(s: str, i: int) -> tuple[str, int, bool] | None:
    """
    Scan a balanced object/array starting at i.
    Returns (substring, next_index, truncated)
    """
    if i >= len(s) or s[i] not in "{[":
        return None
    open_ch = s[i]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == open_ch:
            depth += 1
            continue
        if ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[i : j + 1], j + 1, False
            continue
    # ran out of input before closing
    return s[i:], len(s), True


def _scan_primitive(s: str, i: int) -> tuple[Any, int] | None:
    """
    Scan primitive JSON values: true/false/null/number.
    """
    if s.startswith("true", i):
        return True, i + 4
    if s.startswith("false", i):
        return False, i + 5
    if s.startswith("null", i):
        return None, i + 4

    m = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", s[i:])
    if m:
        raw = m.group(0)
        try:
            val: Any = int(raw) if re.fullmatch(r"-?\d+", raw) else float(raw)
        except Exception:
            val = raw
        return val, i + len(raw)
    return None


def _parse_top_level_keypairs(s: str) -> ParsedResult:
    """
    Parse top-level JSON keypairs out of a possibly-truncated JSON object.

    This is NOT a full JSON parser. It aims to recover:
    - verdict (string)
    - kill_criteria (list of strings, if parseable)
    - other top-level keys as dict/list when balanced; otherwise raw snippet.
    """
    text = s
    start = text.find("{")
    if start < 0:
        return ParsedResult(parsed={}, truncated=False, method="no_object_found")

    parsed: dict[str, Any] = {}
    i = start + 1
    depth = 1
    in_str = False
    esc = False
    truncated_any = False

    while i < len(text) and depth > 0:
        i = _skip_ws(text, i)
        if i >= len(text):
            break
        ch = text[i]

        # Track depth outside strings to stay near top-level
        if ch == '"':
            # Potential key at top-level when depth==1
            if depth == 1:
                key_scan = _scan_json_string(text, i)
                if not key_scan:
                    i += 1
                    continue
                key, i2 = key_scan
                i = _skip_ws(text, i2)
                if i < len(text) and text[i] == ":":
                    i = _skip_ws(text, i + 1)
                    if i >= len(text):
                        break
                    # parse value
                    if i < len(text) and text[i] == '"':
                        vs = _scan_json_string(text, i)
                        if vs:
                            parsed[str(key)] = vs[0]
                            i = vs[1]
                            continue
                    if i < len(text) and text[i] in "{[":
                        bal = _scan_balanced(text, i)
                        if bal:
                            sub, nxt, trunc = bal
                            truncated_any = truncated_any or trunc
                            if not trunc:
                                try:
                                    parsed[str(key)] = json.loads(sub)
                                except Exception:
                                    parsed[str(key)] = sub
                            else:
                                parsed[str(key)] = sub
                            i = nxt
                            continue
                    prim = _scan_primitive(text, i)
                    if prim is not None:
                        parsed[str(key)] = prim[0]
                        i = prim[1]
                        continue
                    # unknown / unparseable: grab until comma or end brace
                    j = i
                    while j < len(text) and text[j] not in ",}":
                        j += 1
                    parsed[str(key)] = text[i:j].strip()
                    i = j
                    continue
            # Not a key position; fall through to generic string tracking
            in_str = True
            i += 1
            continue

        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1

    # Normalize verdict if present in some common variants
    v = parsed.get("verdict")
    if isinstance(v, str):
        vv = v.strip().upper()
        if vv in ("YES", "MAYBE", "NO"):
            parsed["verdict"] = vv

    # Normalize kill_criteria to a compact list when possible
    kc = parsed.get("kill_criteria")
    if isinstance(kc, str):
        # sometimes recovered as raw snippet; attempt to pull bullet-ish strings
        bullets = [x.strip(" \t-•") for x in re.split(r"[\r\n]+", kc) if x.strip()]
        if bullets:
            parsed["kill_criteria"] = bullets

    return ParsedResult(parsed=parsed, truncated=truncated_any or depth != 0, method="keypair_recovery")


def parse_raw_text_to_parsed(raw_text: str) -> ParsedResult:
    """
    Turn an Anthropic 'raw_text' blob into a structured dict of keypairs.

    Strategy:
    1) Extract fenced body if present.
    2) Try full JSON parsing by balancing braces.
    3) If truncated/unbalanced, fall back to top-level keypair recovery.
    """
    body = _strip_code_fences(raw_text or "")
    # locate first object
    start = body.find("{")
    if start >= 0:
        end = _find_balanced_end(body, start)
        if end is not None:
            try:
                obj = json.loads(body[start : end + 1])
                if isinstance(obj, dict):
                    return ParsedResult(parsed=obj, truncated=False, method="balanced_json_load")
            except Exception:
                pass
        # truncated or failed parse
        res = _parse_top_level_keypairs(body[start:])
        return res

    # No JSON object found: minimal heuristic extraction
    verdict_m = re.search(r"\bverdict\b\s*[:=-]\s*(YES|MAYBE|NO)\b", body, flags=re.IGNORECASE)
    parsed: dict[str, Any] = {}
    if verdict_m:
        parsed["verdict"] = verdict_m.group(1).upper()
    return ParsedResult(parsed=parsed, truncated=False, method="minimal_heuristics")

