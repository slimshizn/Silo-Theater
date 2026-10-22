# Silo Theater

Silo Theater brings your Silo media library to **Kodi 21 or newer**, with a fullscreen browsing interface and Kodi's built-in video player.

## Features

- Sign in with an activation code or username and password.
- Choose household profiles, including PIN-protected profiles.
- Browse home hubs and libraries, search, filter, sort and load more results.
- Open title details and navigate series, seasons and episodes.
- Select a movie or episode's media file by filename, edition and format.
- Resume movies and episodes from their saved position and sync progress with Silo.
- Customize resume behavior, file selection, browsing and playback preferences.
- Configure resolution, HDR10, Dolby Vision or P7 FEL capabilities for supported devices.

## Install in Kodi

1. Open **Settings → File manager → Add source** and enter `https://slimshizn.github.io/Silo-Theater/`. Name the source **Silo Theater**.
2. Open **Add-ons → Install from zip file → Silo Theater**. Enable installation from unknown sources if prompted.
3. Select **repository.silo.theater-1.0.1.zip**.
4. Open **Install from repository → Silo Theater Repository**, find **Silo**, and select **Install**.

Open **Silo** in Kodi and enter your server URL and Silo account username and password on the sign-in screen. Native Silo code sign-in is also available when your server supports it. Valid sessions and your last profile are reused on later launches; locked profiles still require their PIN. Server URLs with a reverse-proxy path prefix are supported.

You can also [download the repository installer](https://slimshizn.github.io/Silo-Theater/repository.silo.theater-1.0.1.zip) in a browser and transfer it to your Kodi device.

On a movie or episode's details screen, open **File** to choose which version to play. **Play** follows your resume preference; **Resume** continues from the saved position, and **Play from start** restarts the selected file. Resume points belong to your selected Silo profile.

## Build from source

Build from source using **Python 3.9 or newer**. No third-party Python packages are required.

```sh
make check
make build
make repository
```

`make build` creates `dist/script.silo-0.2.4.zip` for direct installation through Kodi's **Install from zip file** menu. `make repository` creates the static Kodi repository and installer under `dist/pages/`.

## Playback compatibility

Version **0.2.4** supports **original HTTP direct play**. Titles that require remuxing or transcoding may be refused. Auto-next, a custom player overlay and automatic quality switching are not available.

The first time Silo reaches your library it asks **What can this TV show?** and sets the display capability from your answer. Until a device is described, Silo declares SDR with a 1080p ceiling, so HDR and 4K titles are refused with a message that names the setting to change. You can change the answer later under **Settings → Playback → HDR and Dolby Vision capability** and **Maximum video resolution**. Select HDR10 or Dolby Vision only for a compatible device and display. Select **Dolby Vision P7 FEL** only when your hardware, firmware and Kodi build support full enhancement-layer decoding.

Silo chooses the audio track and subtitle for each title; Kodi switches to them itself, so a non-default audio track still plays the original file. On Windows, Silo makes no audio-track change when playback starts and Kodi plays the file's default audio track, because switching at startup left playback stuck there. If Kodi's decoder stops advancing during playback while the stream keeps arriving, Silo nudges playback a few seconds forward to restart it, at most three times in a row. Subtitle files are downloaded by the add-on and given to Kodi as local files, so your sign-in never appears in Kodi's log.

Offline checks cover API requests, authentication, navigation, playback sessions and packaging. Live Silo playback, skin rendering, subtitles, audio-track selection and full P7 FEL output still require validation on real Kodi devices.

## License

Silo Theater is licensed under the **GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`)**. See [LICENSE](LICENSE).
