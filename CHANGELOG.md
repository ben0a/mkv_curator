# Changelog

All notable changes to this project will be documented in this file.

## [0.8] - 2026-04-12

### Added
- Policy-based Dolby Vision handling with configurable `dovi_policy`.
- Rich terminal summary output.
- JSON and JSONL reporting for later reuse by other tools.
- State and JSONL log files for restart-safe processing.

### Changed
- Default Dolby Vision handling now uses `videotoolbox_10bit`.
- README updated for public GitHub publishing and Homebrew-friendly FFmpeg usage.
- Public config example now uses `ffmpeg` / `ffprobe` instead of a private local binary path.

### Fixed
- Default MP4 behavior now drops chapters and metadata to avoid the extra `bin_data` stream seen in earlier outputs.
