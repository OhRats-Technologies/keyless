<p align="center">
  <a href="https://keyless.ohrats.party">
    <img src="assets/header.svg" alt="Keyless" width="100%" />
  </a>
</p>

<p align="center">
  <em>Local-first stereo gaze, acoustic pop-click, and low-latency voice control for the Linux desktop.</em>
</p>

<p align="center">
  <a href="https://keyless.ohrats.party">Website</a> ·
  <a href="https://keyless.ohrats.party/docs.html">Docs</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/RELEASES.md">Releases</a>
</p>

Look at a target, say what you mean, and Keyless does the rest. Stereo cameras, speech recognition (Parakeet TDT 0.6B v2 int8), and command history all stay on your machine.

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
