"""Extract translatable strings without GNU gettext (not always installed).

Finds _("..."), _l("..."), gettext("...") in Python and {% trans %} /
{% blocktrans %} in templates, and returns msgids exactly as Django's runtime
looks them up. Used by build_translations and by the translation test."""
import re
from pathlib import Path

from django.core.management.base import BaseCommand

ROOT = Path(__file__).resolve().parents[2]

PY_RE = re.compile(r"""(?<![\w.])(?:_|_l|gettext|gettext_lazy)\(\s*((?:"(?:[^"\\]|\\.)*"\s*|'(?:[^'\\]|\\.)*'\s*)+)""",
                   re.S)
STR_RE = re.compile(r""""((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'""")
TRANS_RE = re.compile(r"""{%\s*trans\s+"((?:[^"\\]|\\.)*)"\s*%}""")
BLOCK_RE = re.compile(r"{%\s*blocktrans(?P<args>[^%]*)%}(?P<body>.*?)(?:{%\s*plural\s*%}(?P<plural>.*?))?{%\s*endblocktrans\s*%}",
                      re.S)
VAR_RE = re.compile(r"{{\s*(\w+)\s*}}")


def _unescape(s):
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")


def _block_msg(text):
    return VAR_RE.sub(lambda m: f"%({m.group(1)})s", text.replace("%", "%%"))


def extract():
    singles, plurals = set(), {}
    for path in (ROOT).rglob("*.py"):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        for m in PY_RE.finditer(src):
            parts = [a or b for a, b in STR_RE.findall(m.group(1))]
            singles.add(_unescape("".join(parts)))
    for path in (ROOT / "templates").rglob("*.html"):
        src = path.read_text(encoding="utf-8")
        for m in TRANS_RE.finditer(src):
            singles.add(m.group(1))
        for m in BLOCK_RE.finditer(src):
            body = _block_msg(m.group("body"))
            if m.group("plural"):
                plurals[body] = _block_msg(m.group("plural"))
            else:
                singles.add(body)
    singles |= EXTRA
    singles.discard("")
    return singles, plurals


# Messages raised by helpers and translated at the call site with _(str(exc)).
EXTRA = {
    "Enter a 9-digit Cameroon mobile number starting with 6 (e.g. 699 12 34 56)",
    "Amounts must be whole FCFA", "Invalid amount", "Amount too large",
    "Floating-point money values are not allowed",
}


class Command(BaseCommand):
    help = "List translatable messages (debug helper)."

    def handle(self, *args, **opts):
        s, p = extract()
        for m in sorted(s):
            self.stdout.write(repr(m))
        self.stdout.write(f"{len(s)} singular, {len(p)} plural")
