<p align="center">
  <a href="https://keyless.ohrats.party">
    <img src="assets/header.svg" alt="Keyless" width="100%" />
  </a>
</p>

<p align="center">
  <em>An experimental, local-first desktop-control project for Linux: low-latency voice control today, stereo-gaze target intent under active research.</em>
</p>

> **Experimental.** Keyless is in an active research phase, not a shipped gaze product.
> Voice control, STT, the platform seams, and Linux packaging are implemented and usable.
> The gaze engine is being rebuilt from first principles as *target-level intent* (see
> [`docs/GREENFIELD.md`](docs/GREENFIELD.md)); no gaze model is trained or running yet, and the
> real pointer is never moved implicitly. Expect breaking changes.

<p align="center">
  <a href="https://keyless.ohrats.party">Website</a> ·
  <a href="https://keyless.ohrats.party/docs.html">Docs</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/RELEASES.md">Releases</a>
</p>

Say what you mean, and Keyless acts on it. Speech recognition (Parakeet TDT 0.6B v2 int8), command history, and camera capture all stay on your machine. Gaze-based target selection is the research goal: the project is instrumenting the data flywheel and measuring perception before any gaze model is trained.

## Install (Linux)

```sh
curl -fsSL https://keyless.ohrats.party/install.sh | sh
```

On Debian and Ubuntu, this verifies `SHA256SUMS` and installs the self-contained `.deb` via `apt` (including the `/dev/uinput` udev rule, desktop entry, and optional systemd user unit). On other Linux distributions, it installs the AppImage for your user.

Open **Keyless** from your application menu — first run walks you through permissions, cameras, gaze-engine status, and the local speech model with no terminal or JSON editing required.

## CLI & Diagnostics

```sh
keyless doctor
keyless doctor --json
keyless models status
keyless run --dry-run
```

## Development

Install locally in ~1 second from a clone (editable mode; changes apply immediately):

```sh
./scripts/install.sh --open
```

Uninstall (pass `--purge` to also delete settings and models):

```sh
./scripts/uninstall.sh
```

Run tests (the default suite skips the slow Qt UI and model/vision tests):

```sh
uv run pytest            # fast suite
uv run pytest -m ui      # UI windows, overlays, and golden images
uv run pytest -m ""      # everything
```

Build `.deb` and AppImage packages:

```sh
uv run python packaging/linux/build_deb.py --appimage
```

See [`docs/architecture.md`](docs/architecture.md) and [`docs/decisions.md`](docs/decisions.md) for architecture notes, benchmarks, and platform status.

## License

See [`LICENSE`](LICENSE).
