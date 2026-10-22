# Silo Theater

Silo Theater brings your Silo media library to **Kodi 21 or newer**, with a TV interface modeled on the Silo Android TV app and Kodi's built-in video player.

Silo Theater is a community project. It is not an official Silo app and is not made or endorsed by the Silo team. The Silo name and logo are trademarks of Silo Media L.L.C. and are not covered by this project's license.

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
- Silo Settings for your profile and this device; switch between several Silo servers.
- Widgets for Kodi's own home screen: your Silo rows, Watchlist and Favorites.

## Install in Kodi

1. Open **Settings → File manager → Add source** and enter `https://slimshizn.github.io/Silo-Theater/`. Name the source **Silo Theater**.
2. Open **Add-ons → Install from zip file → Silo Theater**. Enable installation from unknown sources if prompted.
3. Select **repository.silo.theater-1.0.1.zip**.
4. Open **Install from repository → Silo Theater Repository**, find **Silo Theater**, and select **Install**.

Open **Silo Theater** in Kodi and enter your server URL and Silo account username and password on the sign-in screen. Native Silo code sign-in is also available when your server supports it. Valid sessions and your last profile are reused on later launches; locked profiles still require their PIN. Server URLs with a reverse-proxy path prefix are supported.

You can also [download the repository installer](https://slimshizn.github.io/Silo-Theater/repository.silo.theater-1.0.1.zip) in a browser and transfer it to your Kodi device.

On a movie or episode's details screen, **Play** follows your resume preference and shows the resume time when there is one; the round **Start over** button restarts the title. The round **Version** button appears when a title has more than one file and **Subtitles** lets you pick a track, or Off, before you press play; the line under the buttons shows the current choice. A series page plays its next episode directly. Resume points belong to your selected Silo profile.

Text uses your Kodi skin's font, because Kodi add-ons cannot bring their own.

## Build from source

Build from source using **Python 3.9 or newer**. No third-party Python packages are required.

```sh
make check
make build
make repository
```

`make build` creates `dist/script.silo-0.5.1.zip` for direct installation through Kodi's **Install from zip file** menu. `make repository` creates the static Kodi repository and installer under `dist/pages/`.

## Playback compatibility

Version **0.5.1** plays original files directly when your device supports them and asks your Silo server to convert the rest (remux or transcode to HLS). Set **Maximum bitrate for converted video** in Settings to limit converted streams.

### During playback

Press **Back** to stop a Silo video and return to Silo Theater, as in the Silo apps (turn off **Back stops playback** in Silo Settings → This device to keep Kodi's background playback). Turn on **Match frame rate** there, or answer yes when Silo Theater first asks, to switch the TV to each video's frame rate; Kodi pauses 2 seconds while the TV switches.

Press **Menu** on the remote (**C** on a keyboard) to open the Silo player menu. It shows playback info and live stats, and lets you change the version, speed, aspect, sleep timer, audio track and subtitles, or jump to a chapter. Audio delay and subtitle style open Kodi's own settings. Kodi passes keys to add-ons only through keymaps, so Silo Theater adds one small keymap file that is active only in fullscreen video; turn off **Silo player menu on the Menu key** in its settings to remove it.

When your Silo server has intro, recap or credits markers for a file, a **Skip** button appears while that part plays. Turn on auto-skip under **Settings** in the profile menu to skip without asking. Near the end of an episode, **Up next** shows the following episode and plays it after a 10-second countdown; turn off **Play next episode automatically** to wait for your choice instead.

The first time Silo reaches your library it asks **What can this TV show?** and sets the display capability from your answer. Until a device is described, Silo declares SDR with a 1080p ceiling, so HDR and 4K titles are refused with a message that names the setting to change. You can change the answer later under **Settings → Playback → HDR and Dolby Vision capability** and **Maximum video resolution**. Select HDR10 or Dolby Vision only for a compatible device and display. Select **Dolby Vision P7 FEL** only when your hardware, firmware and Kodi build support full enhancement-layer decoding.

Silo chooses the audio track and subtitle for each title; Kodi switches to them itself, so a non-default audio track still plays the original file. If Kodi's decoder stops advancing during playback while the stream keeps arriving, Silo nudges playback a few seconds forward to restart it, at most three times in a row. Subtitle files are downloaded by the add-on and given to Kodi as local files, so your sign-in never appears in Kodi's log.

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

Silo Theater is licensed under the **GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`)**. See [LICENSE](LICENSE).
