#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13.9.0", "textual>=0.69.0"]
# ///

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

APP_NAME = "mkv_curator"
APP_VERSION = "0.9"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.toml"
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "mov_text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "pgs", "dvd_subtitle", "xsub", "dvb_subtitle"}
INTERRUPTED = False
CURRENT_SRC: Optional[str] = None
CURRENT_DST: Optional[str] = None
CURRENT_PROC: Optional[subprocess.Popen] = None
CURRENT_STATE_PATH: Optional[Path] = None
CURRENT_LOG_PATH: Optional[Path] = None

# Regex to parse FFmpeg progress line from stderr
FFMPEG_PROGRESS_RE = re.compile(
    r"frame=(\d+)\s+fps=([\d.]+)\s+q=(-?\d+\.\d+|-?\d)\s+"
    r"(?:size=\s*(\d+)KiB\s*)?"
    r"time=(\d+:\d+:\d+\.\d+)\s*"
    r"bitrate=\s*([\d.]+|N/A)\s*kbits/s\s*"
    r"(?:speed=\s*([\d.]+|N/A)\s*x)?"
)

# Progress data passed to callbacks
class ConversionProgress:
    def __init__(self):
        self.frame = 0
        self.fps = 0.0
        self.speed = 1.0
        self.size_kib = 0.0
        self.time_str = "0:00:00.0"
        self.bitrate_kbps = 0.0

# File status in the processing queue
class FileState:
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    SKIPPED_EXISTS = "skipped_exists"
    SKIPPED_DOVI = "skipped_dovi_policy"
    FAILED = "failed"

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


def _load_textual():
    """Lazy-import textual only when TUI mode is used."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Container
        from textual.widgets import Footer, Header, Label, ProgressBar, Static, RichLog
        return App, ComposeResult, Binding, Container, Footer, Header, Label, ProgressBar, Static, RichLog
    except ImportError:
        print("textual not installed; run 'uv pip install textual'", file=sys.stderr)
        sys.exit(2)


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
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error reading config file {path}: {e}", file=sys.stderr)
        raise SystemExit(2)


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
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"Warning: could not read state file {path}; starting fresh", file=sys.stderr)
        return {"app": APP_NAME, "version": APP_VERSION, "updated_at": now_iso(), "files": {}}


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


def parse_progress_line(line: str, progress: ConversionProgress) -> None:
    m = FFMPEG_PROGRESS_RE.search(line.strip())
    if not m:
        return
    progress.frame = int(m.group(1))
    progress.fps = float(m.group(2))
    if m.group(4):
        progress.size_kib = int(m.group(4))
    if m.group(5):
        progress.time_str = m.group(5)
    if m.group(6) and m.group(6) != "N/A":
        progress.bitrate_kbps = float(m.group(6))
    if m.group(7) and m.group(7) != "N/A":
        progress.speed = float(m.group(7))

def _read_ffmpeg_stderr(rd_fd: int, progress: ConversionProgress,
                         on_progress: Optional[Callable], pause_event: threading.Event) -> None:
    """Background thread that reads FFmpeg stderr and fires progress callbacks."""
    import os
    buf = b""
    try:
        while True:
            chunk = os.read(rd_fd, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                try:
                    line = line_bytes.decode("utf-8", errors="replace")
                except Exception:
                    continue
                if "frame=" in line and "fps=" in line and "time=" in line:
                    parse_progress_line(line, progress)
                    if on_progress:
                        while pause_event.is_set():
                            time.sleep(0.1)
                        on_progress(progress)

                # Still write line to /dev/null (we don't want it cluttering the TUI)
    except OSError:
        pass
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)

def convert_one(src: Path, dst: Path, eff: Dict[str, Any], audio_codec: str,
                sfile: Path, lfile: Path,
                on_progress: Optional[Callable] = None,
                pause_event: Optional[threading.Event] = None,
                meta: Optional[Dict[str, Any]] = None,
                plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Process a single MKV file. Returns result dict."""
    started = time.time()
    row: Dict[str, Any] = {"src": str(src), "ts_start": now_iso(), "dst": str(dst)}
    progress = ConversionProgress()

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)

        if meta is None:
            meta = ffprobe_json(eff["ffprobe_bin"], src)
        if plan is None:
            plan = build_plan(meta, eff)
        row.update({"classification": plan["classification"], "encoder_key": plan["encoder_key"],
                     "has_dovi": plan["has_dovi"], "is_hdr": plan["is_hdr"]})

        if plan["encoder_key"] == "skip":
            row.update({"status": FileState.SKIPPED_DOVI, "warning": "DOVI file skipped by policy"})
            update_file_state(sfile, src, dst, FileState.SKIPPED_DOVI,
                              {"classification": plan["classification"], "encoder_key": plan["encoder_key"]})
            append_log(lfile, {"event": FileState.SKIPPED_DOVI, "src": str(src), "dst": str(dst),
                                "classification": plan["classification"]})
            row["elapsed_sec"] = round(time.time() - started, 2)
            row["ts_end"] = now_iso()
            return row

        cmd = build_cmd(eff["ffmpeg_bin"], src, dst, plan, audio_codec, eff)
        append_log(lfile, {"event": "planned", "src": str(src), "dst": str(dst),
                            "classification": plan["classification"], "encoder_key": plan["encoder_key"]})

        update_file_state(sfile, src, dst, FileState.RUNNING)
        append_log(lfile, {"event": "running", "src": str(src), "dst": str(dst)})

        global CURRENT_SRC, CURRENT_DST, CURRENT_PROC, CURRENT_STATE_PATH, CURRENT_LOG_PATH
        CURRENT_SRC = str(src); CURRENT_DST = str(dst)
        CURRENT_STATE_PATH = sfile; CURRENT_LOG_PATH = lfile

        # Spawn FFmpeg with stderr piped for progress parsing (TUI) or DEVNULL (CLI)
        stderr_target = subprocess.PIPE if on_progress else subprocess.DEVNULL
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_target)
        CURRENT_PROC = proc

        # Start background thread to read stderr
        reader_thread = None
        if on_progress:
            pipe_fd = proc.stderr.fileno() if proc.stderr else -1
            reader_thread = threading.Thread(
                target=_read_ffmpeg_stderr, args=(pipe_fd, progress, on_progress, pause_event or threading.Event()),
                daemon=True)
            reader_thread.start()

        # Handle pause/resume during wait
        check_interval = 0.25
        while proc.poll() is None:
            time.sleep(check_interval)
            if pause_event and pause_event.is_set():
                if proc.poll() is None:
                    try:
                        os.kill(proc.pid, signal.SIGSTOP)
                    except Exception:
                        pass
                update_file_state(sfile, src, dst, FileState.PAUSED)
            else:
                if proc.poll() is None:
                    try:
                        os.kill(proc.pid, signal.SIGCONT)
                    except Exception:
                        pass

        rc = proc.returncode or 0
        CURRENT_PROC = None

        if reader_thread:
            reader_thread.join(timeout=2)

        if INTERRUPTED:
            row["status"] = "interrupted"
            update_file_state(sfile, src, dst, "interrupted")
            return row

        if rc != 0:
            row.update({"status": FileState.FAILED, "returncode": rc})
            update_file_state(sfile, src, dst, FileState.FAILED, {"returncode": rc})
            append_log(lfile, {"event": "failed", "src": str(src), "dst": str(dst), "returncode": rc})
        else:
            row.update({"status": FileState.DONE, "output_size_mb": sizeof_mb(dst),
                         "frames_encoded": progress.frame, "avg_speed": round(progress.speed, 2)})
            probe_out = ffprobe_json(eff["ffprobe_bin"], dst)
            has_data_stream = any(s.get("codec_type") == "data" for s in probe_out.get("streams", []))
            if has_data_stream:
                row["warning"] = "output still contains data stream"
            update_file_state(sfile, src, dst, FileState.DONE)
            append_log(lfile, {"event": "done", "src": str(src), "dst": str(dst),
                                "warning": row.get("warning")})

        row["elapsed_sec"] = round(time.time() - started, 2)
        row["ts_end"] = now_iso()
        return row

    except Exception as exc:
        row.update({"status": FileState.FAILED, "error": str(exc),
                     "elapsed_sec": round(time.time() - started, 2), "ts_end": now_iso()})
        append_log(lfile, {"event": "error", "src": str(src), "error": str(exc)})
        raise

def handle_interrupt(signum, frame):
    global INTERRUPTED, CURRENT_PROC, CURRENT_SRC, CURRENT_DST, CURRENT_STATE_PATH, CURRENT_LOG_PATH
    INTERRUPTED = True
    if CURRENT_STATE_PATH and CURRENT_SRC and CURRENT_DST:
        update_file_state(CURRENT_STATE_PATH, Path(CURRENT_SRC), Path(CURRENT_DST), "interrupted")
    if CURRENT_LOG_PATH and CURRENT_SRC and CURRENT_DST:
        append_log(CURRENT_LOG_PATH, {"event": "interrupted", "src": CURRENT_SRC, "dst": CURRENT_DST})
    if CURRENT_PROC and CURRENT_PROC.poll() is None:
        try:
            os.kill(CURRENT_PROC.pid, signal.SIGSTOP)
        except Exception:
            pass

def sizeof_mb(path: Path) -> Optional[float]:
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else None

def format_time(s: float) -> str:
    m, s2 = divmod(int(s), 60)
    h, m2 = divmod(m, 60)
    if h:
        return f"{h}:{m2:02d}:{s2:02d}"
    return f"{m2:02d}:{s2:02d}"

def parse_time_str(t: str) -> float:
    parts = t.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return 0.0

def progress_pct(progress: ConversionProgress, total_frames: int) -> float:
    if not total_frames or not progress.frame:
        return 0.0
    return min(100.0, (progress.frame / total_frames) * 100.0)

def estimate_total_time(elapsed_sec: float, progress: ConversionProgress, total_duration_sec: float) -> Optional[str]:
    """Estimate remaining wall-clock time at current encoding speed."""
    if not progress.speed or progress.speed < 0.1:
        return "?"
    processed = parse_time_str(progress.time_str)
    remaining_video = max(0, total_duration_sec - processed)
    if remaining_video <= 0 or progress.speed <= 0:
        return "?"
    remaining_wall = remaining_video / progress.speed
    if remaining_wall > 99999:
        return "?"
    return format_time(remaining_wall)


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
    use_tui = not args.plain and as_bool(cfg_get(cfg, "report", "tui", True), True)
    return {"output_dir": output_dir, "ffmpeg_bin": ffmpeg_bin, "ffprobe_bin": ffprobe_bin, "recursive": recursive, "resume": resume, "dry_run": args.dry_run, "vt_quality": vt_quality, "x265_preset": x265_preset, "x265_crf_hdr": x265_crf_hdr, "sdr_encoder": sdr_encoder, "hdr_encoder": hdr_encoder, "dovi_policy": dovi_policy, "force_encoder": force_encoder, "audio_mode": audio_mode, "subtitle_mode": subtitle_mode, "keep_audio_langs": keep_audio_langs, "keep_sub_langs": keep_sub_langs, "allow_sdh_fallback": allow_sdh_fallback, "console_style": console_style, "write_json": write_json, "write_jsonl": write_jsonl, "report_filename": report_filename, "mp4_chapters": mp4_chapters, "map_metadata": map_metadata, "use_tui": use_tui}


def print_effective_config(eff: Dict[str, Any], config_path: Path) -> None:
    print(json.dumps({"config_path": str(config_path), **eff}, indent=2, ensure_ascii=False))


def summarize(results: List[Dict[str, Any]], report_path: Optional[Path], console_style: str) -> None:
    total = len(results)
    converted = sum(1 for r in results if r.get("status") == FileState.DONE)
    failed = sum(1 for r in results if r.get("status") == FileState.FAILED)
    skipped = sum(1 for r in results if (r.get("status") or "").startswith("skipped"))
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


# ──────────────────────── TUI (Textual) ────────────────────────

class _QueueEntry:
    def __init__(self, src: Path):
        self.src = src
        self.status = FileState.PENDING
        self.dst: str = ""
        self.classification: str = ""
        self.encoder_key: str = ""
        self.has_dovi: bool = False
        self.is_hdr: bool = False
        self.error: str = ""
        self.elapsed_sec: float = 0.0

def _make_tui_app():
    """Factory that returns the TUI App class. Defined here so textual is only imported when needed."""
    App, ComposeResult, Binding, Container, Footer, Header, Label, ProgressBarTui, Static, RichLog = _load_textual()

    CSS_CODE = """
    Screen {
        layout: grid;
        grid-size: 2;
        grid-columns: 35% 65%;
    }

    #left-panel {
        border: solid green;
        padding: 1;
        background: $surface;
    }

    #right-panel {
        border: solid blue;
        padding: 1;
        background: $surface;
    }

    #queue-banner {
        dock: bottom;
        height: 1;
        content-align: center middle;
    }

    #stats-header {
        height: auto;
        margin-bottom: 1;
    }

    #progress-section {
        height: auto;
        margin-bottom: 1;
    }

    #metrics-section {
        height: auto;
        margin-bottom: 1;
    }

    #detail-section {
        height: auto;
        border-top: solid $accent;
        padding-top: 1;
    }

    ProgressBar {
        height: 3;
        border: blank;
    }

    .file-done { color: green; }
    .file-failed { color: red; }
    .file-paused { color: yellow; text-style: bold; }
    .file-running { color: cyan; text-style: bold; }
    .file-pending { color: $text-muted; }

    #pause-overlay {
        display: none;
        dock: top;
        height: 2;
        content-align: center middle;
        background: $surface-darken-3;
        color: yellow;
        text-style: bold;
    }

    #pause-overlay.visible { display: block; }
"""

    class _CuratorTui(App[None]):
        BINDINGS = [
            Binding("q", "quit", "Quit batch"),
            Binding("space", "pause_resume", "Pause / Resume"),
            Binding("r", "restart_file", "Restart current file"),
        ]

        CSS = CSS_CODE

        def __init__(self, inp_root: Path, files: List[Path], output_dir: Optional[Path],
                     root: Path, sfile: Path, lfile: Path, eff: Dict[str, Any],
                     audio_codec: str, prev_files: Dict[str, Any]):
            super().__init__()
            self.inp_root = inp_root
            self.files_init = files
            self.output_dir = output_dir
            self.root = root
            self.sfile = sfile
            self.lfile = lfile
            self.eff = eff
            self.audio_codec = audio_codec
            self.queue: List[_QueueEntry] = []
            self.current_idx: int = -1
            self.pause_event = threading.Event()
            self.running = False
            self.cancelled = False
            self.progress = ConversionProgress()
            self.total_frames: int = 0
            self.total_duration_sec: float = 3600.0
            self.prev_files = prev_files

        def compose(self) -> ComposeResult:
            yield Header()
            with Container(id="left-panel"):
                yield Label("  FILES TO PROCESS", id="left-title")
                yield Static("", id="file-list")
                yield Label("", id="queue-banner", classes="file-pending")
            with Container(id="right-panel"):
                yield Label("  PAUSED — press Space to resume", id="pause-overlay")
                yield Static("", id="stats-header")
                with Container(id="progress-section"):
                    yield ProgressBarTui(total=100, show_eta=True)
                with Container(id="metrics-section"):
                    yield Label("", id="metric-fps")
                    yield Label("", id="metric-speed")
                    yield Label("", id="metric-bitrate")
                with Container(id="detail-section"):
                    yield Static("", id="detail-classification")
            yield Footer()

        def on_mount(self) -> None:
            self.build_queue()
            self.query_one("#left-panel").styles.height = "100%"
            self.query_one("#right-panel").styles.height = "100%"
            self._start_file_watcher()
            self._run_worker_thread()

        def _start_file_watcher(self) -> None:
            t = threading.Thread(target=self._poll_new_files, daemon=True)
            t.start()

        def _poll_new_files(self) -> None:
            seen = {str(e.src) for e in self.queue}
            try:
                while True:
                    time.sleep(3)
                    if self.cancelled:
                        break
                    new_files = find_files(self.inp_root, self.eff.get("recursive", False))
                    for f in new_files:
                        if str(f) not in seen:
                            self.call_from_thread(self._add_new_file, f)
                    seen = {str(e.src) for e in self.queue}
            except Exception:
                pass

        def _add_new_file(self, src: Path) -> None:
            entry = _QueueEntry(src)
            dst = output_path_for(src, self.inp_root, self.output_dir)
            entry.dst = str(dst)
            if dst.exists():
                entry.status = FileState.SKIPPED_EXISTS
            self.queue.append(entry)
            self._render_file_list()

        def build_queue(self) -> None:
            for src in self.files_init:
                entry = _QueueEntry(src)
                dst = output_path_for(src, self.inp_root, self.output_dir)
                entry.dst = str(dst)
                if self.eff["resume"]:
                    prev = self.prev_files.get(str(src), {})
                    if prev.get("status") == FileState.DONE:
                        entry.status = FileState.SKIPPED_EXISTS
                    elif prev.get("status") in (FileState.RUNNING, FileState.PAUSED):
                        if dst.exists():
                            dst.unlink()
                if entry.status == FileState.PENDING:
                    if not self.eff["resume"] and dst.exists():
                        entry.status = FileState.SKIPPED_EXISTS
                self.queue.append(entry)

        def _run_worker_thread(self) -> None:
            self.running = True
            t = threading.Thread(target=self._process_queue, daemon=True)
            t.start()

        def _process_queue(self) -> None:
            idx = 0
            while idx < len(self.queue):
                if self.cancelled:
                    break
                entry = self.queue[idx]
                if entry.status in (FileState.SKIPPED_EXISTS, FileState.SKIPPED_DOVI):
                    self.call_from_thread(self._on_file_done, idx)
                    idx += 1
                    continue

                self.current_idx = idx
                entry.status = FileState.RUNNING

                dst = Path(entry.dst) if entry.dst else output_path_for(
                    entry.src, self.inp_root, self.output_dir)
                if not entry.dst:
                    entry.dst = str(dst)

                if self.eff.get("dry_run"):
                    entry.status = "planned"
                    entry.dst = str(dst)
                    self.call_from_thread(self._on_file_done, idx)
                    idx += 1
                    continue

                # Run ffprobe in worker thread to avoid blocking the UI
                try:
                    meta = ffprobe_json(self.eff["ffprobe_bin"], entry.src)
                    plan = build_plan(meta, self.eff)
                    video_stream = plan.get("video", {})
                    fps_str = video_stream.get("r_frame_rate", "25/1")
                    if "/" in fps_str:
                        num, den = fps_str.split("/")
                        est_fps = float(num) / max(1, int(den))
                    else:
                        est_fps = float(fps_str) if fps_str else 25.0
                    duration_str = meta.get("format", {}).get("duration", "0")
                    total_duration_sec = float(duration_str) if duration_str else 3600.0
                    total_frames = max(1, int(total_duration_sec * est_fps))
                except Exception:
                    meta = plan = None
                    total_frames = 100000
                    total_duration_sec = 3600.0

                self.call_from_thread(self._on_file_running, idx, plan, total_frames, total_duration_sec)

                prev_frame = 0
                def on_progress(prog: ConversionProgress, _idx: int = idx) -> None:
                    nonlocal prev_frame
                    if prog.frame > prev_frame:
                        prev_frame = prog.frame
                        self.call_from_thread(self._on_progress_update, _idx, prog)

                try:
                    row = convert_one(
                        entry.src, dst, self.eff, self.audio_codec,
                        self.sfile, self.lfile,
                        on_progress=on_progress,
                        pause_event=self.pause_event,
                        meta=meta, plan=plan)

                    entry.status = row.get("status", FileState.FAILED)
                    entry.classification = row.get("classification", "")
                    entry.encoder_key = row.get("encoder_key", "")
                    entry.has_dovi = row.get("has_dovi", False)
                    entry.is_hdr = row.get("is_hdr", False)
                    entry.elapsed_sec = row.get("elapsed_sec", 0.0)

                except Exception as exc:
                    entry.status = FileState.FAILED
                    entry.error = str(exc)

                self.call_from_thread(self._on_file_done, idx)
                idx += 1

            self.current_idx = -1
            self.running = False
            self.call_from_thread(self._on_batch_done)

        def _on_file_running(self, idx: int, plan: Optional[dict], total_frames: int, total_duration_sec: float) -> None:
            entry = self.queue[idx]
            if plan:
                entry.classification = plan.get("classification", "")
                entry.encoder_key = plan.get("encoder_key", "")
                entry.has_dovi = plan.get("has_dovi", False)
                entry.is_hdr = plan.get("is_hdr", False)
            self.total_frames = max(1, total_frames)
            self.total_duration_sec = total_duration_sec
            entry.status = FileState.RUNNING
            self.progress = ConversionProgress()
            self._set_detail(entry)
            self._render_file_list()

        def _on_progress_update(self, idx: int, prog: ConversionProgress) -> None:
            pct = progress_pct(prog, self.total_frames) if self.total_frames else 0.0
            bar = self.query_one(ProgressBarTui)
            bar.update(total=max(self.total_frames, 1), progress=prog.frame)

            self.query_one("#metric-fps").update(
                f"    FPS: {prog.fps:.1f}  |  Speed: {prog.speed:.2f}x")
            self.query_one("#metric-speed").update(
                f"    Bitrate: {prog.bitrate_kbps:.0f} kbps  |  "
                f"Frames: {prog.frame}")

            entry = self.queue[idx] if 0 <= idx < len(self.queue) else None
            if entry and hasattr(entry, "_start_time"):
                base = Path(entry.src).name.rsplit(".", 1)[0]
                elapsed = time.time() - entry._start_time
                eta = estimate_total_time(elapsed, prog, self.total_duration_sec)
                self.query_one("#stats-header").update(
                    f"  {base}\n"
                    f"  Elapsed: {format_time(elapsed)}  |  ETA: {eta}")

        def _on_file_done(self, idx: int) -> None:
            entry = self.queue[idx]
            self._render_file_list()

            if not INTERRUPTED:
                self.current_idx = -1
                if entry.status in (FileState.DONE, FileState.SKIPPED_EXISTS):
                    self._set_done(entry)
                elif entry.status == FileState.FAILED:
                    self._set_failed(entry)

        def _on_batch_done(self) -> None:
            results = []
            for entry in self.queue:
                row = {
                    "src": str(entry.src),
                    "dst": entry.dst,
                    "status": entry.status,
                    "classification": entry.classification,
                    "encoder_key": entry.encoder_key,
                    "has_dovi": entry.has_dovi,
                    "is_hdr": entry.is_hdr,
                }
                if entry.elapsed_sec:
                    row["elapsed_sec"] = round(entry.elapsed_sec, 2)
                results.append(row)

            write_reports(self.root, results, self.eff)

            done = sum(1 for e in self.queue if e.status == FileState.DONE)
            failed = sum(1 for e in self.queue if e.status == FileState.FAILED)
            skipped = sum(1 for e in self.queue if e.status.startswith("skipped"))
            total = len(self.queue)
            banner = self.query_one("#queue-banner")
            if failed:
                banner.update(f"  {done}/{total} done  |  {failed} failed  |  {skipped} skipped")
                banner.classes = "file-failed"
            else:
                banner.update(f"  {done}/{total} done  |  {skipped} skipped")
                banner.classes = "file-done"

            self.query_one("#stats-header").update("  BATCH COMPLETE")

        def _render_file_list(self) -> None:
            widgets = []
            for i, entry in enumerate(self.queue):
                status_icon = {
                    FileState.PENDING: "•  ",
                    FileState.QUEUED: "◌  ",
                    FileState.RUNNING: "▶  ",
                    FileState.PAUSED: "▌  ",
                    FileState.DONE: "✓   ",
                    FileState.SKIPPED_EXISTS: "⊘  ",
                    FileState.SKIPPED_DOVI: "⊘  ",
                    FileState.FAILED: "✗   ",
                }.get(entry.status, "?  ")

                name = Path(entry.src).name.rsplit(".", 1)[0]
                if i == self.current_idx:
                    line = f"[bold cyan]{status_icon}{name}[/]"
                else:
                    cls = {
                        FileState.DONE: "file-done",
                        FileState.FAILED: "file-failed",
                        FileState.PAUSED: "file-paused",
                        FileState.RUNNING: "file-running",
                    }.get(entry.status, "file-pending")
                    line = f"[{cls}]{status_icon}{name}[/]"

                widgets.append(Label(line))

            done = sum(1 for e in self.queue if e.status == FileState.DONE)
            failed = sum(1 for e in self.queue if e.status == FileState.FAILED)
            skipped = sum(1 for e in self.queue if e.status.startswith("skipped"))
            total = len(self.queue)

            fl = self.query_one("#file-list")
            for w in list(fl.children):
                w.remove()
            for w in widgets:
                fl.mount(w)

            banner = self.query_one("#queue-banner")
            if failed:
                banner.update(f"  {done}/{total} done  |  {failed} failed  |  {skipped} skipped")
                banner.classes = "file-failed"
            else:
                banner.update(f"  {done}/{total} done  |  {skipped} skipped")
                banner.classes = "file-done"

        def _set_detail(self, entry: _QueueEntry) -> None:
            base = Path(entry.src).name.rsplit(".", 1)[0]
            entry._start_time = time.time()

            self.query_one("#stats-header").update(
                f"  {base}\n"
                f"  Elapsed: --:--  |  ETA: ?")

            self.query_one("#detail-classification").update(
                f"  Class: {entry.classification or '?'}\n"
                f"  Encoder: {entry.encoder_key or '?'}")

            bar = self.query_one(ProgressBarTui)
            bar.update(total=1, progress=0)

        def _set_done(self, entry: _QueueEntry) -> None:
            base = Path(entry.src).name.rsplit(".", 1)[0]
            self.query_one("#stats-header").update(
                f"  ✓ {base}\n"
                f"  Completed in {format_time(entry.elapsed_sec)}")

        def _set_failed(self, entry: _QueueEntry) -> None:
            base = Path(entry.src).name.rsplit(".", 1)[0]
            err = (entry.error or "")[:80]
            self.query_one("#stats-header").update(
                f"  ✗ {base}\n"
                f"  Failed: {err}")

        def action_pause_resume(self) -> None:
            if not self.running or self.current_idx < 0:
                return
            entry = self.queue[self.current_idx]

            if self.pause_event.is_set():
                self.pause_event.clear()
                entry.status = FileState.RUNNING
                self.query_one("#pause-overlay").remove_class("visible")
                update_file_state(self.sfile, entry.src, Path(entry.dst), FileState.RUNNING)
            else:
                self.pause_event.set()
                entry.status = FileState.PAUSED
                self.query_one("#pause-overlay").add_class("visible")
                update_file_state(self.sfile, entry.src, Path(entry.dst), FileState.PAUSED)

            self._render_file_list()

        def action_quit(self) -> None:
            if self.running and CURRENT_PROC and CURRENT_PROC.poll() is None:
                try:
                    os.kill(CURRENT_PROC.pid, signal.SIGSTOP)
                except Exception:
                    pass

            self.cancelled = True
            global INTERRUPTED
            INTERRUPTED = True

            if self.current_idx >= 0:
                entry = self.queue[self.current_idx]
                if entry.status == FileState.RUNNING:
                    update_file_state(self.sfile, entry.src, Path(entry.dst), "interrupted")

            self.exit()

        def action_restart_file(self) -> None:
            if self.current_idx < 0:
                return
            entry = self.queue[self.current_idx]
            if entry.dst and Path(entry.dst).exists():
                Path(entry.dst).unlink()
            entry.status = FileState.PENDING

    return _CuratorTui


def run_tui(inp_root: Path, files: List[Path], output_dir: Optional[Path],
            root: Path, sfile: Path, lfile: Path, eff: Dict[str, Any],
            audio_codec: str, prev_files: Dict[str, Any]) -> int:
    """Launch the TUI and return exit code after it closes."""
    CuratorTuiCls = _make_tui_app()
    app = CuratorTuiCls(inp_root, files, output_dir, root, sfile, lfile, eff, audio_codec, prev_files)
    app.run()

    if INTERRUPTED:
        print("Interrupted by user — use --resume to continue.", file=sys.stderr)
        return 130

    failed = sum(1 for e in app.queue if e.status == FileState.FAILED)
    return 1 if failed else 0


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
    parser.add_argument("--plain", action="store_true", help="Use plain terminal output instead of TUI")
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

    ffmpeg_ok = shutil.which(eff["ffmpeg_bin"]) is not None or Path(eff["ffmpeg_bin"]).exists()
    ffprobe_ok = shutil.which(eff["ffprobe_bin"]) is not None or Path(eff["ffprobe_bin"]).exists()
    if not ffmpeg_ok:
        print(f"ffmpeg not found: {eff['ffmpeg_bin']}", file=sys.stderr); return 2
    if not ffprobe_ok:
        print(f"ffprobe_bin not found: {eff['ffprobe_bin']}", file=sys.stderr); return 2

    inp = Path(args.input)
    output_dir = Path(eff["output_dir"]) if eff["output_dir"] else None
    root = output_root_for(inp, output_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Error: cannot create output directory {root}: {e}", file=sys.stderr)
        return 2
    sfile = state_file(root); lfile = log_file(root)

    # Read existing state for resume
    prev_state = read_state(sfile) if eff["resume"] else {"files": {}}
    prev_files: Dict[str, Any] = prev_state.get("files", {})

    files = find_files(inp, eff["recursive"])
    if not files:
        print("No MKV files found", file=sys.stderr); return 1

    if eff["use_tui"]:
        # TUI mode
        return run_tui(inp, files, output_dir, root, sfile, lfile, eff, args.audio_codec, prev_files)

    # Plain mode
    failures = 0
    results = []
    for src in files:
        if INTERRUPTED:
            break

        dst = output_path_for(src, inp, output_dir)

        # Resume logic: skip completed files from previous session
        prev_entry = prev_files.get(str(src), {})
        if eff["resume"] and prev_entry.get("status") == FileState.DONE:
            row = {"src": str(src), "dst": str(dst), "status": FileState.SKIPPED_EXISTS}
            print(f"SKIP (resumed, previously done): {dst}")
            results.append(row)
            continue

        # Clean up corrupt output if previous session crashed here
        if eff["resume"] and prev_entry.get("status") in (FileState.RUNNING, FileState.PAUSED):
            if dst.exists():
                print(f"Removing corrupt output from previous crash: {dst}")
                try:
                    dst.unlink()
                except OSError as e:
                    print(f"Warning: could not remove corrupt output {dst}: {e}", file=sys.stderr)

        # Skip if already done (output exists)
        if not eff["resume"] and dst.exists():
            row = {"src": str(src), "dst": str(dst), "status": FileState.SKIPPED_EXISTS}
            update_file_state(sfile, src, dst, FileState.SKIPPED_EXISTS)
            append_log(lfile, {"event": "skipped_exists", "src": str(src), "dst": str(dst)})
            print(f"SKIP existing output: {dst}")
            results.append(row)
            continue

        try:
            meta = ffprobe_json(eff["ffprobe_bin"], src)
            plan = build_plan(meta, eff)

            for line in report_lines(src, dst, plan, eff):
                print(line)

            if eff["dry_run"]:
                cmd = build_cmd(eff["ffmpeg_bin"], src, dst, plan, args.audio_codec, eff)
                print("  Planned command:")
                print("   ", " ".join(cmd)); print()
                row = {"src": str(src), "dst": str(dst), "status": "planned",
                       "classification": plan["classification"], "encoder_key": plan["encoder_key"],
                       "has_dovi": plan["has_dovi"], "is_hdr": plan["is_hdr"]}
                results.append(row)
                continue

            row = convert_one(src, dst, eff, args.audio_codec, sfile, lfile, meta=meta, plan=plan)
            if row.get("status") == FileState.DONE:
                print(f"WROTE: {dst}\n")
            elif row.get("status") == FileState.FAILED:
                failures += 1
                print(f"FAILED: {src}", file=sys.stderr)

            results.append(row)

        except Exception as exc:
            failures += 1
            row = {"src": str(src), "dst": str(dst), "status": FileState.FAILED, "error": str(exc)}
            results.append(row)
            print(f"FAILED: {src}: {exc}", file=sys.stderr)

    report_path = write_reports(root, results, eff)
    summarize(results, report_path, eff["console_style"])
    if INTERRUPTED:
        print("Interrupted by user", file=sys.stderr); return 130
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
