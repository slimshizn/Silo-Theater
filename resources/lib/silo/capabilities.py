"""Conservative Kodi declarations for Silo's native playback-v3 contract.

No hardware or output capability is inferred from a platform name. HDR and
Dolby Vision are explicit user declarations until device probes are validated.
"""

import platform


DV_MODES = ("auto", "hdr10", "dv", "p7_fel")
RESOLUTIONS = ("480p", "720p", "1080p", "2160p")
# Formats Kodi's FFmpeg-based player decodes in software on every platform.
VIDEO_CODECS = ("h264", "hevc", "av1", "mpeg4", "mpeg2video", "vc1", "wmv3", "vp8", "vp9")
AUDIO_CODECS = ("aac", "ac3", "eac3", "truehd", "dts", "flac", "alac", "mp3", "mp2", "opus", "vorbis",
                "wmav2", "wmapro", "pcm_s16le", "pcm_s24le", "pcm_s16be", "pcm_s24be")
# Container names as Silo's scanner reports them, plus FFmpeg's aliases.
CONTAINERS = ("mkv", "matroska", "mp4", "mov", "avi", "mpegts", "ts", "m2ts", "webm",
              "wmv", "flv", "ogg", "mpeg")
# A converted stream reaches Kodi as an HLS playlist (fMP4 or MPEG-TS
# segments), which Kodi plays natively or through inputstream.adaptive.
HLS_CONTAINERS = ("hls", "m3u8")
HLS_VIDEO_CODECS = ("h264", "hevc")
HLS_AUDIO_CODECS = ("aac", "ac3", "eac3", "mp3", "flac", "opus")


def _resolution(mode, max_resolution):
    if max_resolution in RESOLUTIONS:
        return max_resolution
    return "2160p" if mode != "auto" else "1080p"


def playback_capabilities(mode="auto", app_version="0.5.5", max_resolution="auto", client_audio_selection=True):
    """Return the v2 start body's codec capabilities and playback context.

    Kodi's decoder can decode these formats in software, but this build cannot
    attest hardware bounds or receiver passthrough. ``auto`` claims SDR only.
    The explicit P7 mode promises original bytes to a verified executor.

    Two delivery classes are offered: the original file over HTTP (direct
    play) and HLS, which the server uses for remuxed and transcoded streams.
    Progressive remux is declared but disabled: it is a forward-only stream
    without byte ranges, so every Kodi seek would need a server reanchor.
    """
    if mode not in DV_MODES:
        mode = "auto"
    hdr = {"hdr10": mode != "auto", "hdr10_plus": False, "hlg": False,
           "dolby_vision_profiles": [5, 7, 8] if mode == "p7_fel" else [5, 8] if mode == "dv" else []}
    codecs = {
        "video_evidence": "declared", "audio_evidence": "declared",
        "codecs_video": list(VIDEO_CODECS), "codecs_video_hardware": [],
        "codecs_audio": list(AUDIO_CODECS), "containers": list(CONTAINERS),
        "max_resolution": _resolution(mode, max_resolution),
        "hdr": mode != "auto", "hdr_details": hdr,
    }
    original = {
        "enabled": True, "supported_on_device": True,
        "containers": list(CONTAINERS), "video_codecs": list(VIDEO_CODECS),
        "audio_decode_codecs": list(AUDIO_CODECS), "audio_passthrough_codecs": [],
        "hdr_details": dict(hdr), "auth_header_refresh": False,
        "subtitles": {"embedded_text": True, "sidecar_text": True,
                      "ass_styling": True, "embedded_bitmap": True,
                      "sidecar_bitmap": False, "font_attachments": True},
        "features": [], "transformations": [],
        # Kodi selects the planned audio track itself (setAudioStream with the
        # plan's audio ordinal), so a non-default track needs no server remux.
        # Callers can turn this off where that switch is not made.
        "validated_claims": (["client_selected_audio_track_v1"] if client_audio_selection else [])
        + (["client_managed_dynamic_range_v1"] if mode == "p7_fel" else []),
    }
    # An HLS transport carries one audio track and no subtitle stream: the
    # server switches audio with a track_change replan and serves subtitles as
    # text sidecars, which Kodi loads from a local file. Bitmap subtitles are
    # burned in rather than claimed.
    hls = {
        "enabled": True, "supported_on_device": True,
        "containers": list(HLS_CONTAINERS), "video_codecs": list(HLS_VIDEO_CODECS),
        "audio_decode_codecs": list(HLS_AUDIO_CODECS), "audio_passthrough_codecs": [],
        "hdr_details": dict(hdr), "auth_header_refresh": False,
        "subtitles": {"embedded_text": True, "sidecar_text": True,
                      "ass_styling": True, "embedded_bitmap": False,
                      "sidecar_bitmap": False, "font_attachments": False},
        "features": ["hls"], "transformations": [], "validated_claims": [],
    }
    progressive = {
        "enabled": False, "supported_on_device": False,
        "failure_reason": "disabled_pending_seekable_transport",
        "containers": ["mp4"], "video_codecs": list(VIDEO_CODECS),
        "audio_decode_codecs": list(AUDIO_CODECS), "audio_passthrough_codecs": [],
        "auth_header_refresh": False,
        "subtitles": {"embedded_text": True, "sidecar_text": True,
                      "ass_styling": False, "embedded_bitmap": False,
                      "sidecar_bitmap": False, "font_attachments": False},
        "features": [], "transformations": [], "validated_claims": [],
    }
    context = {
        "protocol_version": 3, "form_factor": "tv", "app_version": app_version,
        "app_channel": "private-alpha",
        "device": {"platform": platform.system()[:32],
                   "os_version": platform.release()[:128]},
        "output": {"hdr_details": dict(hdr)},
        "deliveries": {"original_http": original, "hls": hls, "progressive": progressive},
    }
    return codecs, context
