"""Conservative Kodi declarations for Silo's native playback-v3 contract.

No hardware or output capability is inferred from a platform name. HDR and
Dolby Vision are explicit user declarations until device probes are validated.
"""

import platform


DV_MODES = ("auto", "hdr10", "dv", "p7_fel")
VIDEO_CODECS = ("h264", "hevc", "mpeg4", "mpeg2video", "vc1", "vp8", "vp9")
AUDIO_CODECS = ("aac", "ac3", "eac3", "truehd", "dts", "flac", "mp3", "opus", "vorbis", "pcm_s16le", "pcm_s24le")
CONTAINERS = ("mkv", "matroska", "mp4", "mov", "avi", "mpegts", "ts", "webm")


def playback_capabilities(mode="auto", app_version="0.4.0", max_resolution="auto", client_audio_selection=True):
    """Return the v2 start body's codec capabilities and playback context.

    Kodi's decoder can decode these formats in software, but the first build
    cannot attest hardware bounds or receiver passthrough. ``auto`` claims SDR
    only. The explicit P7 mode promises original bytes to a verified executor.
    Only original HTTP delivery is offered.
    """
    if mode not in DV_MODES:
        mode = "auto"
    hdr = {"hdr10": mode != "auto", "hdr10_plus": False, "hlg": False,
           "dolby_vision_profiles": [5, 7, 8] if mode == "p7_fel" else [5, 8] if mode == "dv" else []}
    codecs = {
        "video_evidence": "declared", "audio_evidence": "declared",
        "codecs_video": list(VIDEO_CODECS), "codecs_video_hardware": [],
        "codecs_audio": list(AUDIO_CODECS), "containers": list(CONTAINERS),
        "max_resolution": max_resolution if max_resolution in ("480p", "720p", "1080p", "2160p")
        else "2160p" if mode != "auto" else "1080p",
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
    context = {
        "protocol_version": 3, "form_factor": "tv", "app_version": app_version,
        "app_channel": "private-alpha",
        "device": {"platform": platform.system()[:32],
                   "os_version": platform.release()[:128]},
        "output": {"hdr_details": dict(hdr)},
        "deliveries": {"original_http": original},
    }
    return codecs, context
