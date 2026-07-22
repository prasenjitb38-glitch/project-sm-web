# Project SM Desktop shell

This folder contains the Windows packaging layer. The Flask app remains the source of
truth for the user interface and chart API; Tauri starts it privately on `127.0.0.1`.

## Development

1. Create a Python environment and install `requirements.txt` plus `pyinstaller`.
2. Install Node.js LTS and Rust (MSVC toolchain).
3. Run `npm install`, then `npm run tauri:dev` from the project root.

## Windows installer

Run `npm install` and then `npm run tauri:build`. The build script first packages
the Python server and Tauri then creates an NSIS installer in `src-tauri/target/release/bundle/nsis`.

## Update framework

The Rust shell includes an update check hook but it is intentionally disabled until a
signed HTTPS release feed and public key are available. See `UPDATE_SETUP.md` before
enabling it: never ship an unsigned update endpoint.
