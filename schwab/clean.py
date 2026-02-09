import json, ast, sys

raw = open("schwab/raw.txt", "r", encoding="utf-8").read().strip()

# If it's a JSON string literal or otherwise escaped, unescape once.
# Wrap in quotes only if it isn't already.
# if not (raw.startswith('"') and raw.endswith('"')):
#     raw = '"' + raw.replace('\\', '\\\\').replace('"', '\\"') + '"'

s = json.loads(raw)          # turns \" and \r\n into real " and newlines
obj = json.loads(s["specification"])         # parse the resulting JSON text into a dict

print("top-level keys:", list(obj)[:10])
open("schwab/spec.json", "w", encoding="utf-8").write(
    json.dumps(obj, indent=2)
)
print("wrote spec.json")