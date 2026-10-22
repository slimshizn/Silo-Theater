#!/usr/bin/env python3
"""Write resources/skins/Default/1080i/SiloPlayerMenu.xml.

Each rail tab needs a focus fill, a selected fill and three label colours,
and the row lists repeat the same columns in two layouts. Generating the XML
keeps ids and geometry in step with resources/lib/silo/ui_player.py, which
it imports. Commit the generated file too.

Run: python3 scripts/skin_player.py
"""

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "resources/skins/Default/1080i/SiloPlayerMenu.xml"
sys.path.insert(0, str(ROOT / "resources/lib"))
# ui_player imports Kodi modules; the generator only needs its constants.
sys.modules.setdefault("xbmc", types.ModuleType("xbmc"))
if "xbmcgui" not in sys.modules:
    sys.modules["xbmcgui"] = types.SimpleNamespace(WindowXMLDialog=object, ListItem=object)

from silo import ui_player as ui  # noqa: E402

TEXT = "FFEDEDED"
SECONDARY = "BFEDEDED"
MUTED = "B3EDEDED"
DIM = "9EEDEDED"
INK = "FF000000"
INK_MUTED = "94000000"
SMOKE = "F2131417"  # Android TvPlayerChrome.Smoke (#131417 at 95.5 %)

ROW_WIDTH = ui.CONTENT_WIDTH
INFO_LEFT_WIDTH = 660
COLUMN_GAP = 52
INFO_ROWS_LEFT = ui.CONTENT_LEFT + INFO_LEFT_WIDTH + COLUMN_GAP
INFO_ROWS_WIDTH = ui.CONTENT_LEFT + ui.CONTENT_WIDTH - INFO_ROWS_LEFT
STATS_WIDTH = (ui.CONTENT_WIDTH - COLUMN_GAP) // 2
# Row columns (x, width): check, label, value; chapter number, title, start, length.
CHECK = (20, 32)
LABEL = (64, 860)
VALUE = (940, ROW_WIDTH - 940 - 24)
NUMBER = (64, 60)
TITLE = (140, 900)
START = (1060, 180)
LENGTH = (1260, ROW_WIDTH - 1260 - 24)


def prop(name):
    return "Window.Property({})".format(name)


def has(name):
    return "!String.IsEmpty({})".format(prop(name))


def item_has(name):
    return "!String.IsEmpty(ListItem.Property({}))".format(name)


def tab_is(name):
    return "String.IsEqual({},{})".format(prop("silo.tab"), name)


def visible(condition):
    return "<visible>{}</visible>".format(condition) if condition else ""


def label(left, top, width, height, text, font, color, extra="", control_id=None):
    ident = ' id="{}"'.format(control_id) if control_id else ""
    return ('<control type="label"{}><left>{}</left><top>{}</top><width>{}</width><height>{}</height>'
            '<aligny>center</aligny><font>{}</font><textcolor>{}</textcolor><label>{}</label>{}</control>').format(
                ident, left, top, width, height, font, color, text, extra)


def image(left, top, width, height, texture, extra="", control_id=None):
    ident = ' id="{}"'.format(control_id) if control_id else ""
    return ('<control type="image"{}><left>{}</left><top>{}</top><width>{}</width><height>{}</height>'
            '{}{}</control>').format(ident, left, top, width, height, texture, extra)


def indent(lines, depth):
    return ["  " * depth + line for line in lines]


# --------------------------------------------------------------------- rail
def rail():
    left, width, positions = ui.rail_layout(list(ui.TAB_NAMES))
    lines = [image(left, ui.RAIL_TOP, width, ui.RAIL_HEIGHT,
                   '<texture border="38" colordiffuse="{}">pill76.png</texture>'.format(SMOKE),
                   control_id=ui.RAIL)]
    for name, text, width in ui.TABS:
        button = ui.TAB_BUTTON[name]
        focus = "Control.HasFocus({})".format(button)
        down = ui.ROWS if name in ui.ROW_TABS else button
        lines.append('<control type="group" id="{}">'.format(ui.TAB_GROUP[name]))
        lines.extend(indent([
            "<left>{}</left><top>{}</top><width>{}</width><height>{}</height>".format(
                positions[name], ui.TAB_TOP, width, ui.TAB_HEIGHT),
            visible(has("silo.tab." + name)),
            image(0, 0, width, ui.TAB_HEIGHT, '<texture border="30">pill60_selected.png</texture>',
                  visible("{} + !{}".format(tab_is(name), focus))),
            ('<control type="button" id="{}"><left>0</left><top>0</top><width>{}</width><height>{}</height>'
             '<texturefocus border="30" colordiffuse="{}">pill60.png</texturefocus><texturenofocus></texturenofocus>'
             '<label></label><onleft>{}</onleft><onright>{}</onright><onup>{}</onup><ondown>{}</ondown>'
             '</control>').format(button, width, ui.TAB_HEIGHT, TEXT, button, button, button, down),
            label(0, 0, width, ui.TAB_HEIGHT, text, "font25_title", INK,
                  "<align>center</align>" + visible(focus)),
            label(0, 0, width, ui.TAB_HEIGHT, text, "font25_title", TEXT,
                  "<align>center</align>" + visible("{} + !{}".format(tab_is(name), focus))),
            label(0, 0, width, ui.TAB_HEIGHT, text, "font25_title", DIM,
                  "<align>center</align>" + visible("!{} + !{}".format(tab_is(name), focus))),
        ], 1))
        lines.append("</control>")
    return lines


# -------------------------------------------------------------------- lists
def fact_list(control_id, left, width, condition):
    """Read-only label/value rows (Info and Stats)."""
    layout = [
        label(0, 0, 300, ui.FACT_HEIGHT, "$INFO[ListItem.Label]", "font12", MUTED),
        label(300, 0, width - 300, ui.FACT_HEIGHT, "$INFO[ListItem.Label2]", "font12", TEXT, "<align>right</align>"),
    ]
    lines = ['<control type="list" id="{}">'.format(control_id)]
    lines.extend(indent([
        "<left>{}</left><top>{}</top><width>{}</width><height>{}</height>".format(
            left, ui.CONTENT_TOP, width, ui.FACT_HEIGHT),
        visible(condition),
        "<orientation>vertical</orientation><scrolltime>0</scrolltime>",
        '<itemlayout height="{}" width="{}">'.format(ui.FACT_HEIGHT, width),
    ] + indent(layout, 1) + [
        "</itemlayout>",
        '<focusedlayout height="{}" width="{}">'.format(ui.FACT_HEIGHT, width),
    ] + indent(layout, 1) + ["</focusedlayout>"], 1))
    lines.append("</control>")
    return lines


def row_columns(color, muted, extra=""):
    plain = item_has("silo.start")
    return [
        image(CHECK[0], (ui.ROW_HEIGHT - CHECK[1]) // 2, CHECK[1], CHECK[1],
              '<texture colordiffuse="{}">icon_check.png</texture>'.format(color),
              visible(item_has("silo.checked") + extra)),
        label(LABEL[0], 0, LABEL[1], ui.ROW_HEIGHT, "$INFO[ListItem.Label]", "font27", color,
              visible("!" + plain + extra)),
        label(VALUE[0], 0, VALUE[1], ui.ROW_HEIGHT, "$INFO[ListItem.Label2]", "font27", muted,
              "<align>right</align>" + visible("!" + plain + extra)),
        label(NUMBER[0], 0, NUMBER[1], ui.ROW_HEIGHT, "$INFO[ListItem.Property(silo.number)]", "font27", muted,
              visible(plain + extra)),
        label(TITLE[0], 0, TITLE[1], ui.ROW_HEIGHT, "$INFO[ListItem.Label]", "font27", color,
              visible(plain + extra)),
        label(START[0], 0, START[1], ui.ROW_HEIGHT, "$INFO[ListItem.Property(silo.start)]", "font27", muted,
              "<align>right</align>" + visible(plain + extra)),
        label(LENGTH[0], 0, LENGTH[1], ui.ROW_HEIGHT, "$INFO[ListItem.Property(silo.length)]", "font27", muted,
              "<align>right</align>" + visible(plain + extra)),
    ]


def rows_list():
    focus = "Control.HasFocus({})".format(ui.ROWS)
    focused = [image(0, 2, ROW_WIDTH, ui.ROW_HEIGHT - 4,
                     '<texture border="16" colordiffuse="{}">round16.png</texture>'.format(TEXT), visible(focus))]
    focused += row_columns(INK, INK_MUTED, " + " + focus)
    focused += row_columns(TEXT, SECONDARY, " + !" + focus)
    lines = ['<control type="list" id="{}">'.format(ui.ROWS)]
    lines.extend(indent([
        "<left>{}</left><top>{}</top><width>{}</width><height>{}</height>".format(
            ui.CONTENT_LEFT, ui.CONTENT_TOP, ROW_WIDTH, ui.rows_area()),
        visible(has("silo.rows")),
        "<onup>{}</onup><ondown>{}</ondown><onleft>{}</onleft><onright>{}</onright>".format(
            ui.TAB_BUTTON["info"], ui.ROWS, ui.ROWS, ui.ROWS),
        "<orientation>vertical</orientation>",
        '<scrolltime tween="cubic" easing="out">200</scrolltime>',
        '<itemlayout height="{}" width="{}">'.format(ui.ROW_HEIGHT, ROW_WIDTH),
    ] + indent(row_columns(TEXT, SECONDARY), 1) + [
        "</itemlayout>",
        '<focusedlayout height="{}" width="{}">'.format(ui.ROW_HEIGHT, ROW_WIDTH),
    ] + indent(focused, 1) + ["</focusedlayout>"], 1))
    lines.append("</control>")
    return lines


def chapter_header():
    left, top, height = ui.CONTENT_LEFT, ui.CONTENT_TOP, ui.CHAPTER_HEADER
    condition = visible(tab_is("chapters"))
    return [
        label(left + NUMBER[0], top, NUMBER[1], height, "#", "font12", DIM, condition),
        label(left + TITLE[0], top, TITLE[1], height, "Chapter", "font12", DIM, condition),
        label(left + START[0], top, START[1], height, "Start", "font12", DIM, "<align>right</align>" + condition),
        label(left + LENGTH[0], top, LENGTH[1], height, "Length", "font12", DIM, "<align>right</align>" + condition),
    ]


# --------------------------------------------------------------------- info
def info_header():
    left, top = ui.CONTENT_LEFT, ui.CONTENT_TOP
    lines = ['<control type="group">', "  " + visible(tab_is("info"))]
    lines.extend(indent([
        label(left, top, INFO_LEFT_WIDTH, 34, "$INFO[{}]".format(prop("silo.eyebrow")), "font12", MUTED),
        label(left, top + 34, INFO_LEFT_WIDTH, 64, "$INFO[{}]".format(prop("silo.title")), "font36_title", TEXT),
        label(left, top + 104, INFO_LEFT_WIDTH, 40, "$INFO[{}]".format(prop("silo.runtime")), "font12", SECONDARY),
    ], 1))
    for slot in range(ui.MAX_BADGES):
        name = "silo.badge.{}".format(slot)
        lines.append("  " + image(left, ui.BADGE_TOP, 60, ui.BADGE_HEIGHT, '<texture border="8">badge.png</texture>',
                                  visible(has(name)), ui.BADGE + slot))
        lines.append("  " + label(left, ui.BADGE_TOP, 60, ui.BADGE_HEIGHT, "$INFO[{}]".format(prop(name)),
                                  "font_flag", TEXT, "<align>center</align>" + visible(has(name)),
                                  ui.BADGE_LABEL + slot))
    lines.append("</control>")
    return lines


def build():
    controls = rail()
    controls.append(image(ui.PANEL_LEFT, ui.PANEL_TOP, ui.PANEL_WIDTH, ui.PANEL_MIN,
                          '<texture border="28" colordiffuse="{}">round28.png</texture>'.format(SMOKE),
                          control_id=ui.PANEL))
    controls += info_header()
    controls += fact_list(ui.INFO_ROWS, INFO_ROWS_LEFT, INFO_ROWS_WIDTH, tab_is("info"))
    controls += fact_list(ui.STATS_LEFT, ui.CONTENT_LEFT, STATS_WIDTH, tab_is("stats"))
    controls += fact_list(ui.STATS_RIGHT, ui.CONTENT_LEFT + STATS_WIDTH + COLUMN_GAP, STATS_WIDTH, tab_is("stats"))
    controls += chapter_header()
    controls += rows_list()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!-- Generated by scripts/skin_player.py; edit the generator, not this file.",
        "     Silo player menu drawn over fullscreen video: a tab rail at the top centre and a",
        "     smoked panel under it. ui_player.PlayerMenu positions the tabs, badges and panel height. -->",
        '<window type="dialog">',
        '  <defaultcontrol always="true">{}</defaultcontrol>'.format(ui.TAB_BUTTON["info"]),
        "  <coordinates><system>1</system><width>1920</width><height>1080</height></coordinates>",
        '  <animation effect="fade" start="0" end="100" time="150">WindowOpen</animation>',
        '  <animation effect="fade" start="100" end="0" time="150">WindowClose</animation>',
        "  <controls>",
    ] + indent(controls, 2) + ["  </controls>", "</window>", ""]
    return "\n".join(lines)


def main():
    OUTPUT.write_text(build(), encoding="utf-8")
    print("wrote", OUTPUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
