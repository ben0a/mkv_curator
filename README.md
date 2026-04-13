# mkv_curator

A pragmatic, **macOS-first** MKV-to-M4V batch conversion tool built around Apple's VideoToolbox. It curates your media library by ensuring **HEVC-only video output**, enforcing language-aware stream selection, and applying a predictable, policy-based approach for HDR and Dolby Vision titles.

## What it does

`mkv_curator` scans a single MKV file or an entire directory recursively. It preserves the video stream (transcoding to HEVC via hardware acceleration if needed), converts audio to AAC, converts text subtitles to Apple-compatible `mov_text`, and outputs clean `.m4v` files.

Main goals:
- **macOS / Apple Ecosystem Focus:** Built specifically to leverage macOS `hevc_videotoolbox` for lightning-fast encoding and target Apple-compatible M4V containers.
- **Opinionated Curation:** Automatically prefers French and English streams. Drops descriptive audio, VFQ (Quebec French), and bitmap subtitles (like PGS) that clutter or break MP4/M4V compatibility.
- **Smart HDR/DOVI Handling:** Applies a configurable policy for HDR and Dolby Vision rather than a one-size-fits-all approach.
- **Resilient Batch Processing:** Generates colorful terminal summaries, structured JSON reports, and uses state/log files to ensure you can safely pause and resume large batch runs.

## Current Video Policy

Default encoding policy in `v0.8`:

| Source type | Default action |
|---|---|
| SDR | `videotoolbox_8bit` |
| HDR without Dolby Vision | `videotoolbox_10bit` |
| HDR with Dolby Vision | `videotoolbox_10bit` (Configurable to `libx265_10bit` or `skip`) |

Dolby Vision titles behave unpredictably across different players. The default `videotoolbox_10bit` policy is a pragmatic choice for reducing file size while keeping 4K outputs in BT.2020/PQ signaling. You can easily set DOVI files to `skip` in the config if you prefer to review them manually.

## Requirements

This tool is designed specifically for **macOS**.

- **Python 3.11+** (using `uv` is recommended to run the script).
- **FFmpeg & FFprobe**: Must be installed with VideoToolbox support. A standard Homebrew installation is perfect:

```bash
brew install ffmpeg
```

To verify your FFmpeg supports the required hardware encoder, run:

```
ffmpeg -hide_banner -encoders | grep videotoolbox
```
(If you want to use the optional libx265_10bit fallback policy, ensure libx265 is also listed in your encoders).

## Installation

Clone the repository and set up your configuration:

```
git clone https://github.com/ben0a/mkv_curator.git
cd mkv_curator

# Recommended: setup the default config folder
mkdir -p ~/.config/mkv_curator
cp config.example.toml ~/.config/mkv_curator/config.toml
```
If you prefer not to use the default config location, pass a custom file using the --config flag.

## Quick start

Convert a single file:
```
uv run mkv_curator.py --input "/path/to/movie.mkv"
```
Dry-run a single file (shows planned FFmpeg commands without executing):
```
uv run mkv_curator.py --input "/path/to/movie.mkv" --dry-run
```
Convert a directory recursively:
```
uv run mkv_curator.py --input "/path/to/library" --recursive
```
Print the effective config:
```
uv run mkv_curator.py --input "/path/to/movie.mkv" --print-effective-config
```

## Stream Selection Rules (Default)

By default, mkv_curator is highly opinionated:
- Audio: Keeps only French and English. Drops descriptive audio and VFQ (Quebec) tracks. Converts to AAC (Stereo, with an optional fallback to 5.1/7.1 if the source has multichannel audio).
- Subtitles: Keeps only French and English text subtitles (SRT, ASS, etc.). Drops bitmap subtitles (PGS, VobSub) entirely to avoid burn-in. Excludes SDH/HI subtitles unless allow_sdh_fallback is enabled.
- Chapters: MP4 chapters are dropped by default to prevent FFmpeg from generating unwanted bin_data streams.

## Output & Reporting

A typical run generates:
- The converted .m4v video files.
- mkv_curator.state.json (tracks progress so you can resume interrupted runs).
- mkv_curator.log.jsonl (detailed event log).
- mkv_curator_report.json & mkv_curator_report.jsonl (final summary of the batch).

The JSON reports contain per-file metadata (source, output, detected class like hdr_4k_dovi, chosen policy, conversion status, output size, elapsed time, and warnings) making it easy to parse the results later with other tools.

