#!/usr/bin/env python3
"""Write SiloRequests.xml and SiloRequestsDetail.xml in resources/skins/Default/1080i/.

The Requests hub repeats one row slot many times, so its XML is generated here
and committed. Control ids must match resources/lib/silo/ui_requests.py.

Run: python3 scripts/skin_requests.py
"""

from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default/1080i"

TEXT, SECONDARY, MUTED, CAPTION_IDLE = "FFEDEDED", "BFEDEDED", "B3EDEDED", "C7EDEDED"
BLACK, SURFACE, DETAIL_BACKGROUND = "FF000000", "FF15171C", "FF040D0F"
SLOTS, SLOT_BASE = 10, 3000
# Status dot colours; keep in step with svc_requests.TINTS.
TINTS = (("pending", "FFE8B04B"), ("on_the_way", "FF6CB4E8"), ("available", "FF58C48E"),
         ("declined", "FFE86A82"), ("failed", "FFE86A82"), ("unavailable", "9EEDEDED"))
FOCUS_ZOOM = 'effect="zoom" start="100" end="110" time="200" tween="cubic" easing="out" center="auto"'


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


def image(left, top, width, height, name, color=None, border=None, diffuse=None, aspect=None, condition=None,
          fade=None, background=False):
    parts = ['<control type="image">', rect(left, top, width, height)]
    if aspect:
        parts.append("<aspectratio>{}</aspectratio>".format(aspect))
    if fade:
        parts.append("<fadetime>{}</fadetime>".format(fade))
    parts += [texture(name, color=color, border=border, diffuse=diffuse, background=background),
              visible(condition), "</control>"]
    return "".join(parts)


def label(left, top, width, height, text, font, color, align="left", condition=None, control_id=None,
          scroll=False, kind="label", aligny=None):
    parts = ['<control type="{}"{}>'.format(kind, ' id="{}"'.format(control_id) if control_id else ""),
             rect(left, top, width, height), "<font>{}</font>".format(font),
             "<textcolor>{}</textcolor>".format(color), "<align>{}</align>".format(align),
             "<aligny>{}</aligny>".format(aligny or ("top" if kind == "textbox" else "center"))]
    if scroll:
        parts.append("<scroll>true</scroll>")
    parts += ["<label>{}</label>".format(esc(text)), visible(condition), "</control>"]
    return "".join(parts)


def nav(control_id, up=None, down=None, left=None, right=None):
    """Navigation tags; a missing direction points back at the control itself."""
    return "".join("<{0}>{1}</{0}>".format(tag, target or control_id)
                   for tag, target in (("onup", up), ("ondown", down), ("onleft", left), ("onright", right)))


def button(control_id, left, top, width, height, text, focus, focus_border, idle, idle_border, align="center",
           offset=0, navigation=""):
    return "".join(['<control type="button" id="{}">'.format(control_id), rect(left, top, width, height),
                    "<label>{}</label>".format(esc(text)), "<font>font25_title</font>",
                    "<textcolor>{}</textcolor><focusedcolor>{}</focusedcolor>".format(TEXT, BLACK),
                    "<align>{}</align><aligny>center</aligny>".format(align),
                    "<textoffsetx>{}</textoffsetx>".format(offset) if offset else "",
                    texture(focus, "texturefocus", color=TEXT, border=focus_border),
                    texture(idle, "texturenofocus", border=idle_border), navigation, "</control>"])


def group(control_id, left, top, width, height, children, condition=None, animation=""):
    head = '<control type="group"{}>'.format(' id="{}"'.format(control_id) if control_id else "")
    return "".join([head, rect(left, top, width, height), animation, visible(condition)] + children
                   + ["</control>"])


def status_dots(left, top, size, prop):
    return [image(left, top, size, size, "circle.png", color=color,
                  condition="String.IsEqual({},{})".format(prop, key)) for key, color in TINTS]


# ---------------------------------------------------------------- cards

def card_layout(tag, list_id, item_width, item_height, offset_x, offset_y, width, height, focused, captions=2):
    has_focus = "Control.HasFocus({})".format(list_id)
    art = []
    if focused:
        art.append(image(-24, -24, width + 48, height + 48, "glow16.png", border=48, condition=has_focus))
    status = "ListItem.Property(silo.status_key)"
    art += [image(0, 0, width, height, "round16.png", color=SURFACE, border=16),
            label(12, 0, width - 24, height, "$INFO[ListItem.Label]", "font12", MUTED, "center", kind="textbox",
                  aligny="center"),
            image(0, 0, width, height, "$INFO[ListItem.Art(poster)]", diffuse="mask_poster.png", aspect="scale",
                  background=True),
            image(10, 10, 30, 30, "circle.png", color="B3000000", condition="!String.IsEmpty({})".format(status))]
    art += status_dots(17, 17, 16, status)
    if focused:
        art.append(image(-4, -4, width + 8, height + 8, "ring16.png", color=TEXT, border=24, condition=has_focus))
    animation = ('<animation {} condition="{}">Conditional</animation>'.format(FOCUS_ZOOM, has_focus)
                 if focused else "")
    caption_top = offset_y + height + 14
    parts = ['<{} width="{}" height="{}">'.format(tag, item_width, item_height),
             group(None, offset_x, offset_y, width, height, art, animation=animation)]
    if focused:
        parts += [label(offset_x, caption_top, width, 40, "$INFO[ListItem.Label]", "font27", TEXT,
                        condition=has_focus, scroll=True),
                  label(offset_x, caption_top, width, 40, "$INFO[ListItem.Label]", "font27", CAPTION_IDLE,
                        condition="!" + has_focus)]
    else:
        parts.append(label(offset_x, caption_top, width, 40, "$INFO[ListItem.Label]", "font27", CAPTION_IDLE))
    if captions > 1:
        parts.append(label(offset_x, caption_top + 40, width, 34, "$INFO[ListItem.Label2]", "font12", MUTED))
    parts.append("</{}>".format(tag))
    return "".join(parts)


def poster_list(list_id, left, top, width, height, item_width, item_height, offset_x, offset_y, card_width,
                card_height, navigation, captions=2):
    layouts = [card_layout(tag, list_id, item_width, item_height, offset_x, offset_y, card_width, card_height,
                           tag == "focusedlayout", captions)
               for tag in ("itemlayout", "focusedlayout")]
    return "".join(['<control type="fixedlist" id="{}">'.format(list_id), rect(left, top, width, height),
                    "<orientation>horizontal</orientation><focusposition>0</focusposition>",
                    '<scrolltime tween="cubic" easing="out">200</scrolltime><preloaditems>2</preloaditems>',
                    navigation] + layouts + ["</control>"])


# ---------------------------------------------------------------- hub

def row_slot(index):
    base = SLOT_BASE + 10 * index
    list_id = base + 2
    return group(base, 0, 560, 1920, 450, [
        label(88, 0, 1600, 52, "", "font30_title", TEXT, control_id=base + 1),
        poster_list(list_id, 48, 44, 1872, 406, 216, 406, 40, 44, 176, 264, nav(list_id)),
    ])


def hub():
    query = "Window.Property(silo.query)"
    status = "Window.Property(silo.preview.status_key)"
    search = [
        button(110, 1240, 56, 592, 64, "", "field_focus.png", 30, "field.png", 30, navigation=nav(110)),
        image(1264, 72, 32, 32, "icon_search.png", color=MUTED),
        label(1312, 56, 500, 64, "$INFO[{}]".format(query), "font27", TEXT,
              condition="!String.IsEmpty({})".format(query)),
        label(1312, 56, 500, 64, "Search movies and series", "font27", "8AEDEDED",
              condition="String.IsEmpty({})".format(query)),
        label(1240, 124, 592, 36, "$INFO[Window.Property(silo.search_note)]", "font10", MUTED, "right",
              control_id=151),
    ]
    preview = group(400, 0, 0, 1920, 560, [
        label(88, 180, 1100, 150, "$INFO[Window.Property(silo.preview.title)]", "font_MainMenu", TEXT,
              kind="textbox", aligny="bottom"),
        label(88, 346, 1100, 36, "$INFO[Window.Property(silo.preview.meta)]", "font12", SECONDARY),
        label(88, 392, 780, 84, "$INFO[Window.Property(silo.preview.synopsis)]", "font27", MUTED, kind="textbox"),
    ] + status_dots(88, 497, 14, status) + [
        label(114, 484, 900, 40, "$INFO[Window.Property(silo.preview.status)]", "font12", TEXT),
    ])
    return [
        image(0, 0, 1920, 1080, "white.png", color=BLACK),
        image(691, 0, 1229, 756, "$INFO[Window.Property(silo.preview.backdrop)]", diffuse="mask_hero.png",
              aspect="scale", fade=400, background=True),
        image(0, 0, 1920, 240, "scrim_top.png", color=BLACK),
        group(None, 0, 56, 1920, 64, [
            image(88, 12, 40, 40, "brand_icon.png", aspect="keep"),
            label(142, 0, 346, 64, "[COLOR FFE8CF8F]FOUNDERS[/COLOR] THEATER", "font25_title", "FFFFFFFF"),
            label(480, 0, 400, 64, "Requests", "font36_title", MUTED),
            label(1560, 0, 272, 64, "$INFO[System.Time]", "font25_title", SECONDARY, "right"),
        ]),
    ] + search + [preview] + [row_slot(index) for index in range(SLOTS)] + [
        label(88, 620, 1744, 60, "$INFO[Window.Property(silo.empty)]", "font27", MUTED, "center", control_id=150),
    ]


# ---------------------------------------------------------------- detail

def detail():
    status = "Window.Property(silo.status_key)"
    buttons = [button(control_id, 100 + 260 * index, 556, 240, 76, "", "pill76.png", 38, "pill76_idle.png", 38,
                      navigation=nav(control_id, down=500))
               for index, control_id in enumerate((300, 303, 301, 302))]
    return [
        image(0, 0, 1920, 1080, "white.png", color=DETAIL_BACKGROUND),
        image(691, 0, 1229, 756, "$INFO[Window.Property(silo.backdrop)]", diffuse="mask_details.png",
              aspect="scale", fade=400, background=True),
        label(100, 104, 1080, 40, "$INFO[Window.Property(silo.eyebrow)]", "font25_title", SECONDARY),
        label(100, 144, 1080, 140, "$INFO[Window.Property(silo.title)]", "font_MainMenu", TEXT, kind="textbox",
              aligny="bottom"),
        label(100, 294, 1080, 36, "$INFO[Window.Property(silo.meta)]", "font12", "E0EDEDED"),
        label(100, 346, 1080, 114, "$INFO[Window.Property(silo.plot)]", "font27", "D9EDEDED", kind="textbox"),
        label(100, 466, 1080, 32, "$INFO[Window.Property(silo.credit)]", "font10", "9EEDEDED"),
    ] + status_dots(100, 515, 14, status) + [
        label(126, 502, 1054, 40, "$INFO[Window.Property(silo.status)]", "font12", TEXT),
    ] + buttons + [
        label(100, 644, 1080, 32, "$INFO[Window.Property(silo.hint)]", "font12", SECONDARY),
        label(100, 700, 1600, 52, "More like this", "font30_title", TEXT, control_id=501,
              condition="!String.IsEmpty(Window.Property(silo.has_recommendations))"),
        poster_list(500, 60, 744, 1860, 336, 180, 336, 40, 24, 140, 210, nav(500, up=300), captions=1),
    ]


def window(controls, default, background):
    body = "".join(["<window>", '<defaultcontrol always="false">{}</defaultcontrol>'.format(default),
                    "<backgroundcolor>{}</backgroundcolor>".format(background),
                    "<coordinates><left>0</left><top>0</top></coordinates>", "<controls>"]
                   + controls + ["</controls>", "</window>"])
    pretty = minidom.parseString(body).documentElement.toprettyxml(indent="  ")
    return "\n".join(['<?xml version="1.0" encoding="UTF-8"?>',
                      "<!-- Generated by scripts/skin_requests.py; edit the generator, not this file. -->",
                      pretty.rstrip(), ""])


def windows():
    return {"SiloRequests.xml": window(hub(), 110, BLACK),
            "SiloRequestsDetail.xml": window(detail(), 300, DETAIL_BACKGROUND)}


def main():
    for name, text in windows().items():
        (SKIN / name).write_text(text, encoding="utf-8")
        print("wrote", (SKIN / name).relative_to(ROOT))


if __name__ == "__main__":
    main()
