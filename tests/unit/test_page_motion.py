"""The dashboard's motion layer: animations are plain CSS with no
library, respect prefers-reduced-motion, and the script lets a chart or
a figure animate only when what it shows has changed, never on a
routine refresh of the same data."""

import json
import re
import shutil
import subprocess
from importlib import resources

import pytest

PAGES = {name: resources.files("hlbot.api").joinpath(name).read_text(encoding="utf-8") for name in ("dashboard.html", "login.html")}


@pytest.mark.parametrize("name", PAGES)
def test_motion_is_plain_css_and_is_switched_off_for_reduced_motion(name):
    page = PAGES[name]
    css = page.split("<style>")[1].split("</style>")[0]
    assert "<script src" not in page and "@import" not in css and "url(http" not in css  # no animation library, nothing fetched
    reduced = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation-duration: .01ms !important" in reduced and "transition-duration: .01ms !important" in reduced and "animation-delay: 0s !important" in reduced
    defined = set(re.findall(r"@keyframes ([\w-]+)", css))
    used = set(re.findall(r"animation: ([\w-]+) ", css))
    assert used <= defined, used - defined  # every animation that is used exists
    # only transform, opacity, colour and clipping are animated: nothing that moves the layout
    for body in re.findall(r"@keyframes [\w-]+ \{(.*)\}\n", css):
        assert not re.search(r"\b(width|height|top|left|margin|padding)\s*:", body), body


def test_charts_and_figures_animate_only_on_a_real_change(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = PAGES["dashboard.html"].split("<script>")[-1].split("</script>")[0]
    helpers = script[script.index("function enter(el, signature, cls)"):script.index("/* ------------------------------------------------------------------ tooltip")]
    stub = tmp_path / "motion.js"
    stub.write_text(
        "const isNum = (v) => v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v));\n" + helpers + r"""
const element = () => ({dataset: {}, offsetWidth: 1, on: new Set(), log: [], classList: {toggle(name, force) { e.log.push([name, force]); if (force) e.on.add(name); else e.on.delete(name); }}});
let e = element();
const out = {};
enter(e, "1d", "draw"); out.first = [...e.on];
enter(e, "1d", "draw"); out.sameAgain = [...e.on];
enter(e, "1h", "draw"); out.changed = [...e.on];
e = element();
pulse(e, "$10.00"); out.firstValue = e.log.length;
pulse(e, "$10.00"); out.sameValue = e.log.length;
pulse(e, "$10.50"); out.newValue = e.log;
out.ticks = [tick("GOLD", 4200), tick("GOLD", 4200), tick("GOLD", 4201.5), tick("GOLD", 4199), tick("GOLD", null), tick("GOLD", 4199), tick("OTHER", 5)];
process.stdout.write(JSON.stringify(out));
"""
    )
    done = subprocess.run(["node", str(stub)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert (out["first"], out["sameAgain"], out["changed"]) == (["draw"], [], ["draw"])
    assert (out["firstValue"], out["sameValue"]) == (0, 0) and out["newValue"] == [["pulse", False], ["pulse", True]]  # the first value shown is not a change
    assert out["ticks"] == ["", "", " tick-up", " tick-down", "", "", ""]  # a direction only for a real move, never across a gap


def test_the_wide_screen_menu_opens_on_hover_and_closes_itself_after_a_short_delay(tmp_path):
    """The sidebar's own script against a minimal stand-in for the page:
    hover opens at once, leaving closes after the delay, coming back
    cancels it, keyboard focus holds it open, Escape closes it, a touch
    does not hover, and pinned it is left alone."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    page = PAGES["dashboard.html"]
    script = page.split("<script>")[-1].split("</script>")[0]
    menu = script[script.index("const sidebar = $(\"sidebar\")"):script.index("/* ------------------------------------------------------------------ the active menu item */")]
    stub = tmp_path / "menu.js"
    stub.write_text(r"""
const target = () => { const t = {on: {}, classes: new Set(), attrs: {}, hovered: false, keyboardFocus: false,
  addEventListener(name, fn) { (t.on[name] = t.on[name] || []).push(fn); }, fire(name, event) { for (const fn of t.on[name] || []) fn(event || {}); },
  classList: {toggle(name, force) { if (force) t.classes.add(name); else t.classes.delete(name); }, contains: (name) => t.classes.has(name)},
  setAttribute(k, v) { t.attrs[k] = v; }, matches: (sel) => sel === ":hover" ? t.hovered : false, querySelector: (sel) => sel === ":focus-visible" && t.keyboardFocus ? {} : null,
  contains: (node) => node === t.inside}; t.inside = {matches: () => true}; return t; };
const els = {sidebar: target(), logo: target(), "menu-pin": target()};
const $ = (id) => els[id];
const stored = {}, resized = [];
global.document = Object.assign(target(), {documentElement: {dataset: {}}});
global.localStorage = {setItem(k, v) { stored[k] = v; }};
global.window = {dispatchEvent(e) { resized.push(e.type); }};
global.Event = class { constructor(type) { this.type = type; } };
""" + menu + r"""
const s = els.sidebar, open = () => s.classes.has("open"), wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const out = {delay: MENU_CLOSE_DELAY_MS, start: open(), pinLabel: els["menu-pin"].attrs["aria-label"]};
  s.fire("pointerenter", {pointerType: "mouse"}); out.hover = open();
  s.fire("pointerleave", {pointerType: "mouse"}); out.justLeft = open();
  await wait(90); out.midDelay = open();
  s.fire("pointerenter", {pointerType: "mouse"}); await wait(MENU_CLOSE_DELAY_MS + 80); out.cameBack = open();
  s.fire("pointerleave", {pointerType: "mouse"}); await wait(MENU_CLOSE_DELAY_MS + 60); out.closed = open();
  for (let i = 0; i < 20; i++) s.fire(i % 2 ? "pointerleave" : "pointerenter", {pointerType: "mouse"});
  await wait(MENU_CLOSE_DELAY_MS + 60); out.afterQuickPasses = open();
  s.fire("pointerenter", {pointerType: "touch"}); out.touchHover = open();
  s.keyboardFocus = true; s.fire("focusin", {target: {matches: () => true}}); out.keyboard = open();
  s.fire("pointerenter", {pointerType: "mouse"}); s.fire("pointerleave", {pointerType: "mouse"}); await wait(MENU_CLOSE_DELAY_MS + 60); out.pointerLeftWhileKeyboardInside = open();
  document.fire("keydown", {key: "Escape"}); out.escape = open();
  s.fire("focusin", {target: {matches: () => false}}); out.mouseFocus = open();
  s.fire("focusin", {target: {matches: () => true}}); s.keyboardFocus = false; s.fire("focusout", {relatedTarget: null}); await wait(MENU_CLOSE_DELAY_MS + 60); out.focusLeft = open();
  els.logo.fire("click", {pointerType: "touch"}); out.tapLogo = open();
  document.fire("pointerdown", {pointerType: "touch", target: {}}); out.tapElsewhere = open();
  s.hovered = true; s.fire("pointerenter", {pointerType: "mouse"}); els["menu-pin"].onclick();
  out.pinned = [document.documentElement.dataset.menu, open(), stored["hlbot-menu"], els["menu-pin"].attrs["aria-pressed"], resized.length];
  s.fire("pointerenter", {pointerType: "mouse"}); out.hoverWhilePinned = open();
  els["menu-pin"].onclick(); out.unpinnedUnderPointer = [document.documentElement.dataset.menu, open(), stored["hlbot-menu"]];
  process.stdout.write(JSON.stringify(out));
})();
""")
    done = subprocess.run(["node", str(stub)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert 150 <= out["delay"] <= 250 and out["start"] is False and out["pinLabel"] == "Keep the menu open"
    assert (out["hover"], out["justLeft"], out["midDelay"], out["cameBack"], out["closed"]) == (True, True, True, True, False)
    assert out["afterQuickPasses"] is False and out["touchHover"] is False
    assert (out["keyboard"], out["pointerLeftWhileKeyboardInside"], out["escape"], out["mouseFocus"], out["focusLeft"]) == (True, True, False, False, False)
    assert (out["tapLogo"], out["tapElsewhere"]) == (True, False)
    assert out["pinned"] == ["pinned", False, "pinned", "true", 1] and out["hoverWhilePinned"] is False
    assert out["unpinnedUnderPointer"] == [None, True, "auto"]
    # the rail only where there is a mouse and room for it; the open menu lies over the page instead of pushing it
    css = page.split("<style>")[1].split("</style>")[0]
    assert "@media (min-width: 861px) and (hover: hover)" in css and ':root:not([data-menu="pinned"]) { --side: 68px; }' in css
    assert "grid-template-columns: var(--side, 216px)" in css and page.count('<span class="lbl">') == 9 and "transition-delay: 0s !important" in css
