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
