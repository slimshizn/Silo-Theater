"""Check the static source page contract used by Kodi's HTML source browser."""

from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urljoin, urlsplit


BASE_URL = "https://slimshizn.github.io/Silo-Theater/"
INSTALLER = "repository.silo.theater-1.0.0.zip"

# Kodi 21 HTTPDirectory.cpp matches this pattern with case-insensitive DOTALL.
# https://github.com/xbmc/xbmc/blob/Omega/xbmc/filesystem/HTTPDirectory.cpp
KODI_DIRECTORY_LINK = re.compile(
    r'<a href="([^"]*)"[^>]*>\s*(.*?)\s*</a>(.+?)(?=<a|</tr|$)',
    re.IGNORECASE | re.DOTALL | re.ASCII,
)


def kodi_zip_links(page):
    """Apply Kodi's link/name acceptance rules to this site's ZIP entries."""
    result = []
    for href, label, metadata in KODI_DIRECTORY_LINK.findall(page):
        path = href.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        name = unescape(label.strip()).rstrip("/")
        target = unquote(unescape(path).rstrip("/"))
        if target not in ("", "..") and name == target and path.lower().endswith(".zip"):
            result.append(urljoin(BASE_URL, href))
    return result


class SourcePageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active_link = None
        self.ids = set()
        self.dependencies = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        if tag == "a":
            self.active_link = {"href": attributes.get("href", ""), "text": ""}
        if tag in ("script", "iframe") or tag == "link" and attributes.get("rel") == "stylesheet":
            self.dependencies.append((tag, attributes))

    def handle_endtag(self, tag):
        if tag == "a" and self.active_link is not None:
            self.links.append(self.active_link)
            self.active_link = None

    def handle_data(self, data):
        if self.active_link is not None:
            self.active_link["text"] += data


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).resolve().parents[1] / "site/index.html").read_text(encoding="utf-8")
        self.page = SourcePageParser()
        self.page.feed(self.html)

    def test_installer_link_is_available_without_javascript(self):
        installers = [link for link in self.page.links if link["href"] == INSTALLER]
        self.assertEqual(len(installers), 1)
        self.assertEqual(installers[0]["text"], INSTALLER)
        self.assertEqual(urljoin(BASE_URL, installers[0]["href"]), BASE_URL + INSTALLER)
        self.assertFalse(self.page.dependencies)

    def test_kodi_http_directory_lists_the_installer(self):
        self.assertEqual(kodi_zip_links(self.html), [BASE_URL + INSTALLER])

    def test_browser_valid_attribute_order_can_still_hide_the_kodi_installer(self):
        invalid = '<a class="installer" href="{0}">{0}</a>\n'.format(INSTALLER)
        browser = SourcePageParser()
        browser.feed(invalid)
        self.assertEqual(browser.links[0]["href"], INSTALLER)
        self.assertEqual(kodi_zip_links(invalid), [])

    def test_shown_source_is_exact_and_navigation_targets_exist(self):
        self.assertTrue(any(link["href"] == BASE_URL and link["text"] == BASE_URL for link in self.page.links))
        for link in self.page.links:
            if link["href"].startswith("#"):
                self.assertIn(link["href"][1:], self.page.ids)
            else:
                destination = urlsplit(urljoin(BASE_URL, link["href"]))
                self.assertEqual(destination.scheme, "https")
                self.assertTrue(destination.hostname)


if __name__ == "__main__":
    unittest.main()
