"""Static consistency checks for the single-file frontend.

`index.html` is one 7,000-line file, and the two failure modes that quietly
break it are (a) JS that reaches for an element id that no longer exists — the
handler then does nothing and the button looks dead — and (b) an inline
`onclick="fn(...)"` naming a function that was renamed or deleted. Both are
invisible to the Python tests, so check them here instead.
"""

import re
import unittest
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "index.html"

# getElementById('page-' + t) builds its id at runtime; only a literal with
# nothing after the quote is a real, statically checkable reference.
STATIC_ID_LOOKUP = re.compile(r"""getElementById\(\s*(['"])([\w-]+)\1\s*\)""")
MARKUP_ID = re.compile(r'\bid="([\w-]+)"')
INLINE_HANDLER = re.compile(
    r'''\bon(?:click|change|input|keydown|keyup|submit|focus|blur|paste|wheel|dblclick)\s*=\s*"([^"]*)"''',
    re.IGNORECASE,
)
FUNCTION_DEF = re.compile(r"""function\s+([A-Za-z_$][\w$]*)\s*\(""")
# Provided by the browser, or by the Tauri bridge, not by this file.
BROWSER_GLOBALS = {
    "alert", "confirm", "prompt", "event", "if", "return", "typeof", "new",
    "function", "void", "delete", "in", "of", "instanceof", "await", "do",
    "else", "switch", "catch", "throw", "case", "encodeURIComponent",
    "decodeURIComponent", "parseInt", "parseFloat", "isNaN", "JSON", "Math",
    "Date", "String", "Number", "Boolean", "Array", "Object", "Set", "Map",
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
}


def markup_without_scripts(html):
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


class FrontendConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.markup = markup_without_scripts(cls.html)
        cls.ids = set(MARKUP_ID.findall(cls.markup))

    def test_every_static_element_id_lookup_exists(self):
        missing = sorted(
            ref for ref in STATIC_ID_LOOKUP.findall(self.html)
            if ref[1] not in self.ids
        )
        self.assertEqual(
            missing, [],
            "getElementById() targets that no element defines: "
            + ", ".join(ref[1] for ref in missing),
        )

    def test_inline_handlers_reference_defined_functions(self):
        defined = set(FUNCTION_DEF.findall(self.html))
        undefined = set()
        for body in INLINE_HANDLER.findall(self.html):
            # Drop string literals ('Best (Max Quality)' is a value, not a call)
            # and only look at bare identifiers — never `foo.bar(`, which is a
            # method on an object rather than a page function.
            body = re.sub(r"'[^']*'", "''", body)
            for name in re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", body):
                if name not in defined and name not in BROWSER_GLOBALS:
                    undefined.add(name)
        self.assertEqual(
            undefined, set(),
            f"inline handlers call functions that are never defined: {sorted(undefined)}",
        )

    def test_ids_are_unique_in_the_markup(self):
        ids = MARKUP_ID.findall(self.markup)
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(duplicated, [], f"duplicate id attributes: {duplicated}")

    def has_tag_with(self, tag, *attributes):
        """True when some <tag> carries all of these attributes, whatever order
        or line breaks the markup uses."""
        for open_tag in re.findall(r"<" + tag + r"\b[^>]*>", self.markup, re.DOTALL):
            if all(attr in open_tag for attr in attributes):
                return True
        return False

    def test_tab_strips_are_real_tablists(self):
        """The animated tabs depend on this structure staying intact."""
        for strip_id, tab_id, panel_id in (
            ("topnav", 'id="tab-dl"', 'id="page-dl"'),
            ("hist-tabs", 'id="htab-files"', 'id="hist-list"'),
        ):
            with self.subTest(strip=strip_id):
                self.assertTrue(f'id="{strip_id}"' in self.markup, f"missing #{strip_id}")
                self.assertTrue(
                    self.has_tag_with("button", tab_id, 'role="tab"'),
                    f"{tab_id} is not a role=tab button",
                )
                self.assertTrue(panel_id in self.markup, f"missing {panel_id}")
        for indicator in ("tab-indicator", "hist-indicator", "topnav-hover", "hist-hover"):
            self.assertTrue(f'id="{indicator}"' in self.markup, f"{indicator} is missing")
        # Each sliding indicator is absolutely positioned against its strip, so
        # the strip has to stay the positioning parent or the pill flies off.
        for strip in ("topnav", "hist-tabs"):
            blocks = re.findall(r"\." + re.escape(strip) + r"\s*\{[^}]*\}", self.html, re.DOTALL)
            self.assertTrue(
                any("position: relative" in block for block in blocks),
                f".{strip} is not a positioning parent for its indicator",
            )

    def test_every_icon_reference_resolves(self):
        """A `<use href="#i-…">` into a missing symbol renders *nothing*, in silence.

        That is the worst possible failure for an icon system: the button keeps its
        padding and its label and looks intentional. So every name that can reach
        the sprite has to have a symbol behind it.
        """
        symbols = set(re.findall(r'<symbol id="(i-[\w-]+)"', self.html))
        self.assertTrue(symbols, "the icon sprite disappeared")

        refs = set()
        # literal markup, and the two helper call shapes
        refs |= set(re.findall(r'<use href="#i-([\w-]+)"', self.html))
        refs |= set(re.findall(r"\bicon\(\s*['\"]([\w-]+)['\"]", self.html))
        # SITES[].icon feeds siteIcon(), which builds `#i-<name>`
        refs |= set(re.findall(r"\bicon:\s*['\"]([\w-]+)['\"]", self.html))
        # the toast/notification kind tables map a kind to an icon name; read only
        # their bodies, or every `key: 'value'` in the file looks like an icon
        for table in ("TOAST_KIND", "NOTIF_META"):
            body = re.search(r"const " + table + r" = \{(.*?)\};", self.html, re.S)
            self.assertIsNotNone(body, f"{table} is gone, so its icon names are unchecked")
            refs |= set(re.findall(r"icon:\s*['\"]([\w-]+)['\"]", body.group(1)))
            # TOAST_KIND is `ok: 'check-circle'`, i.e. the value *is* the icon
            if "ok:" in body.group(1):
                refs |= set(re.findall(r":\s*['\"]([\w-]+)['\"]", body.group(1)))

        missing = sorted(r for r in refs if f"i-{r}" not in symbols)
        self.assertEqual(missing, [], "icons referenced but never defined: " + ", ".join(missing))

    def test_no_color_emoji_in_the_interface(self):
        """Emoji are a font fallback, not a design decision.

        They sit on a different baseline and at a different width than the text
        beside them, ignore the theme, and change shape between Windows and macOS —
        which is exactly why "🎬 Movies & HLS" never lined up with "▶ YouTube".
        Monochrome dingbats (✓ ✕ ★ ›) are punctuation-like and still allowed.
        """
        bad = re.compile("[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF]")
        # A comment may quote an old emoji for context; only rendered markup counts.
        lines = [i for i, ln in enumerate(self.markup.split("\n"), 1) if bad.search(ln)]
        self.assertEqual(lines, [], f"color emoji on lines {lines[:12]}")

    def test_the_changelog_covers_the_shipped_version(self):
        """"Every release gets a visible What's-New entry" used to be a rule you
        either remembered or did not. Now it is a failing test: the version in
        app.py must be the first entry of the CHANGELOG the modal renders."""
        version = re.search(r'APP_VERSION\s*=\s*"([^"]+)"',
                            (INDEX.parent / "app.py").read_text(encoding="utf-8")).group(1)
        block = re.search(r"const CHANGELOG = \[(.*?)\n        \];", self.html, re.S)
        self.assertIsNotNone(block, "the CHANGELOG array is missing")
        first = re.search(r"ver:\s*'([\d.]+)'", block.group(1))
        self.assertEqual(
            first and first.group(1), version,
            f"v{version} has no What's-New entry at the top of the changelog",
        )

    def test_indicator_elements_are_not_display_hidden(self):
        """A `display:none` indicator silently kills the whole animation."""
        for selector in (".tab-indicator", ".hist-indicator"):
            for block in re.findall(re.escape(selector) + r"[^{]*\{[^}]*\}", self.html, re.DOTALL):
                self.assertNotIn(
                    "display: none", block,
                    f"{selector} is hidden: {block.splitlines()[0]}",
                )


if __name__ == "__main__":
    unittest.main()
