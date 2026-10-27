"""Card, hero and details text match the Android TV app without Kodi installed."""

import unittest

from resources.lib.silo import present


def image(item, kind):
    value = item.get(kind + "_url")
    return "https://example.invalid/" + value if value else ""


EPISODE = {
    "content_id": "episode-1", "type": "episode", "title": "The Return", "series_title": "Example Show",
    "season_number": "2", "episode_number": "8", "runtime": "52", "content_rating": "TV-MA",
    "position_seconds": 1990.0, "duration_seconds": 3108, "backdrop_url": "still.jpg", "poster_url": "poster.jpg",
    "logo_url": "logo.png", "overview": "Something happens.",
    "overlay_summary": {"resolution": "2160p", "hdr": "DV HDR10", "audio": "Atmos"},
}
MOVIE = {
    "content_id": "movie-1", "type": "movie", "title": "Example Movie", "year": 2024, "runtime": 133,
    "rating_imdb": 7.84, "genres": ["Drama", "Thriller"], "content_rating": "PG-13",
    "overlay_summary": {"resolution": "1080p", "hdr": "SDR", "audio": "EAC3"},
    "user_state": {"played": True}, "poster_url": "poster.jpg",
}


class CardTests(unittest.TestCase):
    def test_wide_episode_card_shows_series_episode_badge_and_progress(self):
        card = present.card(EPISODE, image, wide=True)
        self.assertEqual(card["label"], "Example Show")
        self.assertEqual(card["label2"], "The Return")
        self.assertEqual(card["badge"], "S2 · E8")
        self.assertEqual(card["progress"], 64)
        self.assertFalse(card["watched"])
        self.assertEqual(card["art"]["thumb"], "https://example.invalid/still.jpg")

    def test_poster_cards_show_year_and_episode_code(self):
        self.assertEqual(present.card(MOVIE, image)["label2"], "2024")
        self.assertTrue(present.card(MOVIE, image)["watched"])
        self.assertEqual(present.card(EPISODE, image)["label2"], "S2:E8 · The Return")

    def test_missing_or_invalid_progress_hides_the_bar(self):
        for item in ({}, {"position_seconds": "x", "duration_seconds": 10},
                     {"position_seconds": 5, "duration_seconds": 0}, {"position_seconds": True, "runtime": 10}):
            self.assertEqual(present.progress_percent(item), 0, item)
        self.assertEqual(present.progress_percent({"user_data": {"position_seconds": 300}, "runtime": 10}), 50)

    def test_video_items_drop_books_and_malformed_entries(self):
        items = [MOVIE, {"type": "audiobook", "content_id": "a"}, {"type": "movie"}, "x", None]
        self.assertEqual(present.video_items(items), [MOVIE])


class HeroTests(unittest.TestCase):
    def test_badges_follow_android_order(self):
        self.assertEqual(present.badges(EPISODE), ["4K", "DOLBY VISION", "ATMOS", "TV-MA"])
        self.assertEqual(present.badges(MOVIE), ["FHD", "EAC3", "PG-13"])
        version = {"resolution": "2160p", "hdr": True, "codec_audio": "truehd"}
        self.assertEqual(present.badges({}, version), ["4K", "HDR", "TRUEHD"])

    def test_resolution_badges_tell_each_tier_apart(self):
        # Silo labels files 480p, 720p, 1080p, 2160p or 4320p.
        for value, badge in (("4320p", "8K"), ("2160p", "4K"), ("4K", "4K"), ("uhd", "4K"), ("1080p", "FHD"),
                             ("1080i", "FHD"), ("720p", "HD"), ("576p", "SD"), ("480p", "SD"), ("480i", "SD"),
                             ("sd", "SD"), ("", ""), (None, ""), ("unknown", "")):
            self.assertEqual(present.resolution_badge(value), badge, value)

    def test_hero_meta_for_episode_and_movie(self):
        self.assertEqual(present.hero_meta(EPISODE), "S2 E8 · The Return · 52 min · 19m left")
        self.assertEqual(present.hero_meta(MOVIE), "2024 · 2h 13m · IMDb 7.8 · Drama")

    def test_hero_respects_plot_setting(self):
        hero = present.hero(EPISODE, image, show_plot=False)
        self.assertEqual(hero["synopsis"], "")
        self.assertEqual(hero["logo"], "https://example.invalid/logo.png")
        self.assertEqual(hero["title"], "Example Show")

    def test_badge_width_grows_with_text(self):
        self.assertLess(present.badge_width("HD"), present.badge_width("DOLBY VISION"))


class DetailsTests(unittest.TestCase):
    def test_detail_meta_per_type(self):
        self.assertEqual(present.detail_meta(MOVIE), "2024 · 2h 13m · IMDb 7.8 · Drama · Thriller")
        series = {"type": "series", "year": 2022, "season_count": 2, "rating_tmdb": 8.4}
        self.assertEqual(present.detail_meta(series), "2022 · 2 Seasons · TMDb 8.4")
        episode = dict(EPISODE, air_date="2026-03-30T00:00:00Z")
        self.assertEqual(present.detail_meta(episode), "S2 · E8 · 2026-03-30 · 52 min")

    def test_credit_line_prefers_director_then_creator_then_cast(self):
        crew = [{"job": "Producer", "name": "A"}, {"job": "Director", "name": "B"}]
        self.assertEqual(present.credit_line({"crew": crew}), "Directed by B")
        self.assertEqual(present.credit_line({"crew": [{"job": "Creator", "name": "C"}]}), "Created by C")
        cast = [{"name": "D"}, {"name": "E"}, {"name": "F"}, {"name": "G"}]
        self.assertEqual(present.credit_line({"cast": cast}), "Starring D, E, F")
        self.assertEqual(present.credit_line({}), "")

    def test_seasons_put_specials_last_and_start_at_next_up(self):
        seasons = [{"content_id": "s0", "season_number": 0, "is_specials": True},
                   {"content_id": "s2", "season_number": 2, "title": "Season 2"},
                   {"content_id": "s1", "season_number": 1}]
        ordered = present.ordered_seasons(seasons)
        self.assertEqual([season["content_id"] for season in ordered], ["s1", "s2", "s0"])
        self.assertEqual([present.season_label(season) for season in ordered], ["Season 1", "Season 2", "Specials"])
        self.assertEqual(present.initial_season_index({"play_season_number": 2}, ordered), 1)
        self.assertEqual(present.initial_season_index({}, ordered), 0)

    def test_episode_caption(self):
        line1, line2 = present.episode_caption({"season_number": 1, "episode_number": 2, "title": "Pilot",
                                                "air_date": "2022-02-18", "runtime": 57})
        self.assertEqual(line1, "S01E02 · Pilot")
        self.assertEqual(line2, "2022-02-18 · 57 min")


class ProfileTests(unittest.TestCase):
    def test_initials(self):
        self.assertEqual(present.initials("Example Person"), "EP")
        self.assertEqual(present.initials("kid"), "K")
        self.assertEqual(present.initials(""), "?")

    def test_avatar_urls_become_png_and_unsafe_urls_are_dropped(self):
        self.assertEqual(present.avatar_image("https://avatars.example.invalid/9.x/robot/svg?seed=a"),
                         "https://avatars.example.invalid/9.x/robot/png?seed=a")
        self.assertEqual(present.avatar_image("https://example.invalid/a.png"), "https://example.invalid/a.png")
        for url in ("", None, "https://example.invalid/a.svg", "file:///etc/passwd",
                    "https://user:pass@example.invalid/a.png"):
            self.assertEqual(present.avatar_image(url), "", url)


if __name__ == "__main__":
    unittest.main()
