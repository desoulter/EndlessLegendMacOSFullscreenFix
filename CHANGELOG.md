# Changelog

## 1.1.0 — 2026-11-07

- Optional post-swap frame cap for ordinary Steam launches, disabled by default;
  configured with `configure --fps-cap N` after updating the helper.
- Transactional `update` of an intact supported helper, preserving the player,
  graphics settings and original backups; `update --fps-cap 60` enables the cap.
- Compatible update from 1.0.0 with original backups and existing frame-cap
  preferences preserved; status and removal also accept intact 1.0.0 installs.
- Rendering diagnostics, in-game F8 measurement markers and controlled tutorial
  comparisons. The observer preserves the production MSAA clamp and frame cap.
- Installer rollback/configuration tests and native interposition/pacing tests,
  including the production limiter without the observer.

## 1.0.0 — 2026-11-06

First public release.

- Native macOS fullscreen with application switching.
- Explicit old-drawable detachment before Unity replaces its OpenGL context.
- Retina drawable support, pixel-based resolution enumeration, and selectable
  lower fullscreen resolutions while retaining the game's large interface.
- Driver-limit-aware MSAA allocation for both fullscreen color and depth/stencil
  buffers, fixing the black screen caused by an unsupported 8× request.
- Complete Objective-C source and Python build/install/status/uninstall workflow.
- Original-file and permissions backup, compatibility checks, rollback on ordinary
  write failures, and refusal to overwrite externally changed files or backups.
- Preserves and validates the native helper's `LC_UUID` command, required for
  loading it under macOS/Rosetta; detects affected earlier builds without UUID.
- Self-contained synthetic tests and optional tests against temporary copies of
  real game files.
- English and Russian instructions, technical explanation, and OpenGL reproductions.

Tested on Apple M4 Pro, macOS 26.6.2, Unity 5.3.6p1, ELCP 2.7.9 S3:
Retina, 2048×1330 with the large interface, and Cmd-Tab away and back.

The repository distributes source only. There is no experimental-version
migration or update command in this release.
