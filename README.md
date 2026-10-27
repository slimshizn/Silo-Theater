# Founders Theater - Silo for Kodi

Founders Theater brings your Silo media library to **Kodi 21 or newer**, with a TV interface modeled on the Silo Android TV app and Kodi's built-in video player.

Founders Theater is a community project. It is not an official Silo app and is not made or endorsed by the Silo team. Silo is a trademark of Silo Media L.L.C.

## Features

- Sign in with an activation code or username and password, or create an account when your server allows it.
- Choose household profiles, including PIN-protected ones, and manage profiles (names, avatars, PINs, kid limits).
- Home rows in your server's order, with a hero that follows the title you focus; reorder or hide rows; remove titles from Continue Watching.
- Movies and Series grids with library, sort, filters (genre, decade, watch status, rating, resolution, HDR, studio, network, country), an A–Z jump and Shuffle; each library's Recommended rows and Collections.
- For You, Calendar and, when your server offers it, Requests (search, request, Notify me).
- Search with people; person pages with filmography.
- Title details with seasons, an episode rail, cast, trailers and extras, related titles and a facts table; Watchlist, Favorite, Mark watched, Go to series and Shuffle.
- Watchlist, Favorites and History screens.
- Plays original files directly, and titles your device cannot play as converted HLS streams from your server; choose the quality during playback.
- Skip Intro, Skip Recap and Skip Credits from your server's markers, with your profile's auto-skip settings.
- Up next with a countdown, Are you still watching?, and rewind when resuming.
- A Silo player menu during playback: info, stats, version, quality, speed, aspect, sleep timer, audio, subtitles (including online search, AI translation and sync to audio) and chapters.
- Settings for your profile and this device (profile menu → **Settings**); switch between several Silo servers.
- Widgets for Kodi's own home screen with the optional **Founders Theater Widgets** add-on: your Silo rows, Watchlist and Favorites.

## Install in Kodi

1. Open **Settings → File manager → Add source** and enter `https://slimshizn.github.io/Silo-Theater/`. Name the source **Founders Theater**.
2. Open **Add-ons → Install from zip file → Founders Theater**. Enable installation from unknown sources if prompted.
3. Select **repository.silo.theater-1.0.2.zip**.
4. Open **Install from repository → Founders Theater Repository**, find **Founders Theater**, and select **Install**.

Open **Founders Theater** in Kodi and enter your server URL and Silo account username and password on the sign-in screen. Native Silo code sign-in is also available when your server supports it. Valid sessions and your last profile are reused on later launches; locked profiles still require their PIN. Server URLs with a reverse-proxy path prefix are supported.

For Silo rows on Kodi's own home screen, also install **Founders Theater Widgets** from the same repository (**Video add-ons**). Point your skin's widgets at its folders; selecting a title plays it in Founders Theater.

You can also [download the repository installer](https://slimshizn.github.io/Silo-Theater/repository.silo.theater-1.0.2.zip) in a browser and transfer it to your Kodi device.

On a movie or episode's details screen, **Play** follows your resume preference and shows the resume time when there is one; the round **Start over** button restarts the title. The round **Version** button appears when a title has more than one file and **Subtitles** lets you pick a track, or Off, before you press play; the line under the buttons shows the current choice. **Watchlist** and **Favorite** toggle those lists, and **More** offers Mark watched, Go to series or season, Shuffle and Media info. A series page plays its next episode directly. Resume points belong to your selected Silo profile.

Text uses your Kodi skin's font, because Kodi add-ons cannot bring their own.

## Build from source

Build from source using **Python 3.9 or newer**. Building and testing need no third-party Python packages; only regenerating the skin images (`python3 scripts/build_textures.py`) needs Pillow.

```sh
make check
make build
make repository
```

`make build` creates `dist/script.silo-0.7.3.zip` for direct installation through Kodi's **Install from zip file** menu. `make repository` creates the static Kodi repository and installer under `dist/pages/`.

## Playback compatibility

Version **0.7.3** plays original files directly when your device supports them and asks your Silo server to convert the rest (remux or transcode to HLS). Set **Maximum bitrate for converted video** under **Settings → This device → More device settings…** to limit converted streams. On Automatic quality, **Lower quality when buffering** (Settings → Playback) asks your server for a lower quality when playback keeps buffering, as the Silo apps do; choose Ask first or Never to change that. **Settings → Account → Test connection speed** measures your connection to Silo and can clear a limit Silo learned after buffering.

### Theme music

Turn on **Theme music** under **Settings → Audio and subtitles** to hear a title's theme on its details page, when your Silo server has theme songs. **Loop theme music** repeats it while the page stays open. It is downloaded first (and kept in a small cache), so it starts without Kodi's buffering spinner, fades in from a low volume, and fades out when you leave the page or start a video; it never plays over other audio. Kodi has no per-song volume, so the fade briefly moves Kodi's own volume and then puts it back where you had it. Silo plays local theme files (`theme.mp3` or a `theme-music` folder next to a title). If your server has none for a show, turn on **Online TV themes** (same screen) to play the show's theme from Plex's public TV theme library by its TVDB id; Plex then sees which show's theme you ask for. A show Plex has no theme for is checked again after a week.

### During playback

Press **Back** to stop a Silo video and return to Founders Theater, as in the Silo apps (turn off **Back stops playback** in **Settings → This device** to keep Kodi's background playback). Turn on **Match frame rate** there, or answer yes when Founders Theater first asks, to switch the TV to each video's frame rate; Kodi pauses 2 seconds while the TV switches.

Press **Menu** on the remote (**C** on a keyboard) to open the Silo player menu. It shows playback info and live stats, and lets you change the version, quality (for converted streams), speed, aspect, sleep timer, audio track and subtitles, search for subtitles online, translate them with AI or sync them to the audio (when your server offers it), or jump to a chapter. Audio delay and subtitle style open Kodi's own settings. Kodi passes keys to add-ons only through keymaps, so Founders Theater adds one small keymap file that is active only in fullscreen video; turn off **Silo player menu on the Menu key** in its settings to remove it.

When your Silo server has intro, recap or credits markers for a file, a **Skip** button appears while that part plays. Turn on auto-skip under **Settings → Playback** in the profile menu to skip without asking. Near the end of an episode, **Up next** shows the following episode and plays it after a 10-second countdown; turn off **Play next episode automatically** to wait for your choice instead.

The first time Founders Theater reaches your library it asks **What can this TV show?** and sets the display capability from your answer. Until a device is described, it declares SDR with a 1080p ceiling, so the server converts HDR and 4K titles for this device instead of sending the original file. You can change the answer later under **Settings → This device → More device settings… → Playback → HDR and Dolby Vision capability** and **Maximum video resolution**. Select HDR10 or Dolby Vision only for a compatible device and display. Select **Dolby Vision P7 FEL** only when your hardware, firmware and Kodi build support full enhancement-layer decoding.

Audio and subtitles follow your Silo profile's preferences, the same way the Silo apps choose them: the audio language from your profile (then English), and subtitles from your subtitle mode and language (Automatic shows subtitles when the audio is in another language, and only forced subtitles when it is yours). The details screen shows the choice, marked **(Auto)** when it comes from your profile. If you pick a different subtitle, or Off, on the details screen or in the player menu, or a different audio language (such as the original Japanese instead of a dub) in the player menu, Silo saves it for that title, and for a show for the following episodes, on your profile, so the choice also applies in the other Silo apps, and a choice made there applies here. Kodi switches to the chosen tracks itself, so a non-default audio track still plays the original file. If Kodi's decoder stops advancing during playback while the stream keeps arriving, Silo nudges playback a few seconds forward to restart it, at most three times in a row. Subtitle files are downloaded by the add-on and given to Kodi as local files, so your sign-in never appears in Kodi's log. When your Silo server streams a video itself rather than through a proxy node, Kodi plays it through a small relay inside the add-on on the same device (127.0.0.1), which adds your sign-in to each request; nothing needs to be set up, and videos from proxy nodes still go to Kodi directly.

### Playback stops after a few seconds

If video plays for a few seconds and then stops while Silo keeps loading, Kodi's HTTP/2 download may be stalling through a reverse proxy in front of your Silo server. The best fix is on the server side: serve the media hostnames over HTTP/1.1 (for nginx or OpenResty 1.25.1 and newer, `http2 off;` in those server blocks). To fix a single device instead, add this to Kodi's `userdata/advancedsettings.xml` and restart Kodi:

```xml
<advancedsettings version="1.0">
  <network>
    <disablehttp2>true</disablehttp2>
  </network>
</advancedsettings>
```

Offline checks cover API requests, authentication, navigation, playback sessions and packaging. Live Silo playback, skin rendering, subtitles, audio-track selection and full P7 FEL output still require validation on real Kodi devices.

## License

Founders Theater is licensed under the **GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`)**. See [LICENSE](LICENSE).
