#!/usr/bin/env python3
"""Write resources/skins/Default/1080i/SiloHome.xml.

The home screen repeats the same row slot and card layouts many times, so
the XML is generated here and committed. Control ids must match
resources/lib/silo/ui_home.py.

Run: python3 scripts/skin_home.py
"""

from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "resources/skins/Default/1080i/SiloHome.xml"

TEXT, SECONDARY, MUTED, TAB_IDLE = "FFEDEDED", "BFEDEDED", "B3EDEDED", "9EEDEDED"
CAPTION_IDLE, BLACK, SURFACE = "C7EDEDED", "FF000000", "FF15171C"

SLOTS, SLOT_BASE = 12, 3000
TABS = (("home", "Home", 210, 215, 124), ("movies", "Movies", 211, 216, 148),
        ("series", "Series", 212, 217, 140), ("for_you", "For You", 203, 206, 156),
        ("calendar", "Calendar", 204, 207, 172), ("requests", "Requests", 205, 208, 172))
SEARCH_CHIPS = ((530, "video", "All", 112), (531, "movie", "Movies", 150), (532, "series", "Series", 140))
MENU_ITEMS = ((610, "Switch profile"), (613, "Watchlist"), (614, "Favorites"), (615, "History"),
              (611, "Settings"), (616, "Edit home rows"), (617, "Switch server"), (612, "Sign out"))
PILL_ITEMS = ((642, "Recommended"), (643, "Browse"), (644, "Collections"))
# Library chips in order: (id, chevron ids or None).
LIBRARY_CHIPS = ((510, (512, 513)), (511, (514, 515)), (516, None), (517, None), (518, None))
RAIL_LETTERS = 28
FOCUS_ZOOM = 'effect="zoom" start="100" end="{}" time="200" tween="cubic" easing="out" center="auto"'


def esc(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def rect(left, top, width, height):
    return "<left>{}</left><top>{}</top><width>{}</width><height>{}</height>".format(left, top, width, height)


def texture(name, tag="texture", color=None, border=None, diffuse=None, background=False):
    attrs = ""
    if color:
        attrs += ' colordiffuse="{}"'.format(color)
    if border:
        attrs += ' border="{}"'.format(border)
    if diffuse:
        attrs += ' diffuse="{}"'.format(diffuse)
    if background:
        attrs += ' background="true"'
    return "<{0}{1}>{2}</{0}>".format(tag, attrs, esc(name))


def visible(condition):
    return "<visible>{}</visible>".format(esc(condition)) if condition else ""


def image(left, top, width, height, name, color=None, border=None, diffuse=None, aspect=None,
          condition=None, control_id=None, fade=None, background=False):
    parts = ['<control type="image"{}>'.format(' id="{}"'.format(control_id) if control_id else ""),
             rect(left, top, width, height)]
    if aspect:
        parts.append(aspect if aspect.startswith("<") else "<aspectratio>{}</aspectratio>".format(aspect))
    if fade:
        parts.append("<fadetime>{}</fadetime>".format(fade))
    parts += [texture(name, color=color, border=border, diffuse=diffuse, background=background),
              visible(condition), "</control>"]
    return "".join(parts)


def label(left, top, width, height, text, font, color, align="left", condition=None, control_id=None,
          scroll=False, kind="label"):
    parts = ['<control type="{}"{}>'.format(kind, ' id="{}"'.format(control_id) if control_id else ""),
             rect(left, top, width, height), "<font>{}</font>".format(font),
             "<textcolor>{}</textcolor>".format(color), "<align>{}</align>".format(align)]
    parts.append("<aligny>{}</aligny>".format("bottom" if kind == "textbox" else "center"))
    if scroll:
        parts.append("<scroll>true</scroll>")
    parts += ["<label>{}</label>".format(esc(text)), visible(condition), "</control>"]
    return "".join(parts)


def nav(control_id, up=None, down=None, left=None, right=None):
    """Navigation tags; a missing direction points back at the control itself."""
    return "".join("<{0}>{1}</{0}>".format(tag, target or control_id)
                   for tag, target in (("onup", up), ("ondown", down), ("onleft", left), ("onright", right)))


def button(control_id, left, top, width, height, text, font="font25_title", color=TEXT, focus="pill60.png",
           focus_border=30, idle=None, idle_border=None, align="center", offset=0, navigation="",
           condition=None):
    parts = ['<control type="button" id="{}">'.format(control_id), rect(left, top, width, height),
             "<label>{}</label>".format(esc(text)), "<font>{}</font>".format(font),
             "<textcolor>{}</textcolor><focusedcolor>{}</focusedcolor>".format(color, BLACK),
             "<align>{}</align><aligny>center</aligny>".format(align),
             "<textoffsetx>{}</textoffsetx>".format(offset) if offset else "",
             texture(focus, "texturefocus", color=TEXT, border=focus_border) if focus else "<texturefocus />",
             texture(idle, "texturenofocus", border=idle_border) if idle else "<texturenofocus />",
             navigation, visible(condition), "</control>"]
    return "".join(parts)


def group(control_id, left, top, width, height, children, condition=None, animation=""):
    head = '<control type="group"{}>'.format(' id="{}"'.format(control_id) if control_id else "")
    return "".join([head, rect(left, top, width, height), animation, visible(condition)] + children
                   + ["</control>"])


def progress(left, top, width):
    return "".join(['<control type="progress">', rect(left, top, width, 6),
                    "<info>ListItem.PercentPlayed</info>",
                    texture("white.png", "texturebg", color="33FFFFFF"),
                    texture("white.png", "midtexture", color=TEXT),
                    "<lefttexture /><righttexture /><overlaytexture />",
                    visible("!String.IsEmpty(ListItem.Property(silo.progress))"), "</control>"])


# ---------------------------------------------------------------- cards

def card_art(width, height, mask, art, wide, focused, list_id):
    """Card art, overlays and (focused) ring + glow, relative to the card's corner."""
    has_focus = "Control.HasFocus({})".format(list_id)
    parts = []
    if focused:
        parts.append(image(-24, -24, width + 48, height + 48, "glow16.png", border=48, condition=has_focus))
    parts += [image(0, 0, width, height, "round16.png", color=SURFACE, border=16),
              image(0, 0, width, height, "$INFO[ListItem.Art({})]".format(art), diffuse=mask, aspect="scale",
                    background=True)]
    if wide:
        parts += [image(0, 0, width, height, "scrim_bottom.png", color="D9000000", diffuse=mask),
                  image(14, height - 60, 124, 40, "round6.png", color="99000000", border=6,
                        condition="!String.IsEmpty(ListItem.Property(silo.badge))"),
                  label(14, height - 60, 124, 40, "$INFO[ListItem.Property(silo.badge)]", "font12", TEXT,
                        "center")]
    else:
        watched = "!String.IsEmpty(ListItem.Property(silo.watched))"
        parts += [image(width - 53, 11, 42, 42, "circle.png", color="FFFFFFFF", condition=watched),
                  image(width - 45, 19, 26, 26, "icon_check.png", color=BLACK, condition=watched)]
    parts.append(progress(16, height - 18, width - 32))
    if focused:
        parts.append(image(-4, -4, width + 8, height + 8, "ring16.png", color=TEXT, border=24,
                           condition=has_focus))
    return parts


def card_layout(tag, item_width, item_height, offset_x, offset_y, width, height, mask, art, wide, zoom, list_id,
                focused):
    has_focus = "Control.HasFocus({})".format(list_id)
    animation = ('<animation {} condition="{}">Conditional</animation>'.format(FOCUS_ZOOM.format(zoom), has_focus)
                 if focused else "")
    caption_top = offset_y + height + 14
    parts = ['<{} width="{}" height="{}">'.format(tag, item_width, item_height),
             group(None, offset_x, offset_y, width, height,
                   card_art(width, height, mask, art, wide, focused, list_id), animation=animation)]
    if focused:
        parts += [label(offset_x, caption_top, width, 40, "$INFO[ListItem.Label]", "font27", TEXT,
                        condition=has_focus, scroll=True),
                  label(offset_x, caption_top, width, 40, "$INFO[ListItem.Label]", "font27", CAPTION_IDLE,
                        condition="!" + has_focus)]
    else:
        parts.append(label(offset_x, caption_top, width, 40, "$INFO[ListItem.Label]", "font27", CAPTION_IDLE))
    parts += [label(offset_x, caption_top + 40, width, 34, "$INFO[ListItem.Label2]", "font12", MUTED),
              "</{}>".format(tag)]
    return "".join(parts)


def card_list(list_id, kind, left, top, width, height, item_width, item_height, offset_x, offset_y,
              card_width, card_height, mask, art, wide, zoom, navigation, orientation="horizontal"):
    layouts = [card_layout(tag, item_width, item_height, offset_x, offset_y, card_width, card_height, mask, art,
                           wide, zoom, list_id, tag == "focusedlayout")
               for tag in ("itemlayout", "focusedlayout")]
    extra = "<focusposition>0</focusposition>" if kind == "fixedlist" else ""
    return "".join(['<control type="{}" id="{}">'.format(kind, list_id), rect(left, top, width, height),
                    "<orientation>{}</orientation>".format(orientation), extra,
                    '<scrolltime tween="cubic" easing="out">200</scrolltime><preloaditems>2</preloaditems>',
                    navigation] + layouts + ["</control>"])


# ---------------------------------------------------------------- screen parts

def top_bar():
    bar_focus = "ControlGroup(200).HasFocus() | ControlGroup(600).HasFocus()"
    dim = ('<animation effect="fade" start="100" end="70" time="200" condition="!({})">Conditional</animation>'
           .format(bar_focus))
    search = [button(201, 0, 4, 56, 56, "", focus="circle.png", focus_border=None, navigation=nav(201, right=210)),
              image(14, 18, 28, 28, "icon_search.png", color="9EFFFFFF", condition="!Control.HasFocus(201)"),
              image(14, 18, 28, 28, "icon_search.png", color=BLACK, condition="Control.HasFocus(201)")]
    tabs = []
    ids = [201] + [tab[2] for tab in TABS] + [230]
    for index, (key, text, button_id, group_id, width) in enumerate(TABS):
        selected = "String.IsEqual(Window.Property(silo.tab),{}) + !Control.HasFocus({})".format(key, button_id)
        tabs.append(group(group_id, 80 + sum(tab[4] + 8 for tab in TABS[:index]), 2, width, 60, [
            image(0, 0, width, 60, "pill60_selected.png", border=30, condition=selected),
            button(button_id, 0, 0, width, 60, text, color=TAB_IDLE,
                   navigation=nav(button_id, left=ids[index], right=ids[index + 2])),
            label(0, 0, width, 60, text, "font25_title", TEXT, "center", condition=selected),
        ]))
    avatar = [button(230, 0, 0, 56, 56, "", focus=None, navigation=nav(230, left=TABS[-1][2])),
              image(0, 0, 56, 56, "circle.png", color="2EFFFFFF"),
              label(0, 0, 56, 56, "$INFO[Window.Property(silo.initials)]", "font25_title", TEXT, "center"),
              image(0, 0, 56, 56, "$INFO[Window.Property(silo.avatar)]", diffuse="mask_circle.png",
                    aspect="scale", background=True),
              image(-4, -4, 64, 64, "ring_circle.png", color=TEXT, condition="Control.HasFocus(230)")]
    return group(200, 0, 56, 1920, 64, [
        label(88, 0, 520, 64, "Founders Theater", "font36_title", TEXT),
        group(202, 404, 0, 1200, 64, search + tabs),
        group(None, 1776, 4, 56, 56, avatar),
    ], animation=dim)


def row_slot(index):
    base = SLOT_BASE + 10 * index
    wide_id, poster_id = base + 3, base + 4
    return group(base, 0, 530, 1920, 450, [
        image(88, 4, 44, 44, "icon_play_circle.png", control_id=base + 2),
        label(88, 0, 1600, 52, "", "font30_title", TEXT, control_id=base + 1),
        card_list(wide_id, "fixedlist", 52, 56, 1868, 330, 400, 330, 36, 32, 360, 202, "mask_wide.png",
                  "thumb", True, 104, nav(wide_id)),
        card_list(poster_id, "fixedlist", 48, 44, 1872, 406, 216, 406, 40, 44, 176, 264, "mask_poster.png",
                  "poster", False, 110, nav(poster_id)),
    ])


def home():
    badges = []
    for slot in range(1, 5):
        prop = "Window.Property(silo.hero.badge{})".format(slot)
        badges += [image(88, 346, 60, 36, "badge.png", border=8, control_id=410 + slot,
                         condition="!String.IsEmpty({})".format(prop)),
                   label(88, 346, 60, 36, "$INFO[{}]".format(prop), "font_flag", TEXT, "center",
                         control_id=420 + slot)]
    logo = "Window.Property(silo.hero.logo)"
    hero = group(400, 0, 0, 1920, 530, [
        image(88, 180, 880, 150, "$INFO[{}]".format(logo), fade=200, background=True,
              aspect='<aspectratio align="left" aligny="bottom">keep</aspectratio>',
              condition="!String.IsEmpty({})".format(logo)),
        label(88, 180, 880, 150, "$INFO[Window.Property(silo.hero.title)]", "font_MainMenu", TEXT,
              condition="String.IsEmpty({})".format(logo), kind="textbox"),
    ] + badges + [
        label(88, 346, 880, 36, "$INFO[Window.Property(silo.hero.meta)]", "font12", SECONDARY, control_id=430),
        '<control type="textbox">' + rect(88, 396, 780, 84) + "<font>font27</font><textcolor>" + MUTED
        + "</textcolor><label>$INFO[Window.Property(silo.hero.synopsis)]</label></control>",
    ])
    return group(300, 0, 0, 1920, 1080, [
        image(691, 0, 1229, 756, "$INFO[Window.Property(silo.hero.backdrop)]", diffuse="mask_hero.png",
              aspect="scale", fade=400, background=True),
        image(0, 0, 1920, 240, "scrim_top.png", color=BLACK),
        hero,
    ] + [row_slot(index) for index in range(SLOTS)], condition="String.IsEqual(Window.Property(silo.tab),home)")


def chip(index):
    """Library grid chip; Python sets its label, width, position and left/right chain."""
    control_id, chevrons = LIBRARY_CHIPS[index]
    left = LIBRARY_CHIPS[index - 1][0] if index else None
    right = LIBRARY_CHIPS[index + 1][0] if index + 1 < len(LIBRARY_CHIPS) else None
    parts = [button(control_id, 88, 188, 260, 56, "", focus="pill56.png", focus_border=28, idle="pill56_idle.png",
                    idle_border=28, align="left", offset=28,
                    navigation=nav(control_id, down=500, left=left, right=right))]
    if chevrons:
        idle, focused = chevrons
        parts += [image(296, 204, 24, 24, "icon_chevron_down.png", color=TEXT, control_id=idle,
                        condition="!Control.HasFocus({})".format(control_id)),
                  image(296, 204, 24, 24, "icon_chevron_down.png", color=BLACK, control_id=focused,
                        condition="Control.HasFocus({})".format(control_id))]
    return parts


def filter_badge():
    """Active-filter count on the Filters chip; colours invert with the chip."""
    count = "Window.Property(silo.filter_count)"
    focused = "Control.HasFocus(516)"
    return group(525, 300, 200, 32, 32, [
        image(0, 0, 32, 32, "circle.png", color=TEXT, condition="!" + focused),
        image(0, 0, 32, 32, "circle.png", color=BLACK, condition=focused),
        label(0, 0, 32, 32, "$INFO[{}]".format(count), "font_flag", BLACK, "center", control_id=526,
              condition="!" + focused),
        label(0, 0, 32, 32, "$INFO[{}]".format(count), "font_flag", TEXT, "center", condition=focused),
    ], condition="!String.IsEmpty({})".format(count))


def rail():
    """A-Z jump rail (Android TvAlphabetRail): dots while idle, letters while focused."""
    focused = "Control.HasFocus(560)"
    empty = "String.IsEmpty(ListItem.Property(silo.has))"

    def layout(tag, is_focused):
        parts = ['<{} width="56" height="28">'.format(tag),
                 image(24, 10, 8, 8, "circle.png", color="80EDEDED", condition="!{} + !{}".format(focused, empty)),
                 image(25, 11, 6, 6, "circle.png", color="26EDEDED", condition="!{} + {}".format(focused, empty))]
        if is_focused:
            parts += [image(4, 0, 48, 28, "round6.png", color=TEXT, border=6, condition=focused),
                      label(0, 0, 56, 28, "$INFO[ListItem.Label]", "font10", BLACK, "center", condition=focused)]
        else:
            # Letters without titles stay dimmed while the rail is expanded.
            parts += [label(0, 0, 56, 28, "$INFO[ListItem.Label]", "font10", SECONDARY, "center",
                            condition="{} + !{}".format(focused, empty)),
                      label(0, 0, 56, 28, "$INFO[ListItem.Label]", "font10", "4DEDEDED", "center",
                            condition="{} + {}".format(focused, empty))]
        parts.append("</{}>".format(tag))
        return "".join(parts)

    rail_list = "".join(['<control type="list" id="560">', rect(0, 0, 56, 784),
                         "<orientation>vertical</orientation>",
                         '<scrolltime tween="cubic" easing="out">120</scrolltime>',
                         nav(560, left=500), layout("itemlayout", False), layout("focusedlayout", True),
                         "</control>"])
    return group(565, 1856, 270, 56, 784, [
        image(-8, -8, 72, 800, "round16.png", color="E60A0A0A", border=16, condition=focused),
        rail_list,
    ], condition="!String.IsEmpty(Window.Property(silo.rail))")


def people_row():
    """Search People row: round photos (or initials) with names."""
    focused = "Control.HasFocus(570)"

    def layout(tag, is_focused):
        parts = ['<{} width="200" height="230">'.format(tag),
                 image(30, 10, 140, 140, "circle.png", color="2EFFFFFF"),
                 label(30, 10, 140, 140, "$INFO[ListItem.Property(silo.initials)]", "font36_title", TEXT, "center"),
                 image(30, 10, 140, 140, "$INFO[ListItem.Art(thumb)]", diffuse="mask_circle.png", aspect="scale",
                       background=True)]
        if is_focused:
            parts += [image(22, 2, 156, 156, "ring_circle.png", color=TEXT, condition=focused),
                      label(0, 164, 200, 40, "$INFO[ListItem.Label]", "font12", TEXT, "center", condition=focused,
                            scroll=True),
                      label(0, 164, 200, 40, "$INFO[ListItem.Label]", "font12", CAPTION_IDLE, "center",
                            condition="!" + focused)]
        else:
            parts.append(label(0, 164, 200, 40, "$INFO[ListItem.Label]", "font12", CAPTION_IDLE, "center"))
        parts.append("</{}>".format(tag))
        return "".join(parts)

    people = "".join(['<control type="list" id="570">', rect(48, 56, 1872, 230),
                      "<orientation>horizontal</orientation>",
                      '<scrolltime tween="cubic" easing="out">200</scrolltime>',
                      nav(570, up=530, down=500), layout("itemlayout", False), layout("focusedlayout", True),
                      "</control>"])
    return group(575, 0, 370, 1920, 290, [
        label(88, 0, 1600, 52, "People", "font30_title", TEXT),
        people,
    ], condition="!String.IsEmpty(Window.Property(silo.people))")


def browse():
    tab = "Window.Property(silo.tab)"
    grid = card_list(500, "panel", 40, 270, 1840, 810, 300, 538, 40, 40, 260, 390, "mask_grid.png", "poster",
                     False, 110, nav(500, up=510, right=560), orientation="vertical")
    chips = [part for index in range(len(LIBRARY_CHIPS)) for part in chip(index)]
    library = group(710, 0, 0, 1920, 1080, chips + [
        filter_badge(),
        label(1332, 188, 500, 56, "$INFO[Window.Property(silo.total)]", "font12", MUTED, "right", control_id=540),
        rail(),
    ], condition="String.IsEqual({0},movies) | String.IsEqual({0},series)".format(tab))
    chips, left = [], 960 - (sum(chip[3] for chip in SEARCH_CHIPS) + 16 * (len(SEARCH_CHIPS) - 1)) // 2
    for index, (control_id, kind, text, width) in enumerate(SEARCH_CHIPS):
        selected = "String.IsEqual(Window.Property(silo.search_type),{})".format(kind)
        chips += [image(left, 292, width, 56, "pill56_idle.png", border=28, condition="!" + selected),
                  image(left, 292, width, 56, "pill60_selected.png", border=30,
                        condition="{} + !Control.HasFocus({})".format(selected, control_id)),
                  button(control_id, left, 292, width, 56, text, focus="pill56.png", focus_border=28,
                         navigation=nav(control_id, up=520, down=500,
                                        left=SEARCH_CHIPS[index - 1][0] if index else None,
                                        right=SEARCH_CHIPS[index + 1][0] if index + 1 < len(SEARCH_CHIPS)
                                        else None))]
        left += width + 16
    query = "Window.Property(silo.query)"
    search = group(800, 0, 0, 1920, 1080, [
        button(520, 440, 164, 1040, 104, "", focus="field_focus.png", focus_border=30, idle="field.png",
               idle_border=30, navigation=nav(520, down=530)),
        image(476, 194, 44, 44, "icon_search.png", color=MUTED),
        label(544, 164, 900, 104, "$INFO[{}]".format(query), "font27", TEXT, condition="!String.IsEmpty({})"
              .format(query)),
        label(544, 164, 900, 104, "Search movies, series…", "font27", "8AEDEDED",
              condition="String.IsEmpty({})".format(query)),
    ] + chips + [people_row()], condition="String.IsEqual({},search)".format(tab))
    return group(700, 0, 0, 1920, 1080, [
        library, search, grid,
        label(88, 560, 1744, 60, "$INFO[Window.Property(silo.empty)]", "font27", MUTED, "center", control_id=550),
    ], condition="!String.IsEmpty({0}) + !String.IsEqual({0},home)".format(tab))


def dropdown(group_id, panel_id, left, top, width, items, condition, focus_border=16):
    """A panel.png dropdown of left-aligned rows; Python repositions rows and the panel height."""
    buttons = []
    for index, (control_id, text) in enumerate(items):
        up = items[index - 1][0] if index else None
        down = items[index + 1][0] if index + 1 < len(items) else None
        buttons.append(button(control_id, 16, 16 + 72 * index, width - 32, 64, text, focus="round16.png",
                              focus_border=focus_border, align="left", offset=24,
                              navigation=nav(control_id, up=up, down=down),
                              condition="String.IsEmpty(Window.Property(silo.profile_locked))"
                              if control_id == 610 else None))
    height = 32 + 72 * len(items) - 8
    return group(group_id, left, top, width, height, [image(0, 0, width, height, "panel.png", border=24,
                                                            control_id=panel_id)] + buttons, condition=condition)


def menu():
    return dropdown(600, 601, 1352, 128, 480, MENU_ITEMS, "!String.IsEmpty(Window.Property(silo.menu))")


def pill():
    """Library tab dropdown (Android TvLibraryPill); Python moves it under the Movies or Series tab."""
    return dropdown(640, 641, 0, 128, 320, PILL_ITEMS, "!String.IsEmpty(Window.Property(silo.pill))")


def window():
    controls = [image(0, 0, 1920, 1080, "white.png", color=BLACK), home(), browse(), top_bar(), menu(), pill()]
    body = "".join([
        "<window>",
        '<defaultcontrol always="false">210</defaultcontrol>',
        "<backgroundcolor>FF000000</backgroundcolor>",
        "<coordinates><left>0</left><top>0</top></coordinates>",
        "<controls>",
    ] + controls + ["</controls>", "</window>"])
    pretty = minidom.parseString(body).documentElement.toprettyxml(indent="  ")
    return "\n".join(['<?xml version="1.0" encoding="UTF-8"?>',
                      "<!-- Generated by scripts/skin_home.py; edit the generator, not this file. -->",
                      pretty.rstrip(), ""])


def main():
    OUT.write_text(window(), encoding="utf-8")
    print("wrote", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
