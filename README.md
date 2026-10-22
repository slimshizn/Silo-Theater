# Silo Theater

Silo Theater brings your Silo media library to **Kodi 21 or newer**, with a fullscreen browsing interface and Kodi's built-in video player.

## Features

- Sign in with an activation code or username and password.
- Choose household profiles, including PIN-protected profiles.
- Browse home hubs and libraries, search, filter, sort and load more results.
- Open title details and navigate series, seasons and episodes.
- Play original media files, resume playback and sync progress with Silo.
- Configure HDR10, Dolby Vision or P7 FEL capabilities for supported devices.

## Install in Kodi

1. Open **Settings → File manager → Add source** and enter `https://slimshizn.github.io/Silo-Theater/`. Name the source **Silo Theater**.
2. Open **Add-ons → Install from zip file → Silo Theater**. Enable installation from unknown sources if prompted.
3. Select **repository.silo.theater-1.0.0.zip**.
4. Open **Install from repository → Silo Theater Repository**, find **Silo**, and select **Install**.

Open **Silo** in Kodi, enter your Silo server's full HTTP or HTTPS URL, sign in and choose a profile. Server URLs with a reverse-proxy path prefix are supported.

You can also [download the repository installer](https://slimshizn.github.io/Silo-Theater/repository.silo.theater-1.0.0.zip) in a browser and transfer it to your Kodi device.

## Build from source

Build from source using **Python 3.9 or newer**. No third-party Python packages are required.

```sh
make check
make build
make repository
```

`make build` creates `dist/script.silo-0.1.0.zip` for direct installation through Kodi's **Install from zip file** menu. `make repository` creates the static Kodi repository and installer under `dist/pages/`.

## Playback compatibility

Version **0.1.0** supports **original HTTP direct play**. Titles that require remuxing or transcoding may be refused. Auto-next, a custom player overlay and automatic quality switching are not available.

The default display setting declares SDR with a 1080p ceiling. Select HDR10 or Dolby Vision only for a compatible device and display; these settings allow a 2160p ceiling. Select **Dolby Vision P7 FEL** only when your hardware, firmware and Kodi build support full enhancement-layer decoding. Device capabilities are configured manually.

Offline checks cover API requests, authentication, navigation, playback sessions and packaging. Live Silo playback, skin rendering, subtitles, audio-track selection and full P7 FEL output still require validation on real Kodi devices.

## License

Silo Theater is licensed under the **GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`)**. See [LICENSE](LICENSE).
