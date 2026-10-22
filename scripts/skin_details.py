#!/usr/bin/env python3
"""Write resources/skins/Default/1080i/SiloDetails.xml.

The details screen repeats the same button and list shapes many times, and
the action row's left/right links must skip whichever buttons are hidden.
Generating the XML keeps those links consistent with the Python layout in
resources/lib/silo/ui_details.py. Commit the generated file too.

Run: python3 scripts/skin_details.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "resources/skins/Default/1080i/SiloDetails.xml"

TEXT = "FFEDEDED"
SECONDARY = "BFEDEDED"
MUTED = "B3EDEDED"
DIM = "9EEDEDED"
PLACEHOLDER = "FF15171C"


def prop(name):
    return "Window.Property({})".format(name)


def has(name):
    return "!String.IsEmpty({})".format(prop(name))


def label(left, top, width, height, text, font, color, extra="", control_id=None):
    ident = ' id="{}"'.format(control_id) if control_id else ""
    return ('<control type="label"{}><left>{}</left><top>{}</top><width>{}</width><height>{}</height>'
            '<font>{}</font><textcolor>{}</textcolor><label>{}</label>{}</control>').format(
                ident, left, top, width, height, font, color, text, extra)


def image(left, top, width, height, texture, extra="", control_id=None):
    ident = ' id="{}"'.format(control_id) if control_id else ""
    return ('<control type="image"{}><left>{}</left><top>{}</top><width>{}</width><height>{}</height>'
            '{}{}</control>').format(ident, left, top, width, height, texture, extra)


def visible(condition):
    return "<visible>{}</visible>".format(condition)


# ------------------------------------------------------------------ buttons
# (id, kind, icon, visible, enabled). Order is the on-screen order; the Python
# layout (ui_details.BUTTON_ORDER) uses the same order.
ENABLED = has("silo.has_file")
BUTTONS = (
    (105, "capsule", "icon_play.png", has("silo.continue"), None),
    (100, "capsule", "icon_play.png", has("silo.playable"), ENABLED),
    (101, "circle", "icon_resume.png", has("silo.resume_button"), ENABLED),
    (104, "circle", "icon_restart.png", has("silo.resume"), ENABLED),
    (103, "circle", "icon_versions.png", has("silo.multi_version"), ENABLED),
    (106, "circle", "icon_subtitles.png", has("silo.playable") + " + " + has("silo.has_subtitles"), None),
)
BUTTON_LABELS = {105: "silo.continue_label", 100: "silo.play_label"}


def reachable(control_id):
    return "Control.IsVisible({0}) + Control.IsEnabled({0})".format(control_id)


def nav(control_id, direction, candidates):
    links = "".join('<{0} condition="{1}">{2}</{0}>'.format(direction, reachable(target), target)
                    for target in candidates)
    return links + "<{0}>{1}</{0}>".format(direction, control_id)


def button(index):
    control_id, kind, icon, show, enable = BUTTONS[index]
    order = [entry[0] for entry in BUTTONS]
    links = (nav(control_id, "onleft", reversed(order[:index])) + nav(control_id, "onright", order[index + 1:])
             + "<onup>{0}</onup><ondown>{0}</ondown>".format(control_id))
    gating = visible(show) + ("<enable>{}</enable>".format(enable) if enable else "")
    if kind == "capsule":
        width, body = 240, (
            '<label>$INFO[Window.Property({})]</label><font>font25_title</font><textoffsetx>74</textoffsetx>'
            '<align>left</align><aligny>center</aligny>'
            '<textcolor>{}</textcolor><focusedcolor>FF000000</focusedcolor><disabledcolor>66EDEDED</disabledcolor>'
            '<texturefocus border="38" colordiffuse="{}">pill76.png</texturefocus>'
            '<texturenofocus border="38">pill76_idle.png</texturenofocus>').format(
                BUTTON_LABELS[control_id], TEXT, TEXT)
        icon_left = 24
    else:
        width, body = 76, (
            '<label></label><texturefocus colordiffuse="{}">circle.png</texturefocus>'
            '<texturenofocus>circle_idle.png</texturenofocus>').format(TEXT)
        icon_left = 18
    focused = "Control.HasFocus({})".format(control_id)
    return "\n".join((
        '      <control type="group" id="{}"><left>100</left><top>556</top><width>{}</width><height>76</height>{}'
        .format(1300 + control_id, width, visible(show)),
        '        <control type="button" id="{}"><left>0</left><top>0</top><width>{}</width><height>76</height>'
        '{}{}{}</control>'.format(control_id, width, body, links, gating),
        "        " + image(icon_left, 18, 40, 40, "<texture>{}</texture>".format(icon), visible("!" + focused)),
        "        " + image(icon_left, 18, 40, 40, '<texture colordiffuse="FF000000">{}</texture>'.format(icon),
                           visible(focused)),
        "      </control>",
    ))


# -------------------------------------------------------------------- lists
def self_links(control_id):
    return "".join("<{0}>{1}</{0}>".format(direction, control_id)
                   for direction in ("onleft", "onright", "onup", "ondown"))


def season_list():
    focused = "Control.HasFocus(510)"
    capsule = '<texture border="26"{}>{}</texture>'
    caption = "$INFO[ListItem.Label]"
    centered = "<align>center</align><aligny>center</aligny>"
    idle = "".join((
        image(8, 6, 180, 52, capsule.format("", "pill52_idle.png")),
        label(20, 6, 156, 52, caption, "font25_title", SECONDARY, centered),
    ))
    selected = "".join((
        image(8, 6, 180, 52, capsule.format("", "pill52_selected.png"), visible("!" + focused)),
        image(8, 6, 180, 52, capsule.format(' colordiffuse="{}"'.format(TEXT), "pill52.png"), visible(focused)),
        label(20, 6, 156, 52, caption, "font25_title", TEXT, centered + visible("!" + focused)),
        label(20, 6, 156, 52, caption, "font25_title", "FF000000", centered + visible(focused)),
    ))
    return (
        '      <control type="group"><left>0</left><top>0</top>{}\n'
        '        <control type="list" id="510"><left>92</left><top>684</top><width>1828</width><height>64</height>'
        '<orientation>horizontal</orientation><scrolltime tween="cubic" easing="out">200</scrolltime>{}\n'
        '          <itemlayout width="196" height="64">{}</itemlayout>\n'
        '          <focusedlayout width="196" height="64">{}</focusedlayout>\n'
        '        </control>\n      </control>'
    ).format(visible(has("silo.has_seasons")), self_links(510), idle, selected)


def episode_card(focused):
    has_focus = "Control.HasFocus(520)"
    parts = [
        image(8, 8, 360, 202, '<texture border="16" colordiffuse="{}">round16.png</texture>'.format(PLACEHOLDER)),
        image(8, 8, 360, 202, '<texture diffuse="mask_wide.png">$INFO[ListItem.Art(thumb)]</texture>'
              '<aspectratio>scale</aspectratio>'),
        '<control type="progress"><left>24</left><top>186</top><width>328</width><height>8</height>'
        '<texturebg colordiffuse="4DFFFFFF">white.png</texturebg><lefttexture></lefttexture>'
        '<midtexture colordiffuse="{}">white.png</midtexture><righttexture></righttexture>'
        '<info>ListItem.PercentPlayed</info>{}</control>'.format(
            TEXT, visible("!String.IsEmpty(ListItem.Property(silo.progress))")),
        image(320, 20, 36, 36, '<texture colordiffuse="CC000000">circle.png</texture>',
              visible("String.IsEqual(ListItem.Property(silo.watched),true)")),
        image(326, 26, 24, 24, "<texture>icon_check.png</texture>",
              visible("String.IsEqual(ListItem.Property(silo.watched),true)")),
    ]
    if focused:
        parts.append(image(4, 4, 368, 210, '<texture border="24" colordiffuse="E6FFFFFF">ring16.png</texture>',
                           visible(has_focus)))
        parts.append(label(8, 220, 360, 38, "$INFO[ListItem.Label]", "font27", TEXT, "<scroll>true</scroll>"))
    else:
        parts.append(label(8, 220, 360, 38, "$INFO[ListItem.Label]", "font27", SECONDARY))
    parts.append(label(8, 258, 360, 30, "$INFO[ListItem.Label2]", "font12", MUTED))
    return "".join(parts)


def episode_list():
    return (
        '      <control type="group"><left>0</left><top>0</top>{}\n'
        '        <control type="fixedlist" id="520"><left>92</left><top>756</top><width>1828</width>'
        '<height>300</height><orientation>horizontal</orientation><focusposition>0</focusposition>'
        '<movement>0</movement><scrolltime tween="cubic" easing="out">200</scrolltime>{}\n'
        '          <itemlayout width="400" height="300">{}</itemlayout>\n'
        '          <focusedlayout width="400" height="300">{}</focusedlayout>\n'
        '        </control>\n      </control>'
    ).format(visible(has("silo.has_episodes")), self_links(520), episode_card(False), episode_card(True))


def cast_person(focused):
    parts = [
        image(20, 12, 200, 200, '<texture colordiffuse="{}">circle.png</texture>'.format(PLACEHOLDER)),
        label(20, 12, 200, 200, "$INFO[ListItem.Property(silo.initials)]", "font36_title", MUTED,
              "<align>center</align><aligny>center</aligny>" + visible("String.IsEmpty(ListItem.Art(thumb))")),
        image(20, 12, 200, 200, '<texture diffuse="mask_circle.png">$INFO[ListItem.Art(thumb)]</texture>'
              '<aspectratio>scale</aspectratio>'),
    ]
    if focused:
        parts.append(image(12, 4, 216, 216, "<texture>ring_circle.png</texture>", visible("Control.HasFocus(530)")))
    parts.append(label(0, 226, 240, 34, "$INFO[ListItem.Label]", "font12", TEXT,
                       "<align>center</align>" + ("<scroll>true</scroll>" if focused else "")))
    parts.append(label(0, 260, 240, 30, "$INFO[ListItem.Label2]", "font10", MUTED, "<align>center</align>"))
    return "".join(parts)


def cast_section():
    return (
        '      <control type="group" id="3000"><left>0</left><top>1100</top><width>1920</width><height>380</height>{}\n'
        "        {}\n"
        '        <control type="list" id="530"><left>80</left><top>56</top><width>1840</width><height>310</height>'
        '<orientation>horizontal</orientation><scrolltime tween="cubic" easing="out">200</scrolltime>{}\n'
        '          <itemlayout width="240" height="310">{}</itemlayout>\n'
        '          <focusedlayout width="240" height="310">{}</focusedlayout>\n'
        '        </control>\n      </control>'
    ).format(visible(has("silo.has_cast")), label(100, 0, 1200, 50, "Cast &amp; Crew", "font36_title", TEXT),
             self_links(530), cast_person(False), cast_person(True))


def similar_card(focused):
    parts = [
        image(16, 28, 176, 264, '<texture border="16" colordiffuse="{}">round16.png</texture>'.format(PLACEHOLDER)),
        image(16, 28, 176, 264, '<texture diffuse="mask_poster.png">$INFO[ListItem.Art(poster)]</texture>'
              '<aspectratio>scale</aspectratio>'),
        '<control type="progress"><left>28</left><top>276</top><width>152</width><height>6</height>'
        '<texturebg colordiffuse="4DFFFFFF">white.png</texturebg><lefttexture></lefttexture>'
        '<midtexture colordiffuse="{}">white.png</midtexture><righttexture></righttexture>'
        '<info>ListItem.PercentPlayed</info>{}</control>'.format(
            TEXT, visible("!String.IsEmpty(ListItem.Property(silo.progress))")),
    ]
    if focused:
        has_focus = "Control.HasFocus(540)"
        zoom = ('<animation effect="zoom" start="100" end="110" time="200" tween="cubic" easing="out" '
                'center="104,160" condition="{}">Conditional</animation>').format(has_focus)
        parts = (['<control type="group"><left>0</left><top>0</top>{}'.format(zoom),
                  image(-8, 4, 224, 312, '<texture border="48">glow16.png</texture>', visible(has_focus))]
                 + parts
                 + [image(12, 24, 184, 272, '<texture border="24">ring16.png</texture>', visible(has_focus)),
                    "</control>"])
    parts.append(label(16, 304, 176, 36, "$INFO[ListItem.Label]", "font27", TEXT if focused else SECONDARY,
                       "<scroll>true</scroll>" if focused else ""))
    parts.append(label(16, 340, 176, 30, "$INFO[ListItem.Label2]", "font12", MUTED))
    return "".join(parts)


def similar_section():
    return (
        '      <control type="group" id="4000"><left>0</left><top>1500</top><width>1920</width><height>460</height>{}\n'
        "        {}\n"
        '        <control type="list" id="540"><left>84</left><top>40</top><width>1836</width><height>390</height>'
        '<orientation>horizontal</orientation><scrolltime tween="cubic" easing="out">200</scrolltime>{}\n'
        '          <itemlayout width="208" height="390">{}</itemlayout>\n'
        '          <focusedlayout width="208" height="390">{}</focusedlayout>\n'
        '        </control>\n      </control>'
    ).format(visible(has("silo.has_similar")), label(100, 0, 1200, 50, "More like this", "font36_title", TEXT),
             self_links(540), similar_card(False), similar_card(True))


# --------------------------------------------------------------------- hero
def hero():
    title_visible = visible("String.IsEmpty({})".format(prop("silo.logo")))
    badges = []
    for slot in range(5):
        name = "silo.badge.{}".format(slot)
        badges.append("      " + image(100, 294, 60, 36, '<texture border="8">badge.png</texture>',
                                       visible(has(name)), 1211 + slot))
        badges.append("      " + label(100, 294, 60, 36, "$INFO[{}]".format(prop(name)), "font_flag", TEXT,
                                       "<align>center</align><aligny>center</aligny>" + visible(has(name)),
                                       1221 + slot))
    rows = [
        "      " + image(691, 0, 1229, 756, '<texture diffuse="mask_details.png">$INFO[{}]</texture>'
                         '<aspectratio>scale</aspectratio><fadetime>400</fadetime>'.format(prop("silo.backdrop"))),
        "      " + image(100, 116, 650, 160, "<texture>$INFO[{}]</texture><aspectratio align=\"left\" "
                         "aligny=\"bottom\">keep</aspectratio><fadetime>200</fadetime>".format(prop("silo.logo")),
                         visible(has("silo.logo"))),
        "      " + label(100, 156, 1080, 40, "$INFO[{}]".format(prop("silo.supertitle")), "font25_title", SECONDARY,
                         visible(has("silo.supertitle")), 1101),
        "      " + label(100, 196, 1080, 80, "$INFO[{}]".format(prop("silo.title")), "font_MainMenu", TEXT,
                         "<aligny>center</aligny>" + title_visible, 1102),
        "      " + label(100, 226, 1080, 50, "$INFO[{}]".format(prop("silo.title2")), "font36_title", TEXT,
                         "<aligny>center</aligny>" + visible(has("silo.title2") + " + String.IsEmpty({})".format(
                             prop("silo.logo"))), 1103),
        "      " + image(100, 294, 80, 36, '<texture border="8">chip_outline.png</texture>',
                         visible(has("silo.rating")), 1201),
        "      " + label(100, 294, 80, 36, "$INFO[{}]".format(prop("silo.rating")), "font12", TEXT,
                         "<align>center</align><aligny>center</aligny>" + visible(has("silo.rating")), 1202),
    ] + badges + [
        "      " + label(100, 294, 1080, 36, "$INFO[{}]".format(prop("silo.meta")), "font12", "E0EDEDED",
                         "<aligny>center</aligny>", 1230),
        '      <control type="textbox" id="1240"><left>100</left><top>346</top><width>1080</width><height>114</height>'
        '<font>font27</font><textcolor>D9EDEDED</textcolor><label>$INFO[{}]</label>{}</control>'.format(
            prop("silo.plot"), visible(has("silo.plot"))),
        "      " + label(100, 466, 1080, 32, "$INFO[{}]".format(prop("silo.credit")), "font10", DIM, "", 1250),
        "      " + label(100, 644, 1080, 32, "$INFO[{}]".format(prop("silo.hint")), "font12", SECONDARY,
                         visible(has("silo.hint")), 1260),
    ]
    return "\n".join(rows)


def build():
    body = "\n".join([hero()] + [button(index) for index in range(len(BUTTONS))]
                     + [season_list(), episode_list(), cast_section(), similar_section()])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Generated by scripts/skin_details.py; edit the script, not this file. -->\n"
        "<window>\n"
        "  <defaultcontrol>100</defaultcontrol>\n"
        "  <backgroundcolor>FF040D0F</backgroundcolor>\n"
        "  <coordinates><system>1</system><width>1920</width><height>1080</height></coordinates>\n"
        "  <controls>\n"
        '    <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height>'
        '<texture colordiffuse="FF040D0F">white.png</texture></control>\n'
        "    <!-- Python moves this group to scroll the page between focus zones. -->\n"
        '    <control type="group" id="1000"><left>0</left><top>0</top><width>1920</width><height>2200</height>\n'
        "{}\n"
        "    </control>\n"
        "  </controls>\n"
        "</window>\n"
    ).format(body)


if __name__ == "__main__":
    OUTPUT.write_text(build(), encoding="utf-8")
    print("wrote", OUTPUT.relative_to(ROOT))
