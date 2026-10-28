import json
import ast
import re

# In Love bot, Salebot helper: adds one man to the mailing map without
# erasing the other men's lists (so their "next profile" buttons keep working).
# Input: {"current": <project.man_woman_result_newsletter>, "man": "<pid>", "women": ["<pid>", ...]}
# Output: JSON dict {"<man pid>": ["<woman pid>", ...], ...}


def _loads(data):
    if not isinstance(data, str):
        return data
    s = data.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return ast.literal_eval(s)
    except Exception:
        return None


def _to_list(val):
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, (int, float)):
        return [str(val)]
    parsed = _loads(val) if isinstance(val, str) else None
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def _fallback(data):
    # Salebot can render the map in a way that breaks parsing of the whole
    # string; then pull the fields out one by one.
    out = {}
    if not isinstance(data, str):
        return out
    m = re.search(r'"man"\s*:\s*"?([0-9]+)"?', data)
    if m:
        out["man"] = m.group(1)
    w = re.search(r'"women"\s*:\s*(\[[^\]]*\])', data)
    if w:
        out["women"] = w.group(1)
    c = re.search(r'"current"\s*:\s*(\{.*\})\s*,\s*"man"', data, re.S)
    if c:
        out["current"] = c.group(1)
    return out


def handle(data):
    params = _loads(data)
    if not isinstance(params, dict):
        params = _fallback(data)
    current = _loads(params.get("current"))
    if not isinstance(current, dict):
        current = {}
    man = str(params.get("man", "")).strip()
    women = _to_list(params.get("women"))
    result = {str(k): v for k, v in current.items()}
    if man and women:
        result[man] = women
    return json.dumps(result, ensure_ascii=False)
