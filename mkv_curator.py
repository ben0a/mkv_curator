#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13.9.0"]
# ///

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_NAME = "mkv_curator"
APP_VERSION = "0.8"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.toml"
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "mov_text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "pgs", "dvd_subtitle", "xsub", "dvb_subtitle"}
INTERRUPTED = False
CURRENT_SRC: Optional[str] = None
CURRENT_DST: Optional[str] = None
CURRENT_PROC: Optional[subprocess.Popen] = None
CURRENT_STATE_PATH: Optional[Path] = None
CURRENT_LOG_PATH: Optional[Path] = None

try:
    import tomllib
except Exception:
    tomllib = None

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
except Exception:
    Console = None
    Table = None
    Panel = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(v: Optional[str]) -> str:
    return (v or "").strip().lower()


def as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return norm(v) in {"1", "true", "yes", "on"}
    return bool(v)


def as_list(v: Any, default: Optional[List[str]] = None) -> List[str]:
    if v is None:
        return list(default or [])
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(default or [])


def load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    if tomllib is None:
        raise RuntimeError("Python tomllib unavailable; use Python 3.11+")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def cfg_get(cfg: Dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    return (cfg.get(section, {}) or {}).get(key, default)


def tags_of(stream: Dict[str, Any]) -> Dict[str, str]:
    return stream.get("tags", {}) or {}


def disp_of(stream: Dict[str, Any]) -> Dict[str, Any]:
    return stream.get("disposition", {}) or {}


def title_blob(stream: Dict[str, Any]) -> str:
    tags = tags_of(stream)
    return " ".join(filter(None, [tags.get("title", ""), tags.get("language", ""), tags.get("handler_name", ""), tags.get("HANDLER_NAME", "")]))


def is_default(stream: Dict[str, Any]) -> bool:
    return int(disp_of(stream).get("default", 0) or 0) == 1


def is_forced(stream: Dict[str, Any]) -> bool:
    return int(disp_of(stream).get("forced", 0) or 0) == 1 or "forced" in norm(title_blob(stream))


def is_hi(stream: Dict[str, Any]) -> bool:
    return any(x in norm(title_blob(stream)) for x in ["sdh", "hearing impaired", "malentendant"])


def is_french(stream: Dict[str, Any]) -> bool:
    lang = norm(tags_of(stream).get("language"))
    txt = norm(title_blob(stream))
    return lang in {"fra", "fre", "fr"} or any(x in txt for x in ["french", "français", "francais", "vff", "vfi"])


def is_english(stream: Dict[str, Any]) -> bool:
    lang = norm(tags_of(stream).get("language"))
    txt = norm(title_blob(stream))
    return lang in {"eng", "en"} or any(x in txt for x in ["english", "original", "vo"])


def stream_lang_code(stream: Dict[str, Any]) -> Optional[str]:
    if is_french(stream):
        return "fra"
    if is_english(stream):
        return "eng"
    lang = norm(tags_of(stream).get("language"))
    return lang or None


def allowed_lang(stream: Dict[str, Any], keep_langs: List[str]) -> bool:
    code = stream_lang_code(stream)
    if code is None:
        return False
    keep = {norm(x) for x in keep_langs}
    aliases = {"fra": {"fra", "fre", "fr"}, "eng": {"eng", "en"}}
    for preferred, vals in aliases.items():
        if preferred in keep and code in vals:
            return True
    return code in keep


def is_descriptive(stream: Dict[str, Any]) -> bool:
    txt = norm(title_blob(stream))
    title = norm(tags_of(stream).get("title"))
    return title in {"ad", "vf ad", "vff ad"} or any(x in txt for x in ["visual impaired", "visually impaired", "descriptive", "audio description", "description audio"])


def is_vfq(stream: Dict[str, Any]) -> bool:
    return any(x in norm(title_blob(stream)) for x in ["vfq", "québec", "quebec", "fr-ca", "canadian french"])


def is_text_subtitle(stream: Dict[str, Any]) -> bool:
    return norm(stream.get("codec_name")) in TEXT_SUB_CODECS


def is_bitmap_subtitle(stream: Dict[str, Any]) -> bool:
    return norm(stream.get("codec_name")) in BITMAP_SUB_CODECS


def ffprobe_json(ffprobe_bin: str, path: Path) -> Dict[str, Any]:
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams", "-show_chapters", "-show_format", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"ffprobe failed for {path}")
    return json.loads(res.stdout)


def find_files(inp: Path, recursive: bool) -> List[Path]:
    if inp.is_file():
        return [inp]
    pattern = "**/*.mkv" if recursive else "*.mkv"
    return sorted(inp.glob(pattern))


def has_dovi(stream: Dict[str, Any]) -> bool:
    for sd in stream.get("side_data_list", []) or []:
        if isinstance(sd, dict) and norm(sd.get("side_data_type")) == "dovi configuration record":
            return True
    return False


def is_hdr_video(stream: Dict[str, Any]) -> bool:
    cp = norm(stream.get("color_primaries"))
    ct = norm(stream.get("color_transfer"))
    cs = norm(stream.get("color_space")) or norm(stream.get("colorspace"))
    pf = norm(stream.get("pix_fmt"))
    return cp == "bt2020" or ct in {"smpte2084", "arib-std-b67"} or cs.startswith("bt2020") or ("10" in pf and has_dovi(stream))


def is_4k_like(stream: Dict[str, Any]) -> bool:
    return int(stream.get("width", 0) or 0) >= 3000


def classify_source(stream: Dict[str, Any]) -> str:
    hdr = is_hdr_video(stream)
    dovi = has_dovi(stream)
    uhd = is_4k_like(stream)
    if not hdr:
        return "sdr_4k" if uhd else "sdr_1080p"
    if dovi:
        return "hdr_4k_dovi" if uhd else "hdr_hd_dovi"
    return "hdr_4k" if uhd else "hdr_hd"


def decide_video_pipeline(stream: Dict[str, Any], eff: Dict[str, Any]) -> Dict[str, Any]:
    classification = classify_source(stream)
    force = eff.get("force_encoder")
    if force:
        video_mode = "hdr_keep" if "10bit" in force else "sdr_direct"
        return {"classification": classification, "encoder_key": force, "video_mode": video_mode, "reason": "forced encoder override"}
    if classification in {"sdr_1080p", "sdr_4k"}:
        return {"classification": classification, "encoder_key": eff["sdr_encoder"], "video_mode": "sdr_direct", "reason": "SDR source -> SDR HEVC"}
    if classification in {"hdr_4k_dovi", "hdr_hd_dovi"}:
        return {"classification": classification, "encoder_key": eff["dovi_policy"], "video_mode": "hdr_keep", "reason": f"HDR+DOVI source -> {eff['dovi_policy']} policy"}
    return {"classification": classification, "encoder_key": eff["hdr_encoder"], "video_mode": "hdr_keep", "reason": "HDR source without DOVI -> configured HDR path"}


def select_audio(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cands:
        return None
    def score(s: Dict[str, Any]):
        codec = norm(s.get("codec_name"))
        codec_rank = {"truehd": 6, "eac3": 5, "dts": 4, "ac3": 3, "aac": 2}.get(codec, 0)
        ch = int(s.get("channels", 0) or 0)
        return (is_default(s), ch, codec_rank, -int(s.get("index", 0)))
    return sorted(cands, key=score, reverse=True)[0]


def select_subs(cands: List[Dict[str, Any]], subtitle_mode: str, allow_sdh_fallback: bool) -> List[Dict[str, Any]]:
    if subtitle_mode == "none":
        return []
    forced = [s for s in cands if is_forced(s)]
    regular = [s for s in cands if not is_forced(s) and not is_hi(s)]
    hi = [s for s in cands if is_hi(s)]
    out = []
    if forced:
        out.append(sorted(forced, key=lambda s: (is_default(s), -int(s.get("index", 0))), reverse=True)[0])
    if subtitle_mode == "forced_only":
        return out
    if regular:
        out.append(sorted(regular, key=lambda s: (is_default(s), -int(s.get("index", 0))), reverse=True)[0])
    elif allow_sdh_fallback and hi:
        out.append(sorted(hi, key=lambda s: (is_default(s), -int(s.get("index", 0))), reverse=True)[0])
    seen = set(); uniq = []
    for s in out:
        if s["index"] not in seen:
            uniq.append(s); seen.add(s["index"])
    return uniq


def build_plan(meta: Dict[str, Any], eff: Dict[str, Any]) -> Dict[str, Any]:
    streams = meta.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not videos:
        raise RuntimeError("No video stream found")
    video = videos[0]
    pipeline = decide_video_pipeline(video, eff)

    excluded_subs = []
    kept_sub_candidates = []
    for s in subs:
        if is_bitmap_subtitle(s):
            excluded_subs.append((s, "bitmap subtitle not compatible with MP4/M4V; dropping instead of burning"))
            continue
        if not is_text_subtitle(s):
            excluded_subs.append((s, "unsupported subtitle codec for mov_text conversion"))
            continue
        kept_sub_candidates.append(s)

    fr_audio = [s for s in audios if allowed_lang(s, eff["keep_audio_langs"]) and stream_lang_code(s) == "fra" and not is_descriptive(s) and not is_vfq(s)]
    en_audio = [s for s in audios if allowed_lang(s, eff["keep_audio_langs"]) and stream_lang_code(s) == "eng" and not is_descriptive(s) and not is_vfq(s)]
    rejected_audio = []
    for s in audios:
        if is_descriptive(s):
            rejected_audio.append((s, "descriptive audio excluded"))
        elif is_vfq(s):
            rejected_audio.append((s, "VFQ/Quebec track excluded"))
        elif not allowed_lang(s, eff["keep_audio_langs"]):
            rejected_audio.append((s, "language not allowed"))

    fr_sub_candidates = [s for s in kept_sub_candidates if allowed_lang(s, eff["keep_sub_langs"]) and stream_lang_code(s) == "fra" and not is_vfq(s)]
    en_sub_candidates = [s for s in kept_sub_candidates if allowed_lang(s, eff["keep_sub_langs"]) and stream_lang_code(s) == "eng" and not is_vfq(s)]
    for s in kept_sub_candidates:
        if is_vfq(s):
            excluded_subs.append((s, "VFQ/Quebec subtitle excluded"))
        elif not allowed_lang(s, eff["keep_sub_langs"]):
            excluded_subs.append((s, "subtitle language not allowed"))
        elif is_hi(s) and not eff["allow_sdh_fallback"]:
            excluded_subs.append((s, "SDH/HI subtitle excluded by default"))

    if not eff["allow_sdh_fallback"]:
        fr_sub_candidates = [s for s in fr_sub_candidates if not is_hi(s)]
        en_sub_candidates = [s for s in en_sub_candidates if not is_hi(s)]

    return {
        "video": video,
        "video_mode": pipeline["video_mode"],
        "classification": pipeline["classification"],
        "encoder_key": pipeline["encoder_key"],
        "decision_reason": pipeline["reason"],
        "has_dovi": has_dovi(video),
        "is_hdr": is_hdr_video(video),
        "is_4k": is_4k_like(video),
        "audio": {"fra": select_audio(fr_audio), "eng": select_audio(en_audio)},
        "subs": {"fra": select_subs(fr_sub_candidates, eff["subtitle_mode"], eff["allow_sdh_fallback"]), "eng": select_subs(en_sub_candidates, eff["subtitle_mode"], eff["allow_sdh_fallback"])},
        "excluded_audio": rejected_audio,
        "excluded_subs": excluded_subs,
    }


def audio_title(lang: str, stereo: bool, ch: int) -> str:
    base = "French" if lang == "fra" else "English"
    return f"{base} Stereo" if stereo else f"{base} {ch}ch"


def sub_title(lang: str, forced: bool, hi: bool) -> str:
    base = "French" if lang == "fra" else "English"
    if forced:
        return f"{base} Forced"
    if hi:
        return f"{base} SDH"
    return f"{base} Full"


def output_root_for(inp: Path, output_dir: Optional[Path]) -> Path:
    return output_dir if output_dir is not None else (inp.parent if inp.is_file() else inp)


def output_path_for(src: Path, input_root: Path, output_dir: Optional[Path]) -> Path:
    if output_dir is None:
        return src.with_suffix(".m4v")
    if input_root.is_file():
        return output_dir / f"{src.stem}.m4v"
    rel_parent = src.parent.relative_to(input_root)
    return output_dir / rel_parent / f"{src.stem}.m4v"


def state_file(root: Path) -> Path:
    return root / f"{APP_NAME}.state.json"


def log_file(root: Path) -> Path:
    return root / f"{APP_NAME}.log.jsonl"


def report_json_file(root: Path, filename: str) -> Path:
    return root / filename


def read_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"app": APP_NAME, "version": APP_VERSION, "updated_at": now_iso(), "files": {}}
    return json.loads(path.read_text())


def write_state(path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def append_log(path: Path, event: Dict[str, Any]) -> None:
    event = dict(event); event["ts"] = now_iso()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def update_file_state(path: Path, src: Path, dst: Path, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    state = read_state(path)
    key = str(src)
    entry = state["files"].get(key, {})
    entry.update({"src": str(src), "dst": str(dst), "status": status, "updated_at": now_iso()})
    if extra:
        entry.update(extra)
    state["files"][key] = entry
    write_state(path, state)


def build_video_args(plan: Dict[str, Any], eff: Dict[str, Any]) -> List[str]:
    encoder = plan["encoder_key"]
    if encoder == "skip":
        raise RuntimeError("skip encoder should not build video args")
    if encoder == "videotoolbox_8bit":
        return ["-c:v", "hevc_videotoolbox", "-profile:v", "main", "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-allow_sw", "1", "-q:v", str(eff["vt_quality"]), "-movflags", "+faststart", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if encoder == "videotoolbox_10bit":
        return ["-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-pix_fmt", "p010le", "-tag:v", "hvc1", "-allow_sw", "1", "-q:v", str(eff["vt_quality"]), "-movflags", "+faststart", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
    if encoder == "libx265_10bit":
        params = "repeat-headers=1:hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
        return ["-c:v", "libx265", "-preset", eff["x265_preset"], "-crf", str(eff["x265_crf_hdr"]), "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", "-movflags", "+faststart", "-x265-params", params, "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
    raise RuntimeError(f"Unknown encoder key: {encoder}")


def build_cmd(ffmpeg_bin: str, src: Path, dst: Path, plan: Dict[str, Any], audio_codec: str, eff: Dict[str, Any]) -> List[str]:
    cmd = [ffmpeg_bin, "-y", "-i", str(src), "-map", "0:v:0", "-map", "-0:d", "-dn"]
    if eff["mp4_chapters"] == "keep":
        cmd += ["-map_chapters", "0"]
    else:
        cmd += ["-map_chapters", "-1"]
    if eff["map_metadata"]:
        cmd += ["-map_metadata", "0"]
    else:
        cmd += ["-map_metadata", "-1"]
    out_a = 0
    for lang in ["fra", "eng"]:
        s = plan["audio"][lang]
        if not s:
            continue
        idx = int(s["index"]); ch = int(s.get("channels", 0) or 0)
        cmd += ["-map", f"0:{idx}"]
        cmd += [f"-c:a:{out_a}", audio_codec, f"-ac:a:{out_a}", "2", f"-ar:a:{out_a}", "48000", f"-b:a:{out_a}", "160k"]
        cmd += [f"-metadata:s:a:{out_a}", f"language={lang}", f"-metadata:s:a:{out_a}", f"title={audio_title(lang, True, 2)}"]
        cmd += [f"-disposition:a:{out_a}", "default" if out_a == 0 else "0"]
        out_a += 1
        if eff["audio_mode"] == "stereo_plus_multichannel" and ch >= 6:
            out_ch = "8" if ch >= 8 else "6"; bitrate = "640k" if out_ch == "8" else "384k"
            cmd += ["-map", f"0:{idx}"]
            cmd += [f"-c:a:{out_a}", audio_codec, f"-ac:a:{out_a}", out_ch, f"-ar:a:{out_a}", "48000", f"-b:a:{out_a}", bitrate]
            cmd += [f"-metadata:s:a:{out_a}", f"language={lang}", f"-metadata:s:a:{out_a}", f"title={audio_title(lang, False, int(out_ch))}"]
            cmd += [f"-disposition:a:{out_a}", "0"]
            out_a += 1
    out_s = 0
    for lang in ["fra", "eng"]:
        for s in plan["subs"][lang]:
            idx = int(s["index"])
            cmd += ["-map", f"0:{idx}"]
            cmd += [f"-c:s:{out_s}", "mov_text"]
            cmd += [f"-metadata:s:s:{out_s}", f"language={lang}"]
            cmd += [f"-metadata:s:s:{out_s}", f"title={sub_title(lang, is_forced(s), is_hi(s))}"]
            cmd += [f"-disposition:s:{out_s}", "default+forced" if is_forced(s) else "0"]
            out_s += 1
    cmd += build_video_args(plan, eff)
    cmd += ["-f", "mp4", str(dst)]
    return cmd


def report_lines(src: Path, dst: Path, plan: Dict[str, Any], eff: Dict[str, Any]) -> List[str]:
    v = plan["video"]
    lines = [f"File: {src}", f"  Output: {dst}", f"  Video: stream {v['index']} | {v.get('codec_name')} | {v.get('width')}x{v.get('height')} | hdr={plan['is_hdr']} | dovi={plan['has_dovi']} | class={plan['classification']} | encoder={plan['encoder_key']}", f"  Decision: {plan['decision_reason']}"]
    for lang in ["fra", "eng"]:
        a = plan['audio'][lang]
        lines.append(f"  Audio {lang}: keep stream {a['index']} | {a.get('codec_name')} | {a.get('channels', '?')} ch | {tags_of(a).get('title','')}" if a else f"  Audio {lang}: none kept")
    for lang in ["fra", "eng"]:
        subs = plan['subs'][lang]
        if subs:
            for s in subs:
                lines.append(f"  Sub {lang}: keep stream {s['index']} | {s.get('codec_name')} | {tags_of(s).get('title','')}")
        else:
            lines.append(f"  Sub {lang}: none kept")
    if plan['excluded_audio']:
        lines.append("  Excluded audio:")
        for s, why in plan['excluded_audio']:
            lines.append(f"    - stream {s['index']} | {s.get('codec_name')} | {tags_of(s).get('title','')} | {why}")
    if plan['excluded_subs']:
        lines.append("  Excluded subtitles:")
        for s, why in plan['excluded_subs']:
            lines.append(f"    - stream {s['index']} | {s.get('codec_name')} | {tags_of(s).get('title','')} | {why}")
    lines.append(f"  Effective config: sdr_encoder={eff['sdr_encoder']} hdr_encoder={eff['hdr_encoder']} dovi_policy={eff['dovi_policy']} vt_quality={eff['vt_quality']} x265_preset={eff['x265_preset']} x265_crf_hdr={eff['x265_crf_hdr']} mp4_chapters={eff['mp4_chapters']}")
    return lines


def handle_interrupt(signum, frame):
    global INTERRUPTED, CURRENT_PROC, CURRENT_SRC, CURRENT_DST, CURRENT_STATE_PATH, CURRENT_LOG_PATH
    INTERRUPTED = True
    if CURRENT_STATE_PATH and CURRENT_SRC and CURRENT_DST:
        update_file_state(CURRENT_STATE_PATH, Path(CURRENT_SRC), Path(CURRENT_DST), "interrupted")
    if CURRENT_LOG_PATH and CURRENT_SRC and CURRENT_DST:
        append_log(CURRENT_LOG_PATH, {"event": "interrupted", "src": CURRENT_SRC, "dst": CURRENT_DST})
    if CURRENT_PROC and CURRENT_PROC.poll() is None:
        CURRENT_PROC.send_signal(signal.SIGINT)


def sizeof_mb(path: Path) -> Optional[float]:
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else None


def effective_settings(args, cfg: Dict[str, Any]) -> Dict[str, Any]:
    output_dir = args.output_dir if args.output_dir is not None else cfg_get(cfg, "paths", "output_dir")
    ffmpeg_bin = args.ffmpeg_bin if args.ffmpeg_bin is not None else cfg_get(cfg, "paths", "ffmpeg_bin", "/opt/ffmpeg-zimg/bin/ffmpeg")
    ffprobe_bin = args.ffprobe_bin if args.ffprobe_bin is not None else cfg_get(cfg, "paths", "ffprobe_bin", "/opt/ffmpeg-zimg/bin/ffprobe")
    recursive = args.recursive or as_bool(cfg_get(cfg, "behavior", "recursive", False))
    resume = args.resume or as_bool(cfg_get(cfg, "behavior", "resume", False))
    vt_quality = args.vt_quality if args.vt_quality is not None else int(cfg_get(cfg, "video", "vt_quality", 60))
    x265_preset = args.x265_preset if args.x265_preset is not None else cfg_get(cfg, "video", "x265_preset", "slow")
    x265_crf_hdr = args.x265_crf_hdr if args.x265_crf_hdr is not None else int(cfg_get(cfg, "video", "x265_crf_hdr", 20))
    sdr_encoder = args.sdr_encoder if args.sdr_encoder is not None else cfg_get(cfg, "video", "sdr_encoder", "videotoolbox_8bit")
    hdr_encoder = args.hdr_encoder if args.hdr_encoder is not None else cfg_get(cfg, "video", "hdr_encoder", "videotoolbox_10bit")
    dovi_policy = args.dovi_policy if args.dovi_policy is not None else cfg_get(cfg, "video", "dovi_policy", "videotoolbox_10bit")
    force_encoder = args.force_encoder if args.force_encoder is not None else cfg_get(cfg, "video", "force_encoder")
    audio_mode = args.audio_mode if args.audio_mode is not None else cfg_get(cfg, "audio", "mode", "stereo_plus_multichannel")
    subtitle_mode = args.subtitle_mode if args.subtitle_mode is not None else cfg_get(cfg, "subtitles", "mode", "forced_and_full")
    keep_audio_langs = as_list(args.keep_audio_langs if args.keep_audio_langs is not None else cfg_get(cfg, "audio", "keep_languages", ["fra", "eng"]), ["fra", "eng"])
    keep_sub_langs = as_list(args.keep_sub_langs if args.keep_sub_langs is not None else cfg_get(cfg, "subtitles", "keep_languages", ["fra", "eng"]), ["fra", "eng"])
    allow_sdh_fallback = args.allow_sdh_fallback or as_bool(cfg_get(cfg, "subtitles", "allow_sdh_fallback", False))
    console_style = args.console_style if args.console_style is not None else cfg_get(cfg, "report", "console_style", "rich")
    write_json = as_bool(args.write_json if args.write_json is not None else cfg_get(cfg, "report", "write_json", True), True)
    write_jsonl = as_bool(args.write_jsonl if args.write_jsonl is not None else cfg_get(cfg, "report", "write_jsonl", True), True)
    report_filename = args.report_filename if args.report_filename is not None else cfg_get(cfg, "report", "report_filename", f"{APP_NAME}_report.json")
    mp4_chapters = args.mp4_chapters if args.mp4_chapters is not None else cfg_get(cfg, "behavior", "mp4_chapters", "drop")
    map_metadata = as_bool(args.map_metadata if args.map_metadata is not None else cfg_get(cfg, "behavior", "map_metadata", False), False)
    return {"output_dir": output_dir, "ffmpeg_bin": ffmpeg_bin, "ffprobe_bin": ffprobe_bin, "recursive": recursive, "resume": resume, "dry_run": args.dry_run, "vt_quality": vt_quality, "x265_preset": x265_preset, "x265_crf_hdr": x265_crf_hdr, "sdr_encoder": sdr_encoder, "hdr_encoder": hdr_encoder, "dovi_policy": dovi_policy, "force_encoder": force_encoder, "audio_mode": audio_mode, "subtitle_mode": subtitle_mode, "keep_audio_langs": keep_audio_langs, "keep_sub_langs": keep_sub_langs, "allow_sdh_fallback": allow_sdh_fallback, "console_style": console_style, "write_json": write_json, "write_jsonl": write_jsonl, "report_filename": report_filename, "mp4_chapters": mp4_chapters, "map_metadata": map_metadata}


def print_effective_config(eff: Dict[str, Any], config_path: Path) -> None:
    print(json.dumps({"config_path": str(config_path), **eff}, indent=2, ensure_ascii=False))


def summarize(results: List[Dict[str, Any]], report_path: Optional[Path], console_style: str) -> None:
    total = len(results)
    converted = sum(1 for r in results if r["status"] == "done")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"].startswith("skipped"))
    dovi = [r for r in results if r.get("has_dovi")]
    dovi_vt = [r for r in dovi if r.get("encoder_key") == "videotoolbox_10bit"]
    warnings = [r for r in results if r.get("warning")]

    if console_style == "rich" and Console and Table and Panel:
        c = Console()
        table = Table(title=f"{APP_NAME} v{APP_VERSION} summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="bold white")
        table.add_row("Files scanned", str(total))
        table.add_row("Converted", str(converted))
        table.add_row("Skipped", str(skipped))
        table.add_row("Failed", str(failed))
        table.add_row("DOVI files", str(len(dovi)))
        table.add_row("DOVI -> VT10", str(len(dovi_vt)))
        c.print(table)
        if dovi:
            dt = Table(title="Dolby Vision files")
            dt.add_column("Source", style="magenta")
            dt.add_column("Class", style="cyan")
            dt.add_column("Policy", style="green")
            dt.add_column("Status", style="yellow")
            for r in dovi:
                dt.add_row(Path(r['src']).name, r.get('classification',''), r.get('encoder_key',''), r.get('status',''))
            c.print(dt)
        if warnings:
            text = "\n".join(f"- {Path(r['src']).name}: {r['warning']}" for r in warnings)
            c.print(Panel(text, title="Warnings", border_style="red"))
        if report_path:
            c.print(Panel(str(report_path), title="JSON report written", border_style="green"))
    else:
        print(f"{APP_NAME} v{APP_VERSION} summary")
        print(f"Files scanned: {total}")
        print(f"Converted: {converted} | Skipped: {skipped} | Failed: {failed}")
        print(f"DOVI files: {len(dovi)} | DOVI -> VT10: {len(dovi_vt)}")
        if report_path:
            print(f"JSON report written: {report_path}")


def write_reports(root: Path, results: List[Dict[str, Any]], eff: Dict[str, Any]) -> Optional[Path]:
    report_path = report_json_file(root, eff["report_filename"])
    payload = {"app": APP_NAME, "version": APP_VERSION, "generated_at": now_iso(), "effective_config": eff, "results": results}
    if eff["write_json"]:
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    if eff["write_jsonl"]:
        with (root / f"{APP_NAME}_report.jsonl").open("w", encoding="utf-8") as f:
            for row in results:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return report_path if eff["write_json"] else None


def main() -> int:
    signal.signal(signal.SIGINT, handle_interrupt)
    parser = argparse.ArgumentParser(prog=f"{APP_NAME} {APP_VERSION}", description="Curate MKV files into HEVC-only M4V outputs with configurable DOVI policy and terminal/JSON reports.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config")
    parser.add_argument("--print-effective-config", action="store_true")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ffmpeg-bin", default=None)
    parser.add_argument("--ffprobe-bin", default=None)
    parser.add_argument("--vt-quality", type=int, default=None)
    parser.add_argument("--x265-preset", default=None, choices=["medium", "slow", "slower", "veryslow"])
    parser.add_argument("--x265-crf-hdr", type=int, default=None)
    parser.add_argument("--sdr-encoder", default=None, choices=["videotoolbox_8bit", "videotoolbox_10bit", "libx265_10bit"])
    parser.add_argument("--hdr-encoder", default=None, choices=["videotoolbox_10bit", "libx265_10bit"])
    parser.add_argument("--dovi-policy", default=None, choices=["videotoolbox_10bit", "libx265_10bit", "skip"])
    parser.add_argument("--force-encoder", default=None, choices=["videotoolbox_8bit", "videotoolbox_10bit", "libx265_10bit"])
    parser.add_argument("--audio-mode", choices=["stereo_only", "stereo_plus_multichannel"], default=None)
    parser.add_argument("--subtitle-mode", choices=["none", "forced_only", "forced_and_full"], default=None)
    parser.add_argument("--keep-audio-langs", default=None)
    parser.add_argument("--keep-sub-langs", default=None)
    parser.add_argument("--allow-sdh-fallback", action="store_true")
    parser.add_argument("--audio-codec", default="aac", choices=["aac"])
    parser.add_argument("--console-style", default=None, choices=["rich", "plain"])
    parser.add_argument("--write-json", default=None)
    parser.add_argument("--write-jsonl", default=None)
    parser.add_argument("--report-filename", default=None)
    parser.add_argument("--mp4-chapters", default=None, choices=["keep", "drop"])
    parser.add_argument("--map-metadata", default=None)
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    cfg = load_toml(config_path) if config_path.exists() else {}
    eff = effective_settings(args, cfg)
    if args.print_effective_config:
        print_effective_config(eff, config_path)
        return 0

    ffmpeg_ok = Path(eff["ffmpeg_bin"]).exists() or shutil.which(eff["ffmpeg_bin"])
    ffprobe_ok = Path(eff["ffprobe_bin"]).exists() or shutil.which(eff["ffprobe_bin"])
    if not ffmpeg_ok:
        print(f"ffmpeg not found: {eff['ffmpeg_bin']}", file=sys.stderr); return 2
    if not ffprobe_ok:
        print(f"ffprobe not found: {eff['ffprobe_bin']}", file=sys.stderr); return 2

    inp = Path(args.input)
    output_dir = Path(eff["output_dir"]) if eff["output_dir"] else None
    root = output_root_for(inp, output_dir)
    root.mkdir(parents=True, exist_ok=True)
    sfile = state_file(root); lfile = log_file(root)
    files = find_files(inp, eff["recursive"])
    if not files:
        print("No MKV files found", file=sys.stderr); return 1

    failures = 0
    results = []
    for src in files:
        if INTERRUPTED:
            break
        started = time.time()
        row = {"src": str(src), "ts_start": now_iso()}
        try:
            dst = output_path_for(src, inp, output_dir)
            row["dst"] = str(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                row.update({"status": "skipped_exists"})
                update_file_state(sfile, src, dst, "skipped_exists")
                append_log(lfile, {"event": "skipped_exists", "src": str(src), "dst": str(dst)})
                print(f"SKIP existing output: {dst}")
                results.append(row)
                continue

            meta = ffprobe_json(eff["ffprobe_bin"], src)
            plan = build_plan(meta, eff)
            row.update({"classification": plan['classification'], "encoder_key": plan['encoder_key'], "has_dovi": plan['has_dovi'], "is_hdr": plan['is_hdr']})
            for line in report_lines(src, dst, plan, eff):
                print(line)

            if plan["encoder_key"] == "skip":
                row.update({"status": "skipped_dovi_policy", "warning": "DOVI file skipped by policy"})
                update_file_state(sfile, src, dst, "skipped_dovi_policy", {"classification": plan['classification'], "encoder_key": plan['encoder_key']})
                append_log(lfile, {"event": "skipped_dovi_policy", "src": str(src), "dst": str(dst), "classification": plan['classification']})
                results.append(row)
                continue

            cmd = build_cmd(eff["ffmpeg_bin"], src, dst, plan, args.audio_codec, eff)
            append_log(lfile, {"event": "planned", "src": str(src), "dst": str(dst), "classification": plan['classification'], "encoder_key": plan['encoder_key'], "effective": eff})
            update_file_state(sfile, src, dst, "planned", {"classification": plan['classification'], "encoder_key": plan['encoder_key'], "effective": eff})
            if eff["dry_run"]:
                print("  Planned command:")
                print("   ", " ".join(cmd)); print()
                row.update({"status": "planned"})
                results.append(row)
                continue

            update_file_state(sfile, src, dst, "running")
            append_log(lfile, {"event": "running", "src": str(src), "dst": str(dst)})
            global CURRENT_SRC, CURRENT_DST, CURRENT_PROC, CURRENT_STATE_PATH, CURRENT_LOG_PATH
            CURRENT_SRC = str(src); CURRENT_DST = str(dst); CURRENT_STATE_PATH = sfile; CURRENT_LOG_PATH = lfile
            CURRENT_PROC = subprocess.Popen(cmd)
            rc = CURRENT_PROC.wait(); CURRENT_PROC = None
            if INTERRUPTED:
                break
            if rc != 0:
                failures += 1
                row.update({"status": "failed", "returncode": rc})
                update_file_state(sfile, src, dst, "failed", {"returncode": rc})
                append_log(lfile, {"event": "failed", "src": str(src), "dst": str(dst), "returncode": rc})
                print(f"FAILED: {src}", file=sys.stderr)
            else:
                row.update({"status": "done", "output_size_mb": sizeof_mb(dst)})
                probe_out = ffprobe_json(eff["ffprobe_bin"], dst)
                has_data_stream = any(s.get("codec_type") == "data" for s in probe_out.get("streams", []))
                if has_data_stream:
                    row["warning"] = "output still contains data stream"
                update_file_state(sfile, src, dst, "done")
                append_log(lfile, {"event": "done", "src": str(src), "dst": str(dst), "warning": row.get('warning')})
                print(f"WROTE: {dst}\n")
            row["elapsed_sec"] = round(time.time() - started, 2)
            row["ts_end"] = now_iso()
            results.append(row)
        except Exception as exc:
            failures += 1
            row.update({"status": "failed", "error": str(exc), "elapsed_sec": round(time.time() - started, 2), "ts_end": now_iso()})
            append_log(lfile, {"event": "error", "src": str(src), "error": str(exc)})
            print(f"FAILED: {src}: {exc}", file=sys.stderr)
            results.append(row)

    report_path = write_reports(root, results, eff)
    summarize(results, report_path, eff["console_style"])
    if INTERRUPTED:
        print("Interrupted by user", file=sys.stderr); return 130
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
