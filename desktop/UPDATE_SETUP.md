# Enabling auto-updates

1. Generate and protect a Tauri updater signing key.
2. Publish a signed update manifest and installer on an HTTPS endpoint you control.
3. Add `tauri-plugin-updater` to `src-tauri/Cargo.toml`, configure its public key and
   endpoint in `tauri.conf.json`, and enable the `check_for_update` function in Rust.
4. Test an upgrade from an installed older version before publishing.

The initial desktop project deliberately does not include a public endpoint or key:
those values are release infrastructure, not application source code.
