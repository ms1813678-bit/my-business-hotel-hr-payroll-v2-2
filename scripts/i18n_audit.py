"""Static i18n coverage audit for templates.

Extracts visible text nodes and translated attributes from every template,
classifies them by script, and checks catalog coverage in EN and ZH.
Read-only: prints a report, changes nothing.
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.i18n import EN, ZH  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

AR = re.compile(r"[؀-ۿ]")
LAT = re.compile(r"[A-Za-z]")
CJK = re.compile(r"[一-鿿]")
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
SKIP_TAGS = {"script", "style", "textarea", "pre", "code"}
ATTRS = {"placeholder", "title", "aria-label"}


class Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = []
        self.items = []  # (kind, raw_text)

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip.append(tag)
        for name, value in attrs:
            if value and name.lower() in ATTRS:
                self.items.append(("attr", value))

    def handle_startendtag(self, tag, attrs):
        for name, value in attrs:
            if value and name.lower() in ATTRS:
                self.items.append(("attr", value))

    def handle_endtag(self, tag):
        if self.skip and self.skip[-1] == tag:
            self.skip.pop()

    def handle_data(self, data):
        if not self.skip:
            self.items.append(("text", data))


def classify(s):
    return AR.search(s) is not None, LAT.search(s) is not None, CJK.search(s) is not None


def main():
    missing_en = {}
    missing_zh = {}
    bilingual = {}
    latin_only = {}
    jinja_merged = {}
    for tpl in sorted(TEMPLATES.glob("*.html")):
        source = tpl.read_text(encoding="utf-8")
        # Remove Jinja comments; turn block tags into element boundaries so
        # if/elif/endif branches stay separate text nodes (as at runtime).
        source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
        # Drop Jinja blocks inside tags first (attribute conditionals), then
        # turn remaining text-level blocks into element boundaries.
        source = re.sub(r"<[^>]*>", lambda m: re.sub(r"\{%.*?%\}", "", m.group(0)), source, flags=re.S)
        source = re.sub(r"\{%.*?%\}", "\n<br>\n", source, flags=re.S)
        source = re.sub(r"\{\{.*?\}\}", "\x00VAR\x00", source, flags=re.S)
        ex = Extractor()
        ex.feed(source)
        for kind, raw in ex.items:
            core = raw.strip()
            if not core:
                continue
            # strip variable markers for classification, remember if merged
            no_jinja = core.replace("\x00VAR\x00", "").strip()
            has_jinja = "\x00VAR\x00" in core
            core = core.replace("\x00VAR\x00", "…")
            if not no_jinja:
                continue
            if no_jinja in {"—", "-", "·", "•", "/", "(", ")", ",", ".", ":", ".."}:
                continue
            ar, lat, cjk = classify(no_jinja)
            if ar:
                if has_jinja:
                    jinja_merged.setdefault(core, set()).add(tpl.name)
                if lat or cjk:
                    bilingual.setdefault(core, set()).add(tpl.name)
                if core not in EN:
                    missing_en.setdefault(core, set()).add(tpl.name)
                if core not in ZH:
                    missing_zh.setdefault(core, set()).add(tpl.name)
            elif lat and not has_jinja:
                latin_only.setdefault(core, set()).add(tpl.name)
            elif cjk and not has_jinja:
                latin_only.setdefault(core, set()).add(tpl.name)

    def dump(title, mapping, show_files=True, limit=400):
        print(f"\n===== {title} ({len(mapping)}) =====")
        for i, (text, files) in enumerate(sorted(mapping.items())):
            if i >= limit:
                print(f"... +{len(mapping) - limit} more")
                break
            where = ",".join(sorted(files))
            print(f"- {text!r}  [{where}]")

    dump("ARABIC SOURCE MISSING EN", missing_en)
    dump("ARABIC SOURCE MISSING ZH", missing_zh)
    dump("BILINGUAL MIXED STRINGS (AR+LAT/CJK in one node)", bilingual)
    dump("ARABIC MERGED WITH JINJA (won't exact-match)", jinja_merged)
    dump("LATIN/CJK-ONLY STRINGS (rendered as-is in AR UI)", latin_only)


if __name__ == "__main__":
    main()
