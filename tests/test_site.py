"""Check the static source page contract used by Kodi's HTML source browser."""

from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import urljoin, urlsplit


BASE_URL = "https://slimshizn.github.io/Silo-Theater/"
INSTALLER = "repository.silo.theater-1.0.0.zip"


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

    def test_kodi_can_discover_repository_installer_without_javascript(self):
        installers = [link for link in self.page.links if link["href"] == INSTALLER]
        self.assertEqual(len(installers), 1)
        self.assertEqual(installers[0]["text"], INSTALLER)
        self.assertEqual(urljoin(BASE_URL, installers[0]["href"]), BASE_URL + INSTALLER)
        self.assertFalse(self.page.dependencies)

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
