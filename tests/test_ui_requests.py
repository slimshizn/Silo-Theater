"""Requests service and windows with Kodi stubbed, plus checks on the generated Requests skins."""

import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default"
ESTUARY_FONTS = {"font_MainMenu", "font52_title", "font36_title", "font30_title", "font25_title", "font27",
                 "font12", "font_flag", "font10"}


class FakeWindow:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        instance.properties = {}
        instance.controls = {}
        instance.focus = 0
        instance.closed = False
        return instance

    def getControl(self, control_id):
        if control_id not in self.controls:
            self.controls[control_id] = MagicMock(name="control{}".format(control_id))
            self.controls[control_id].getSelectedPosition.return_value = 0
        return self.controls[control_id]

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setFocusId(self, control_id):
        self.focus = control_id

    def getFocusId(self):
        return self.focus

    def close(self):
        self.closed = True

    def doModal(self):
        self.onInit()


class FakeListItem:
    def __init__(self, label="", label2=""):
        self.label, self.label2 = label, label2
        self.art, self.properties = {}, {}

    def setArt(self, art):
        self.art.update(art)

    def setProperty(self, key, value):
        self.properties[key] = value


KODI_MODULES = {
    "xbmc": types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4),
    "xbmcgui": types.SimpleNamespace(WindowXML=FakeWindow, WindowXMLDialog=FakeWindow, Dialog=MagicMock,
                                     ListItem=FakeListItem),
}
with patch.dict(sys.modules, KODI_MODULES):
    sys.modules.pop("resources.lib.silo.ui_requests", None)
    ui_requests = importlib.import_module("resources.lib.silo.ui_requests")
svc = importlib.import_module("resources.lib.silo.svc_requests")


class ApiError(Exception):
    pass


class Action:
    def __init__(self, action_id):
        self.action_id = action_id

    def getId(self):
        return self.action_id


UP, DOWN, LEFT, RIGHT, BACK, CONTEXT = 3, 4, 1, 2, 10, 117


def result(number, kind="movie", **extra):
    media = {"media_type": kind, "tmdb_id": number, "title": "Example {} {}".format(kind.title(), number),
             "year": 2021, "overview": "An example synopsis.", "poster_path": "/poster{}.jpg".format(number),
             "backdrop_path": "/backdrop{}.jpg".format(number), "vote_average": 7.25, "availability": "missing",
             "request": {"requestable": True, "following": False, "requested_by_viewer": False}}
    media.update(extra)
    return media


def record(number, state="pending", status="pending", kind="movie", **extra):
    value = {"id": "r{}".format(number), "media_type": kind, "tmdb_id": number,
             "title": "Example Request {}".format(number), "year": 2020, "status": status, "outcome": "active",
             "state": state, "created_at": "2026-01-02T03:04:05.000Z", "poster_path": "/r{}.jpg".format(number)}
    value.update(extra)
    return value


class FakeClient:
    """Answers the request routes from canned data and records every call."""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = {
            ("GET", "/requests/status"): {"state": "available", "allowed": True, "requests_enabled": True,
                                          "follow_supported": True, "season_requests_supported": True},
            ("GET", "/requests/mine"): {"items": [record(1), record(2, "declined", "pending", outcome="declined")],
                                        "page": {"has_more": False}},
            ("GET", "/requests/discover"): {"items": [
                {"key": "trending_movies", "title": "Trending Movies", "results": [result(10), result(11)]},
                {"key": "popular_series", "title": "Popular Series", "results": [result(20, "series")]},
                {"key": "empty", "title": "Empty", "results": []}]},
        }
        self.routes.update(routes or {})

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        answer = self.routes.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(params, body)
        return answer


def make_application(client=None):
    application = MagicMock()
    application.client = client or FakeClient()
    application.addon.getAddonInfo.return_value = str(ROOT)

    def guard(callback):
        try:
            return callback()
        except ApiError as exc:
            application.dialog.ok("Silo", str(exc))
            return None

    application.guard.side_effect = guard
    return application


def open_hub(application=None):
    window = ui_requests.RequestsWindow("SiloRequests.xml", str(ROOT), "Default", "1080i")
    window.application = application or make_application()
    window.preview_debouncer = MagicMock()
    window.onInit()
    return window


def open_detail(media, application=None, feature=None):
    window = ui_requests.RequestDetailWindow("SiloRequestsDetail.xml", str(ROOT), "Default", "1080i")
    window.application = application or make_application()
    window.media = media
    window.feature = feature if feature is not None else {"follow_supported": True,
                                                          "season_requests_supported": True}
    window.onInit()
    return window


def routes(*tables):
    merged = {}
    for table in tables:
        merged.update(table)
    return merged


def detail_route(document):
    return {("GET", "/requests/detail/{}/{}".format(document["media_type"], document["tmdb_id"])): document}


class EnabledTests(unittest.TestCase):
    def test_enabled_when_status_allows_this_profile(self):
        self.assertTrue(svc.enabled(FakeClient()))

    def test_disabled_when_off_or_not_allowed(self):
        for document in ({"state": "disabled", "allowed": False, "requests_enabled": False},
                         {"state": "available", "allowed": False, "requests_enabled": True},
                         {"state": "not_configured"}, None, []):
            self.assertFalse(svc.enabled(FakeClient({("GET", "/requests/status"): document})), document)

    def test_any_error_reads_as_disabled(self):
        for error in (ApiError("boom"), OSError("down"), ValueError("bad json")):
            self.assertFalse(svc.enabled(FakeClient({("GET", "/requests/status"): error})))


class ServiceTests(unittest.TestCase):
    def test_search_sends_query_and_keeps_movies_and_series(self):
        client = FakeClient({("GET", "/requests/search"): {"page": 1, "total_pages": 3, "results": [
            result(1), result(2, "series"), {"media_type": "audiobook", "tmdb_id": 3, "title": "Example Book"},
            {"media_type": "movie", "title": "No id"}, "junk"]}})
        response = svc.search(client, "  example ")
        self.assertEqual(client.calls[-1][3], {"q": "example", "media_type": "all", "page": 1})
        self.assertEqual([media["tmdb_id"] for media in response["results"]], [1, 2])
        self.assertEqual(response["total_pages"], 3)

    def test_blank_search_does_not_call_the_server(self):
        client = FakeClient()
        self.assertEqual(svc.search(client, "  ")["results"], [])
        self.assertEqual(client.calls, [])

    def test_discover_drops_empty_and_unsupported_rows(self):
        sections = svc.discover(FakeClient())
        self.assertEqual([section["key"] for section in sections], ["trending_movies", "popular_series"])
        self.assertEqual(sections[0]["title"], "Trending Movies")

    def test_discover_tolerates_missing_fields(self):
        client = FakeClient({("GET", "/requests/discover"): {"items": [{"results": [result(1)]}, {}]}})
        self.assertEqual(svc.discover(client)[0]["title"], "Discover")
        self.assertEqual(svc.discover(FakeClient({("GET", "/requests/discover"): None})), [])

    def test_movie_request_payload(self):
        client = FakeClient({("POST", "/requests"): {"id": "r9", "state": "pending"}})
        media = dict(result(949), imdb_id="tt0000001", tvdb_id=None)
        svc.create(client, media)
        self.assertEqual(client.calls[-1][2], {
            "media_type": "movie", "tmdb_id": 949, "title": "Example Movie 949", "year": 2021,
            "imdb_id": "tt0000001", "overview": "An example synopsis.", "poster_path": "/poster949.jpg",
            "backdrop_path": "/backdrop949.jpg"})

    def test_series_request_payload_with_seasons(self):
        client = FakeClient({("POST", "/requests"): {"id": "r9"}})
        svc.create(client, dict(result(5, "series"), tvdb_id=77), seasons=[3, 2, 2, 0, "x"])
        body = client.calls[-1][2]
        self.assertEqual(body["seasons"], [2, 3])
        self.assertEqual(body["tvdb_id"], 77)
        svc.create(client, result(6), seasons=[1])
        self.assertNotIn("seasons", client.calls[-1][2])

    def test_request_needs_a_title_and_id(self):
        with self.assertRaises(ValueError):
            svc.create(FakeClient(), {"media_type": "movie", "tmdb_id": 0, "title": "Example"})
        with self.assertRaises(ValueError):
            svc.create(FakeClient(), {"media_type": "book", "tmdb_id": 1, "title": "Example"})

    def test_follow_and_unfollow_routes(self):
        client = FakeClient({("PUT", "/requests/follows/series/20"): {"following": True}})
        self.assertEqual(svc.set_follow(client, result(20, "series"), True), {"following": True})
        svc.set_follow(client, result(20, "series"), False)
        self.assertEqual([call[:2] for call in client.calls],
                         [("PUT", "/requests/follows/series/20"), ("DELETE", "/requests/follows/series/20")])

    def test_mine_follows_cursors_and_stops(self):
        pages = {None: {"items": [record(1)], "page": {"has_more": True, "next_cursor": "next"}},
                 "next": {"items": [record(2), record(3, kind="audiobook")], "page": {"has_more": False}}}
        client = FakeClient({("GET", "/requests/mine"): lambda params, body: pages[params.get("cursor")]})
        self.assertEqual([entry["id"] for entry in svc.mine(client)], ["r1", "r2"])

    def test_cancel_quotes_the_id(self):
        client = FakeClient({("POST", "/requests/a%2Fb/cancel"): {"state": "cancelled"}})
        svc.cancel(client, "a/b")
        self.assertEqual(client.calls[-1][:2], ("POST", "/requests/a%2Fb/cancel"))

    def test_image_urls(self):
        self.assertEqual(svc.image_url("/p.jpg"), "https://image.tmdb.org/t/p/w500/p.jpg")
        self.assertEqual(svc.image_url("https://example.invalid/p.jpg"), "https://example.invalid/p.jpg")
        for bad in ("", None, "//example.invalid/p.jpg", "file:///etc/passwd", "/p.jpg|x", "p.jpg", 5):
            self.assertEqual(svc.image_url(bad), "", bad)

    def test_record_states(self):
        cases = [(record(1), "pending", "Pending"),
                 (record(1, "approved", "queued"), "on_the_way", "Queued"),
                 (record(1, "processing", "downloading"), "on_the_way", "Downloading"),
                 (record(1, "partially_available", "completed"), "on_the_way", "Partly in library"),
                 (record(1, "available", "completed"), "available", "In library"),
                 (record(1, "declined", "pending", outcome_reason="Not this year"), "declined", "Declined"),
                 (record(1, "failed", "failed"), "failed", "Failed"),
                 # Older servers without "state" fall back to status and outcome.
                 (record(1, None, "completed"), "available", "In library"),
                 (record(1, None, "pending", outcome="declined"), "declined", "Declined"),
                 (record(1, "mystery", "new_stage"), "on_the_way", "On the way")]
        for value, key, short in cases:
            state = svc.record_state(value)
            self.assertEqual((state["key"], state["short"]), (key, short), value)
        self.assertEqual(svc.record_state(cases[5][0])["reason"], "Not this year")

    def test_result_states_and_routing(self):
        self.assertIsNone(svc.result_state(result(1)))
        in_library = result(1, availability="available", library_content_id="movie:example-1")
        self.assertEqual(svc.result_state(in_library)["key"], "available")
        self.assertEqual(svc.library_item(in_library), "movie:example-1")
        coming = result(1, request={"requestable": False, "state": "pending", "reason": "already_requested"})
        self.assertEqual(svc.result_state(coming)["long"], "Waiting for approval")
        self.assertEqual(svc.library_item(coming), "")
        blocked = result(1, request={"requestable": False, "reason": "quota_exceeded"})
        self.assertEqual(svc.result_state(blocked)["long"], "Request limit reached")
        self.assertEqual(svc.reason_text("some_new_code"), "")
        self.assertEqual(svc.reason_text("server sentence"), "Server sentence")


class HubTests(unittest.TestCase):
    def test_rows_show_my_requests_then_discover(self):
        window = open_hub()
        titles = [row["title"] for row in window.rows]
        self.assertEqual(titles, ["Your requests", "Trending Movies", "Popular Series"])
        window.getControl(3001).setLabel.assert_called_with("Your requests")
        window.getControl(3011).setLabel.assert_called_with("Trending Movies")
        for index in range(3, ui_requests.SLOTS):
            window.getControl(ui_requests.slot_ids(index)[0]).setVisible.assert_called_with(False)
        self.assertEqual(window.focus, 3002)

    def test_my_request_cards_carry_status_chips(self):
        window = open_hub()
        items = window.getControl(3002).addItems.call_args.args[0]
        self.assertEqual([item.properties["silo.status_key"] for item in items], ["pending", "declined"])
        self.assertEqual([item.label2 for item in items], ["Pending", "Declined"])
        self.assertEqual(items[0].art["poster"], "https://image.tmdb.org/t/p/w500/r1.jpg")
        discover = window.getControl(3012).addItems.call_args.args[0]
        self.assertEqual(discover[0].properties["silo.status_key"], "")
        self.assertEqual(discover[0].label2, "2021")

    def test_cancelled_requests_are_left_out(self):
        client = FakeClient({("GET", "/requests/mine"): {"items": [record(1, "cancelled", "pending")]}})
        window = open_hub(make_application(client))
        self.assertEqual(window.rows[0]["title"], "Trending Movies")

    def test_preview_follows_the_focused_card(self):
        window = open_hub()
        self.assertEqual(window.properties["silo.preview.title"], "Example Request 1")
        self.assertEqual(window.properties["silo.preview.status"], "Waiting for approval")
        self.assertEqual(window.properties["silo.preview.meta"], "2020 · Movie · Requested 2026-01-02")
        window.focus = 3012
        window.getControl(3012).getSelectedPosition.return_value = 1
        window.update_preview()
        self.assertEqual(window.properties["silo.preview.title"], "Example Movie 11")
        self.assertEqual(window.properties["silo.preview.meta"], "2021 · Movie · TMDB 7.2")
        self.assertEqual(window.properties["silo.preview.backdrop"], "https://image.tmdb.org/t/p/w780/backdrop11.jpg")
        self.assertEqual(window.properties["silo.preview.status"], "")

    def test_up_and_down_move_between_rows_and_the_search_field(self):
        window = open_hub()
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, 3012)
        window.getControl(3000).setVisible.assert_called_with(False)
        window.getControl(3010).setPosition.assert_called_with(0, ui_requests.ROWS_TOP)
        window.onAction(Action(UP))
        window.onAction(Action(UP))
        self.assertEqual(window.focus, ui_requests.SEARCH_BUTTON)
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, 3002)

    def test_load_error_goes_through_guard_and_leaves_search(self):
        application = make_application(FakeClient({("GET", "/requests/mine"): ApiError("Requests are off")}))
        window = open_hub(application)
        application.dialog.ok.assert_called_once_with("Silo", "Requests are off")
        self.assertEqual(window.properties["silo.empty"], "Requests could not be loaded.")
        self.assertEqual(window.focus, ui_requests.SEARCH_BUTTON)

    def test_search_puts_results_first(self):
        client = FakeClient({("GET", "/requests/search"): {"results": [result(30), result(31, "series")]}})
        window = open_hub(make_application(client))
        window.application.dialog.input.return_value = " example "
        window.onClick(ui_requests.SEARCH_BUTTON)
        self.assertEqual(window.rows[0]["title"], 'Results for "example"')
        self.assertEqual(window.properties["silo.query"], "example")
        self.assertEqual(window.focus, 3002)
        self.assertEqual(len(window.getControl(3002).addItems.call_args.args[0]), 2)

    def test_search_without_results_says_so(self):
        client = FakeClient({("GET", "/requests/search"): {"results": []}})
        window = open_hub(make_application(client))
        window.application.dialog.input.return_value = "nothing"
        window.onClick(ui_requests.SEARCH_BUTTON)
        self.assertEqual(window.properties["silo.search_note"], 'No results for "nothing"')
        self.assertEqual(window.rows[0]["title"], "Your requests")

    def test_card_in_library_opens_the_catalog_item(self):
        client = FakeClient({("GET", "/requests/mine"): {"items": [
            record(1, "available", "completed", library_content_id="movie:example-1")]}})
        window = open_hub(make_application(client))
        with patch.object(ui_requests, "open_request_detail") as detail:
            window.onClick(3002)
        window.application.open_item.assert_called_once_with({"content_id": "movie:example-1"})
        detail.assert_not_called()
        self.assertEqual(window.focus, 3002)

    def test_other_cards_open_the_request_page_and_refresh_after_a_change(self):
        window = open_hub()
        window.getControl(3012).getSelectedPosition.return_value = 1
        with patch.object(ui_requests, "open_request_detail", return_value=True) as detail:
            window.onClick(3012)
        self.assertEqual(detail.call_args.args[1]["tmdb_id"], 11)
        mine_reads = [call for call in window.application.client.calls if call[1] == "/requests/mine"]
        self.assertEqual(len(mine_reads), 2)

    def test_context_menu_cancels_a_pending_request(self):
        client = FakeClient({("POST", "/requests/r1/cancel"): {"state": "cancelled"}})
        window = open_hub(make_application(client))
        window.application.dialog.select.return_value = 0
        window.onAction(Action(CONTEXT))
        self.assertIn(("POST", "/requests/r1/cancel", {}, None), client.calls)

    def test_back_closes(self):
        window = open_hub()
        window.onAction(Action(BACK))
        self.assertTrue(window.closed)

    def test_open_requests_runs_the_window_modally(self):
        application = make_application()
        with patch.object(ui_requests.RequestsWindow, "doModal") as modal:
            ui_requests.open_requests(application)
        modal.assert_called_once_with()


class DetailTests(unittest.TestCase):
    def test_requestable_movie(self):
        document = dict(result(40), runtime=125, genres=["Drama", "Example"], director="Example Director",
                        recommendations=[result(41), {"media_type": "audiobook", "tmdb_id": 3}])
        client = FakeClient(routes(detail_route(document), {("POST", "/requests"): {"id": "r40"}}))
        window = open_detail(result(40), make_application(client))
        self.assertEqual(window.properties["silo.eyebrow"], "Not in your library")
        self.assertEqual(window.properties["silo.meta"], "2021 · Movie · TMDB 7.2 · 2h 5m · Drama · Example")
        self.assertEqual(window.properties["silo.credit"], "Directed by Example Director")
        window.getControl(ui_requests.PRIMARY).setLabel.assert_called_with("Request")
        window.getControl(ui_requests.FOLLOW).setVisible.assert_called_with(False)
        window.getControl(ui_requests.CANCEL).setVisible.assert_called_with(False)
        self.assertEqual(window.focus, ui_requests.PRIMARY)
        self.assertEqual(len(window.recommendations), 1)
        window.onClick(ui_requests.PRIMARY)
        post = [call for call in client.calls if call[0] == "POST"][0]
        self.assertEqual(post[2]["tmdb_id"], 40)
        self.assertNotIn("seasons", post[2])
        self.assertTrue(window.changed)
        self.assertEqual(window.properties["silo.hint"], "Request sent")

    def test_series_request_chooses_seasons(self):
        seasons = [{"season_number": 1, "availability": "available", "episode_count": 8},
                   {"season_number": 2, "availability": "partial", "episode_count": 10},
                   {"season_number": 3, "availability": "missing", "episode_count": 1},
                   {"season_number": 4, "availability": "missing", "requested": True}]
        document = dict(result(50, "series"), seasons=seasons, tvdb_id=500)
        client = FakeClient(routes(detail_route(document), {("POST", "/requests"): {"id": "r50"}}))
        application = make_application(client)
        application.dialog.multiselect.return_value = [1]
        window = open_detail(result(50, "series"), application)
        window.onClick(ui_requests.PRIMARY)
        labels = application.dialog.multiselect.call_args.args[1]
        self.assertEqual(labels, ["Season 2 · 10 episodes · partly in library", "Season 3 · 1 episode"])
        self.assertEqual(application.dialog.multiselect.call_args.kwargs["preselect"], [0, 1])
        post = [call for call in client.calls if call[0] == "POST"][0]
        self.assertEqual(post[2]["seasons"], [3])
        self.assertEqual(post[2]["tvdb_id"], 500)

    def test_cancelled_season_choice_sends_nothing(self):
        document = dict(result(50, "series"), seasons=[{"season_number": 1, "availability": "missing"}])
        client = FakeClient(detail_route(document))
        application = make_application(client)
        application.dialog.multiselect.return_value = None
        window = open_detail(result(50, "series"), application)
        window.onClick(ui_requests.PRIMARY)
        self.assertFalse([call for call in client.calls if call[0] == "POST"])
        self.assertFalse(window.changed)

    def test_series_without_season_support_requests_the_whole_series(self):
        document = dict(result(50, "series"), seasons=[{"season_number": 1, "availability": "missing"}])
        client = FakeClient(routes(detail_route(document), {("POST", "/requests"): {"id": "r50"}}))
        application = make_application(client)
        window = open_detail(result(50, "series"), application, feature={"season_requests_supported": False})
        window.onClick(ui_requests.PRIMARY)
        application.dialog.multiselect.assert_not_called()
        self.assertNotIn("seasons", [call for call in client.calls if call[0] == "POST"][0][2])

    def test_follow_toggle_on_someone_elses_request(self):
        state = {"following": False}

        def document(params, body):
            return result(60, request={"requestable": False, "reason": "already_requested", "state": "approved",
                                       "status": "queued", "following": state["following"],
                                       "requested_by_viewer": False})

        def follow(params, body):
            state["following"] = True
            return {"following": True}

        def unfollow(params, body):
            state["following"] = False

        client = FakeClient({("GET", "/requests/detail/movie/60"): document,
                             ("PUT", "/requests/follows/movie/60"): follow,
                             ("DELETE", "/requests/follows/movie/60"): unfollow})
        window = open_detail(result(60), make_application(client))
        self.assertEqual(window.properties["silo.eyebrow"], "Requested")
        self.assertEqual(window.properties["silo.status"], "Queued for download")
        window.getControl(ui_requests.PRIMARY).setLabel.assert_called_with("Queued for download")
        window.getControl(ui_requests.FOLLOW).setLabel.assert_called_with("Notify me when available")
        window.onClick(ui_requests.FOLLOW)
        window.getControl(ui_requests.FOLLOW).setLabel.assert_called_with("Stop notifying me")
        self.assertEqual(window.properties["silo.hint"], "You'll be notified when it's available.")
        window.onClick(ui_requests.FOLLOW)
        window.getControl(ui_requests.FOLLOW).setLabel.assert_called_with("Notify me when available")
        self.assertEqual([call[0] for call in client.calls if "follows" in call[1]], ["PUT", "DELETE"])
        self.assertTrue(window.changed)

    def test_no_follow_on_own_request_or_when_unsupported(self):
        own = result(61, request={"requestable": False, "reason": "already_requested", "state": "pending",
                                  "requested_by_viewer": True, "request_id": "r61", "following": True})
        self.assertFalse(svc.can_follow(own))
        other = dict(own, request=dict(own["request"], requested_by_viewer=False))
        self.assertTrue(svc.can_follow(other))
        self.assertFalse(svc.can_follow(other, {"follow_supported": False}))

    def test_cancel_own_pending_request(self):
        own = result(62, request={"requestable": False, "reason": "already_requested", "state": "pending",
                                  "requested_by_viewer": True, "request_id": "r62"})
        client = FakeClient(routes(detail_route(own), {("POST", "/requests/r62/cancel"): {"state": "cancelled"}}))
        application = make_application(client)
        application.dialog.yesno.return_value = True
        window = open_detail(result(62), application)
        window.getControl(ui_requests.CANCEL).setLabel.assert_called_with("Cancel request")
        window.onClick(ui_requests.CANCEL)
        self.assertIn(("POST", "/requests/r62/cancel", {}, None), client.calls)
        self.assertEqual(window.properties["silo.hint"], "Request cancelled")

    def test_in_library_opens_the_item_and_offers_missing_seasons(self):
        document = dict(result(70, "series"), availability="available", library_content_id="series:example-70")
        client = FakeClient(detail_route(document))
        window = open_detail(result(70, "series"), make_application(client))
        window.getControl(ui_requests.PRIMARY).setLabel.assert_called_with("Open in Library")
        window.getControl(ui_requests.MORE_SEASONS).setLabel.assert_called_with("Request missing seasons")
        self.assertEqual(window.properties["silo.eyebrow"], "")
        window.onClick(ui_requests.PRIMARY)
        window.application.open_item.assert_called_once_with({"content_id": "series:example-70"})

    def test_request_errors_go_through_guard(self):
        client = FakeClient(routes(detail_route(result(80)), {("POST", "/requests"): ApiError("Request limit")}))
        application = make_application(client)
        window = open_detail(result(80), application)
        window.onClick(ui_requests.PRIMARY)
        application.dialog.ok.assert_called_once_with("Silo", "Request limit")
        self.assertFalse(window.changed)

    def test_detail_load_failure_closes(self):
        client = FakeClient({("GET", "/requests/detail/movie/90"): ApiError("Not found")})
        window = open_detail(result(90), make_application(client))
        self.assertTrue(window.closed)

    def test_buttons_are_chained_left_to_right(self):
        document = dict(result(70, "series"), availability="available", library_content_id="series:example-70")
        window = open_detail(result(70, "series"), make_application(FakeClient(detail_route(document))))
        primary, more = window.getControl(ui_requests.PRIMARY), window.getControl(ui_requests.MORE_SEASONS)
        primary.controlRight.assert_called_with(more)
        more.controlLeft.assert_called_with(primary)
        width = ui_requests.button_width("Open in Library")
        more.setPosition.assert_called_with(ui_requests.BUTTON_LEFT + width + ui_requests.BUTTON_GAP,
                                            ui_requests.BUTTON_TOP)


class SkinTests(unittest.TestCase):
    NAMES = ("SiloRequests.xml", "SiloRequestsDetail.xml")

    @classmethod
    def setUpClass(cls):
        cls.trees = {name: ET.parse(SKIN / "1080i" / name) for name in cls.NAMES}

    def ids(self, name):
        return [control.get("id") for control in self.trees[name].iter("control") if control.get("id")]

    def test_ids_are_unique(self):
        for name in self.NAMES:
            ids = self.ids(name)
            self.assertEqual(len(ids), len(set(ids)), name)

    def test_navigation_targets_exist(self):
        for name in self.NAMES:
            ids = set(self.ids(name))
            for tag in ("onup", "ondown", "onleft", "onright"):
                for element in self.trees[name].iter(tag):
                    self.assertIn(element.text, ids, "{} {} -> {}".format(name, tag, element.text))

    def test_textures_exist(self):
        media = SKIN / "media"
        for name in self.NAMES:
            for element in self.trees[name].iter():
                if not element.tag.startswith("texture"):
                    continue
                for value in (element.text, element.get("diffuse")):
                    if value and "$INFO" not in value:
                        self.assertTrue((media / value).is_file(), value)

    def test_fonts_are_estuary_names(self):
        for name in self.NAMES:
            for element in self.trees[name].iter("font"):
                self.assertIn(element.text, ESTUARY_FONTS)

    def test_python_control_ids_exist(self):
        hub = set(self.ids("SiloRequests.xml"))
        wanted = [ui_requests.SEARCH_BUTTON, ui_requests.PREVIEW_GROUP, ui_requests.EMPTY_LABEL,
                  ui_requests.SEARCH_NOTE]
        wanted += [control for index in range(ui_requests.SLOTS) for control in ui_requests.slot_ids(index)]
        for control in wanted:
            self.assertIn(str(control), hub)
        detail = set(self.ids("SiloRequestsDetail.xml"))
        for control in ui_requests.BUTTONS + (ui_requests.RECOMMENDATIONS, ui_requests.RECOMMENDATIONS_HEADER):
            self.assertIn(str(control), detail)

    def test_status_dot_colours_match_the_service(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            skin_requests = importlib.import_module("skin_requests")
        finally:
            sys.path.pop(0)
        self.assertEqual(dict(skin_requests.TINTS), svc.TINTS)
        for name, text in skin_requests.windows().items():
            self.assertEqual((SKIN / "1080i" / name).read_text(encoding="utf-8"), text, name)

    def test_public_repo_hygiene(self):
        sources = [ROOT / "resources/lib/silo/ui_requests.py", ROOT / "resources/lib/silo/svc_requests.py",
                   ROOT / "scripts/skin_requests.py"] + [SKIN / "1080i" / name for name in self.NAMES]
        for path in sources:
            text = path.read_text(encoding="utf-8").lower()
            for word in ("192.168.", "localhost", "10.0.0."):
                self.assertNotIn(word, text, path.name)


if __name__ == "__main__":
    unittest.main()
