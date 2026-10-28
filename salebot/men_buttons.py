import json
import ast

# In Love bot, Salebot helper: buttons with men for /mailing_select.
# Input (JSON or Python literal):
#   {"names": ["Diego gonzalez serna", ...], "pids": ["1058923342", ...], "ages": ["61", ...]}
#   or the same values as strings joined with "|": {"names": "Diego|Jonathan", "pids": "105|170"}
# names, pids and ages go in the same order (as returned by the men table).
# Output: JSON list of inline buttons, one man per line.
# Each man button sends callback "<pid>_mansel_clbk".
# Last line: cancel button with callback "mansel_cancel_clbk".


def _loads(data):
    if not isinstance(data, str):
        return data
    s = data.strip()
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
        return val
    if isinstance(val, (int, float)):
        return [str(val)]
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(s)
                if isinstance(parsed, list):
                    return parsed
                return [str(parsed)]
            except Exception:
                pass
        # Salebot passes table columns joined with "|"
        return [x.strip() for x in s.split("|")]
    return []


def handle(data):
    params = _loads(data)
    if not isinstance(params, dict):
        params = {}
    names = [str(x).strip() for x in _to_list(params.get("names"))]
    pids = [str(x).strip() for x in _to_list(params.get("pids"))]
    ages = [str(x).strip() for x in _to_list(params.get("ages"))]

    buttons = []
    line = 0
    for i, pid in enumerate(pids):
        if not pid or pid in ("None", "0"):
            continue
        name = names[i] if i < len(names) and names[i] else "Без имени"
        age = ages[i] if i < len(ages) and ages[i] not in ("", "None") else ""
        text = "{}, {}".format(name, age) if age else name
        buttons.append({
            "line": line,
            "index_in_line": 0,
            "text": text[:60],
            "type": "inline",
            "callback": "{}_mansel_clbk".format(pid),
        })
        line += 1

    buttons.append({
        "line": line,
        "index_in_line": 0,
        "text": "❌ Отменить",
        "type": "inline",
        "callback": "mansel_cancel_clbk",
    })
    return json.dumps(buttons, ensure_ascii=False, separators=(",", ":"))
