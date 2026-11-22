"""Split bilingual 'Arabic / English' template strings into Arabic-only.

For every EN catalog key matching 'AR / EN' pattern:
  - replace the bilingual literal with the Arabic part in all templates
  - rekey EN and ZH catalog entries to the Arabic-only source
Prints collisions for review. Rewrites app/i18n.py catalog blocks in place.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.i18n import EN, ZH  # noqa: E402

PAT = re.compile(r"^(?P<ar>[^/]*[؀-ۿ][^/]*?)\s+/\s+(?P<en>[ -~]+)$")

pairs = {}
for key in EN:
    m = PAT.match(key)
    if m:
        pairs[key] = m.group("ar").strip()

collisions = []
for key, ar in sorted(pairs.items()):
    if ar in EN and ar not in pairs.values():
        collisions.append((key, ar, EN[ar], EN[key]))

# 1) templates: longest keys first to avoid partial-overlap replacement
tpl_dir = ROOT / "app" / "templates"
changed_tpl = {}
for tpl in sorted(tpl_dir.glob("*.html")):
    src = tpl.read_text(encoding="utf-8")
    orig = src
    for key, ar in sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True):
        src = src.replace(key, ar)
    if src != orig:
        tpl.write_text(src, encoding="utf-8")
        changed_tpl[tpl.name] = sum(1 for k in pairs if k in orig)

# 2) catalogs: textual rekey inside app/i18n.py
i18n_path = ROOT / "app" / "i18n.py"
text = i18n_path.read_text(encoding="utf-8")


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


rekeyed = 0
for key, ar in sorted(pairs.items()):
    if ar in EN and ar not in pairs:
        continue  # collision: keep existing Arabic-only entry
    old = '"' + esc(key) + '"'
    new = '"' + esc(ar) + '"'
    # rekey both catalog blocks (EN and ZH) textually
    count = text.count(old + ":")
    if count:
        text = text.replace(old + ":", new + ":")
        rekeyed += count

i18n_path.write_text(text, encoding="utf-8")

print(f"bilingual keys: {len(pairs)}")
print(f"templates changed: {changed_tpl}")
print(f"catalog rekeys: {rekeyed}")
print(f"collisions kept as-is: {len(collisions)}")
for c in collisions:
    print("  COLLISION:", c)
