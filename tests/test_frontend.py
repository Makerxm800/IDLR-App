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


def without_css_comments(css):
    """CSS minus its `/* */` prose.

    Explanatory comments in this sheet quote the very declarations they justify, so
    a test that greps the raw text passes on the comment while the actual rule has
    been changed — which is how a guard for `overflow: hidden` stopped guarding.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def text_the_user_can_read(html):
    """Markup with `<script>` and `<style>` removed — what is actually on screen.

    A glyph in a CSS comment is documentation; the same glyph in a sentence under
    a switch is a promise about a control that may not exist.
    """
    out = re.sub(r"<style\b.*?</style>", "", markup_without_scripts(html),
                 flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"<!--.*?-->", "", out, flags=re.DOTALL)


class FrontendConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.markup = markup_without_scripts(cls.html)
        cls.ids = set(MARKUP_ID.findall(cls.markup))
        cls.css = "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", cls.html,
                                        re.DOTALL | re.IGNORECASE))

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


class StyleSheetShapeTests(unittest.TestCase):
    """The sheet has to stay parseable, not merely readable.

    One brace too many is invisible in a diff and unmistakable in the app: a stray
    `}` closes the enclosing block early, and in a stylesheet written by hand over
    months the rules that follow it can stop applying without any of their own text
    changing. `block_after`-style probes would keep passing, because they read
    declarations as text; only the shape of the whole sheet shows the damage.
    """

    def setUp(self):
        html = INDEX.read_text(encoding="utf-8")
        blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", html,
                            re.DOTALL | re.IGNORECASE)
        self.styles = blocks
        # Comments go first: they quote CSS, and a `{` in a sentence is not a block.
        self.css = [without_css_comments(b) for b in blocks]

    def test_every_style_block_closes_what_it_opens(self):
        for n, css in enumerate(self.css, 1):
            depth = 0
            for line, text in enumerate(css.splitlines(), 1):
                for char in text:
                    if char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                if depth < 0:
                    self.fail(f"style block {n} closes a block it never opened at "
                              f"line {line} — every rule after that line is being "
                              f"parsed inside the wrong selector")
            self.assertEqual(depth, 0,
                             f"style block {n} leaves {depth} block(s) open at the end")

    def test_the_sheet_is_one_block_with_real_rules(self):
        self.assertEqual(len(self.css), 1,
                         "index.html keeps its CSS in one <style> block; a second one "
                         "means two files of rules whose order decides what wins")
        rules = sum(b.count("{") for b in self.css)
        self.assertGreater(rules, 700, "the stylesheet lost most of its rules")


class PaintAndGeometryTests(unittest.TestCase):
    """Two things every other test here was blind to.

    The notification popover clipped its own last row because nothing capped the
    panel, and the settings sheen painted a red band over the cards beside it
    because the pseudo-element was never hidden and its row never clipped. Neither
    is a missing id or a dangling handler: they only ever showed up as an app that
    looked sloppy.

    The assertions read the block that follows an exact selector rather than
    parsing CSS. `index.html` keeps `@media` and `@keyframes` blocks, and any
    brace-walking parser gets its selector/body pairs wrong immediately after
    them — which is precisely where a duplicate rule likes to hide.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.markup = markup_without_scripts(cls.html)
        cls.visible = text_the_user_can_read(cls.html)
        cls.css = without_css_comments("\n".join(
            re.findall(r"<style\b[^>]*>(.*?)</style>", cls.html, re.DOTALL | re.IGNORECASE)))

    def block_after(self, marker, what):
        start = self.css.find(marker)
        self.assertNotEqual(start, -1, f"{marker!r} is gone — {what} is no longer checked")
        open_brace = self.css.index("{", start)
        return self.css[open_brace + 1:self.css.index("}", open_brace)]

    def blocks_after(self, marker, what):
        """Every body written for `marker`, in source order.

        `block_after` reads the first one, which is the right answer while a
        selector is declared once and wrong the moment it is declared twice — and
        a selector split between a plain rule and a media query is declared twice
        on purpose, so "is this true of the panel as rendered" has to be checked
        against the pair.
        """
        bodies = []
        at = 0
        while True:
            start = self.css.find(marker, at)
            if start == -1:
                break
            open_brace = self.css.index("{", start)
            bodies.append(self.css[open_brace + 1:self.css.index("}", open_brace)])
            at = open_brace
        self.assertTrue(bodies, f"{marker!r} is gone — {what} is no longer checked")
        return bodies

    @staticmethod
    def code_only(text):
        """`text` with block comments taken out.

        A guard that forbids a call has to read code, not prose: the comment that
        explains *why* the call is gone names it, and matching that would leave the
        test asserting nothing anyone could break again.
        """
        return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)

    def js_function(self, name):
        """The body of `function name(...) { ... }`, brace-matched.

        CSS gets read by selector for the same reason: `index.html` is one file of
        hand-written markup, scripts and styles, and any extractor that counts
        braces without caring about strings gets lost in it. This one only skips
        over braces, which is enough for a body that closes itself.
        """
        match = re.search(r"\bfunction " + re.escape(name) + r"\s*\(", self.html)
        self.assertIsNotNone(match, f"function {name} is gone")
        open_brace = self.html.index("{", match.end())
        depth = 0
        for i in range(open_brace, len(self.html)):
            if self.html[i] == "{":
                depth += 1
            elif self.html[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.html[open_brace + 1:i]
        self.fail(f"function {name} never closes")

    def test_the_notifications_panel_is_capped_and_the_list_scrolls(self):
        panel = self.block_after(".notif-popover {\n            width: min(392px",
                                 "the notification popover's size")
        self.assertIn("max-height", panel,
                      "an uncapped popover grows past the bottom of the window and its "
                      "own overflow:hidden eats the row you were reading")
        self.assertIn("display: flex", panel)
        self.assertEqual(
            len(re.findall(r"\.notif-popover \.pop-body \.notif-list\s*\{", self.css)), 1,
            "the list's geometry is declared more than once; whichever rule loses the "
            "cascade decides which theme clips rows")
        listed = self.block_after(".notif-popover .pop-body .notif-list {", "list scrolling")
        for declaration in ("overflow-y: auto", "flex: 1", "min-height: 0"):
            self.assertIn(declaration, listed,
                          f"without `{declaration}` the list is not the scroller")

    def test_the_hover_sheen_stays_in_its_own_box_without_eating_the_tooltip(self):
        """The last release clipped the row to contain the sweep — and clipped the
        tooltip with it.

        `.toggle-switch::after` paints *above* the switch (it needs ~34px of room)
        inside a row that offers ~21px, so `overflow: hidden` on `.setting-row` cut
        `Full Playlist: Enabled` in half. The sweep is now a gradient painted inside
        the pseudo-element's own box, so it cannot reach a neighbour and nothing has
        to clip. Both halves are asserted: contain the paint, do not clip the card.
        """
        tooltip = self.block_after(".toggle-switch::after {", "the toggle tooltip")
        self.assertIn("bottom: calc(100% + 8px)", tooltip,
                      "the tooltip is assumed to paint above the switch; if that moves, "
                      "the clipping question below changes too and so should this test")
        row = self.block_after(".setting-row,\n        .theme-option {", "the settings row box")
        self.assertNotIn("overflow: hidden", row,
                         "clipping the row to contain the sheen also clips the toggle "
                         "tooltip — contain the paint instead")
        rest = self.block_after(".setting-row::after,\n        .theme-option::after {",
                                "the sheen at rest")
        hover = self.block_after(".setting-row:hover::after,", "the sheen on hover")
        self.assertIn("opacity: 0", rest,
                      "a sheen painted while nothing is hovered is the light effect users "
                      "report as a bug")
        for escape_route in ("left:", "translate3d", "width: 45%"):
            self.assertNotIn(escape_route, rest,
                             f"`{escape_route}` makes the band a box that can leave the card")
        self.assertIn("background-size: 250% 100%", rest,
                      "painting inside the box needs an oversized background to slide")
        self.assertIn("border-radius: inherit", rest,
                      "nothing clips the paint now, so it has to follow the card's rounding")
        self.assertIn("opacity: 1", hover)
        self.assertIn("background-position", hover)
        self.assertIn("body.reduce-motion .setting-row::after", self.css,
                      "Calm Motion has to switch the sweep off, not merely shorten it")
        self.assertNotIn("body.no-anim .setting-row::after", self.css,
                         "no-anim is the Hero Animations switch; settings feedback must not "
                         "answer to it")

    def test_nothing_animates_forever_behind_the_pages(self):
        self.assertNotIn("glowMove", self.html,
                         "an infinite 12s drift shows through every frosted panel and costs "
                         "a compositor frame until the app is closed")
        self.assertNotIn("animation", self.block_after(".glow {", "the ambient wash"),
                         ".glow still animates")

    def test_every_switch_row_has_the_parts_that_keep_it_honest(self):
        """A row loses its sub-text, its detail or its info button one refactor at a
        time, and the switch then quietly disagrees with its own caption."""
        switches = re.findall(r'<div class="setting-row[^"]*" id="row-([\w-]+)"[^>]*role="switch"',
                              self.markup)
        self.assertGreaterEqual(len(switches), 8,
                                "only found these switch rows: " + ", ".join(switches))
        for key in switches:
            parts = (
                ("switch", 'id="toggle-' + key + '"'),
                ("state text", 'id="sub-' + key + '"'),
                ("detail text", 'id="det-' + key + '"'),
                ("info button", 'id="info-btn-' + key + '"'),
                ("handler", "onclick=" + chr(34) + "toggleSetting('" + key + "')" + chr(34)),
                ("keyboard handler",
                 "onkeydown=" + chr(34) + "toggleKey(event,'" + key + "')" + chr(34)),
            )
            for part, pattern in parts:
                self.assertIn(pattern, self.markup, key + " has no " + part)
            for table in ("_settingHints", "_settingSubs"):
                body = re.search("const " + table + r" = \{(.*?)\};", self.html, re.DOTALL)
                self.assertIsNotNone(body, table + " disappeared")
                self.assertIn(key + ":", body.group(1), key + " is missing from " + table)

    def test_idlr_never_asks_the_browser_to_notify_for_it(self):
        """The user's words were "i dont want it use microsoft browser".

        A `new Notification(...)` raised from a page is drawn and *signed* by
        whatever engine shows that page: in the desktop app that is Edge's
        WebView2, so an IDLR download arrived labelled "Microsoft Edge" and
        needed a permission grant before it worked at all. The in-app bell,
        the toast and the badge are the notification now, so nothing may reach
        for the browser's API again — not the send, not the ask, not a row
        promising a state the app no longer controls.
        """
        code = self.code_only(self.html)
        for pattern in (r"new\s+Notification\s*\(", r"Notification\.requestPermission",
                        r"Notification\.permission"):
            self.assertNotRegex(code, pattern,
                                "notifications are IDLR's own UI; this reaches for the "
                                "browser's instead")
        for dead in ("row-os-notif", "notif-os-state", "requestNotifPermission",
                     "renderNotifPermissionRow", "notifyOS"):
            self.assertNotIn(dead, self.html, dead + " is left over from the browser path")

    def test_the_two_rail_panels_cannot_both_be_open(self):
        """The bell and the account chip sit side by side and both anchored their
        panel to `top: calc(100% + 12px)` of their own wrapper, so opening the
        notifications put a panel on top of the account menu, or under it."""
        notif = self.js_function("toggleNotifPopover")
        chip = self.js_function("toggleUserPopover")
        self.assertIn("closePopover", notif,
                      "opening the bell has to fold the account menu away first")
        self.assertIn("closeNotifPopover", chip,
                      "opening the account menu has to fold the bell away first")

    def test_desktop_popovers_open_beside_the_rail_not_down_it(self):
        """228px of sidebar cannot hold a 392px panel.

        Anchored inside the rail, the notification panel grew over the Info,
        Settings and account rows below the bell — the position the screenshots
        complained about. Desktop mode now pins both panels to the workspace
        beside the rail, at the rail's own width variable, so a change to the
        sidebar can never leave the panel behind.
        """
        flyout = "".join(self.blocks_after(
            "body.desktop-shell :is(.notif-popover, .user-popover) {",
            "the desktop flyout both panels share"))
        for declaration in ("position: fixed", "left: calc(var(--rail-w) + 14px)",
                            "max-height", "bottom: 18px"):
            self.assertIn(declaration, flyout, "the flyout lost " + declaration)
        rail = self.block_after("body.desktop-shell #main-app {", "the desktop grid")
        self.assertIn("var(--rail-w", rail,
                      "the grid still hard-codes the rail width the flyout aligns to")
        self.assertIn("body.desktop-shell { --rail-w: 228px; }", self.css,
                      "nothing defines the rail width the popover offsets itself by")

    def test_switches_do_not_move_the_thing_you_are_clicking(self):
        """Hover used to scale the whole 46px control, so the switch ran away
        under the pointer; the knob's curve ended at a `scale(1.08)` pop."""
        hover = self.block_after(".toggle-switch:hover {", "the switch hover state")
        self.assertNotIn("transform", hover,
                         "a hover transform moves the target mid-click; brighten it instead")
        knob = self.block_after(".toggle-knob {", "the knob travel")
        self.assertNotIn("1.8", knob,
                         "an overshoot that strong pushes the knob out of its slot")
        self.assertNotIn("togglePop", self.html,
                         "a keyframe on the state class replays whenever the row repaints")
        active = self.block_after(":is(.toggle-switch:active, .setting-row:active) .toggle-knob {",
                                  "the press feedback")
        self.assertIn("scaleX", active, "the press should stretch the knob, not the track")
        on_press = self.block_after(
            ":is(.toggle-switch:active, .setting-row:active) .toggle-switch.on .toggle-knob,",
            "the press feedback on a switch that is already on")
        self.assertIn("translateX(20px) scaleX(1.16)", on_press,
                      "the on-state press has to compose with the knob's travel — "
                      "scaleX alone would snap the knob back to the off end while "
                      "the row is held down")

    def test_every_switch_lands_in_the_same_column(self):
        right = self.block_after(".setting-row-right {", "the right-hand control column")
        for declaration in ("min-width: 78px", "justify-content: flex-end"):
            self.assertIn(declaration, right,
                          "without a column of its own the switch sits wherever the "
                          "caption happens to end, so rows do not line up")

    def test_one_writer_for_the_save_folder(self):
        """Three surfaces show the path — the download page, Settings and Info."""
        body = self.js_function("updatePathDisplay")
        for surface in ("path-txt", "sp-savepath", "info-savepath"):
            self.assertIn(surface, body, "the save folder is displayed without " + surface)
        _, sep, settings_row = self.markup.partition('id="row-save-path"')
        self.assertTrue(sep, "the Download folder row in Settings is gone")
        self.assertIn('onclick="changePath()"', settings_row[:900],
                      "Settings must open the same picker as the download page, not a "
                      "second, smaller dialog a desktop build could have skipped")
        _, sep, bar = self.markup.partition('<div class="path-bar"')
        self.assertTrue(sep, "the save-location bar on the download page is gone")
        for action in ("openSaveFolder()", "changePath()"):
            self.assertIn(action, bar[:1200], "the download page is missing " + action)

    def test_the_hero_has_no_second_site_list(self):
        """The Info dialog already lists what IDLR detects; the hero's Sites
        button was the same nine names behind a panel that covered the options."""
        for gone in ("sites-btn", "sites-panel", "renderSitesPanel", "toggleSitesPanel",
                     "idlr_sites_open"):
            self.assertNotIn(gone, self.html, gone + " belongs to the removed hero panel")
        self.assertIn('id="info-chips"', self.markup, "the chips in Info have to stay")
        self.assertIn("function renderInfoChips", self.html)
        self.assertIn("const SITES = [", self.html,
                      "the detector, the history thumbnails and the Info chips all read this")

    def test_settings_copy_describes_a_control_that_exists(self):
        """The blurb used to say "tap ⓘ" when the control is an SVG info button, and
        the alerts row promised an OS notification the webview may not deliver."""
        self.assertNotIn("\u24d8", self.visible,
                         "a sentence pointing at a glyph is not pointing at the icon button "
                         "that is actually there")
        self.assertIn("info button", self.visible)
        for line in re.findall(r"dlNotif: \[[^\]]*\]", self.html):
            self.assertNotIn("Notify when done", line,
                             "promises a system notification from a toggle that only feeds "
                             "the in-app list: " + line)
