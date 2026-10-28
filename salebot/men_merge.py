import json
import ast

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


def handle(data):
    params = _loads(data)
    if not isinstance(params, dict):
        params = {}
    current = _loads(params.get("current"))
    if not isinstance(current, dict):
        current = {}
    man = str(params.get("man", "")).strip()
    women = _to_list(params.get("women"))
    result = {str(k): v for k, v in current.items()}
    if man and women:
        result[man] = women
    return json.dumps(result, ensure_ascii=False)
