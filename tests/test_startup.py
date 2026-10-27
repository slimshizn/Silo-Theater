"""Opening Founders Theater when Kodi starts (resources/lib/silo/startup.py)."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo import startup


class Kodi:
    """A Kodi double whose screen and player state follow a timeline in seconds."""

    def __init__(self, settings=None, screens=((0, "startup"), (2, "home")), playing_from=None,
                 dialog_until=0, abort_at=None, handled=False):
        self.now = 0.0
        self.settings = {"open_on_start": "true"}
        self.settings.update(settings or {})
        self.screens = screens
        self.playing_from = playing_from
        self.dialog_until = dialog_until
        self.abort_at = abort_at
        self.properties = {startup.STARTED_PROPERTY: "1"} if handled else {}
        self.builtins = []
        test = self

        class Monitor:
            def waitForAbort(self, seconds):
                test.now += seconds
                return test.abort_at is not None and test.now >= test.abort_at

        class Window:
            def __init__(self, window_id):
                assert window_id == startup.HOME_WINDOW

            def getProperty(self, name):
                return test.properties.get(name, "")

            def setProperty(self, name, value):
                test.properties[name] = value

        self.xbmc = SimpleNamespace(Monitor=Monitor, getCondVisibility=self.condition,
                                    executebuiltin=self.builtins.append, log=lambda message, level=0: None,
                                    LOGINFO=1)
        self.xbmcgui = SimpleNamespace(Window=Window)
        self.addon = SimpleNamespace(getSetting=lambda name: self.settings.get(name, ""))

    def screen(self):
        current = None
        for start, name in self.screens:
            if self.now >= start:
                current = name
        return current

    def condition(self, name):
        if name == "Player.HasMedia":
            return self.playing_from is not None and self.now >= self.playing_from
        if name == "System.HasActiveModalDialog":
            return self.now < self.dialog_until
        if name.startswith("Window.IsActive(") and name.endswith(")"):
            return self.screen() == name[len("Window.IsActive("):-1]
        raise AssertionError("Unexpected condition " + name)

    def run(self):
        return startup.run(self.xbmc, self.xbmcgui, self.addon, clock=lambda: self.now)


OPEN = "RunScript(script.silo)"


class StartupTests(unittest.TestCase):
    def test_off_by_default_never_opens(self):
        kodi = Kodi(settings={"open_on_start": ""})
        self.assertEqual(kodi.run(), "off")
        self.assertEqual(kodi.builtins, [])
        self.assertEqual(kodi.properties[startup.STARTED_PROPERTY], "1")

    def test_on_opens_once_kodi_reaches_home(self):
        kodi = Kodi()
        self.assertEqual(kodi.run(), "opened")
        self.assertEqual(kodi.builtins, [OPEN])
        self.assertGreaterEqual(kodi.now, 2)  # Waited out the startup screen.

    def test_only_once_per_kodi_session(self):
        # Kodi restarts services when an add-on is updated; the session marker stops a second launch.
        kodi = Kodi(handled=True)
        self.assertEqual(kodi.run(), "already_handled")
        self.assertEqual(kodi.builtins, [])

    def test_delay_waits_before_opening(self):
        kodi = Kodi(settings={"open_delay": "10"})
        self.assertEqual(kodi.run(), "opened")
        self.assertGreaterEqual(kodi.now, 10)

    def test_unknown_delay_falls_back_to_none(self):
        kodi = Kodi(settings={"open_delay": "7"})
        self.assertEqual(kodi.run(), "opened")
        self.assertLess(kodi.now, 7)

    def test_any_screen_is_the_default(self):
        kodi = Kodi(screens=((0, "startup"), (1, "videos")))
        self.assertEqual(kodi.run(), "opened")
        self.assertEqual(kodi.builtins, [OPEN])

    def test_home_only_waits_for_home_then_gives_up(self):
        kodi = Kodi(settings={"open_on_any_screen": "false"}, screens=((0, "startup"), (1, "videos")))
        self.assertEqual(kodi.run(), "not_home")
        self.assertEqual(kodi.builtins, [])
        self.assertGreaterEqual(kodi.now, startup.WAIT_SECONDS)

    def test_home_only_opens_on_home(self):
        kodi = Kodi(settings={"open_on_any_screen": "false"})
        self.assertEqual(kodi.run(), "opened")

    def test_never_opens_over_playback(self):
        kodi = Kodi(playing_from=0)
        self.assertEqual(kodi.run(), "playing")
        self.assertEqual(kodi.builtins, [])

    def test_playback_that_starts_during_the_delay_still_wins(self):
        kodi = Kodi(settings={"open_delay": "5"}, playing_from=3)
        self.assertEqual(kodi.run(), "playing")
        self.assertEqual(kodi.builtins, [])

    def test_waits_for_a_startup_dialog_to_close(self):
        kodi = Kodi(dialog_until=6)
        self.assertEqual(kodi.run(), "opened")
        self.assertGreaterEqual(kodi.now, 6)

    def test_kodi_shutting_down_stops_the_wait(self):
        kodi = Kodi(screens=((0, "startup"),), abort_at=3)
        self.assertEqual(kodi.run(), "aborted")
        self.assertEqual(kodi.builtins, [])
        kodi = Kodi(settings={"open_delay": "20"}, abort_at=3)
        self.assertEqual(kodi.run(), "aborted")

    def test_stuck_on_the_startup_screen_gives_up(self):
        kodi = Kodi(screens=((0, "startup"),))
        self.assertEqual(kodi.run(), "not_ready")
        self.assertEqual(kodi.builtins, [])


if __name__ == "__main__":
    unittest.main()
