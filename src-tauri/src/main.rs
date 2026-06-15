// main.rs — the Tangent Agent native shell (Tauri 2.0). Thin by design: the Python
// daemon (the PyInstaller sidecar) does the work; this shell spawns it, transports a
// per-launch shared secret, exposes a native picker + reveal-in-OS, and stores the
// device token in the OS keychain. Inference is cloud-proxied — NO provider keys here.
//
// WS6 CONTRACT: the daemon validates TANGENT_LOCAL_SHARED_SECRET on mutating /api/*
// calls and rejects non-loopback Origin/Host (app.py _local_mode_guard). This shell only
// GENERATES the secret and passes it to both sides.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use rand::RngCore;
use tauri::{Manager, State};

const KEYRING_SERVICE: &str = "ai.tangent.agent";
const KEYRING_ACCOUNT: &str = "device-token";

struct Daemon(Mutex<Option<Child>>);

/// 32 random bytes, url-safe base64 — the per-launch loopback shared secret.
fn gen_secret() -> String {
    let mut buf = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut buf);
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
    URL_SAFE_NO_PAD.encode(buf)
}

/// Spawn the frozen Python sidecar (Tauri resolves the target-triple binary), pass it the
/// shared secret via env, and read TANGENT_DAEMON_PORT=<n> from its stdout. Returns
/// (port, secret) so the renderer can reach the loopback daemon with the secret header.
fn spawn_sidecar(app: &tauri::AppHandle, state: &Daemon) -> Result<(u16, String), String> {
    let secret = gen_secret();
    let sidecar = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("binaries/tangent-agentcore");
    let mut child = Command::new(sidecar)
        .env("LOCAL_MODE", "1")
        .env("RUN_SCHEDULER", "0")
        .env("TANGENT_LOCAL_SHARED_SECRET", &secret)
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|e| format!("could not start the agent core: {e}"))?;

    let mut port: u16 = 0;
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).lines().flatten() {
            if let Some(rest) = line.strip_prefix("TANGENT_DAEMON_PORT=") {
                port = rest.trim().parse().unwrap_or(0);
                break;
            }
        }
    }
    *state.0.lock().unwrap() = Some(child);
    if port == 0 {
        return Err("the agent core did not report a port".into());
    }
    Ok((port, secret))
}

// --- Renderer-callable commands (scoped by capabilities/default.json) -------------------
#[tauri::command]
fn store_device_token(token: String) -> Result<(), String> {
    // NEVER write the raw token to a file — only the OS keychain.
    keyring::Entry::new(KEYRING_SERVICE, KEYRING_ACCOUNT)
        .and_then(|e| e.set_password(&token))
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn get_device_token() -> Result<Option<String>, String> {
    match keyring::Entry::new(KEYRING_SERVICE, KEYRING_ACCOUNT).and_then(|e| e.get_password()) {
        Ok(t) => Ok(Some(t)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn reveal_in_os(path: String, app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .reveal_item_in_dir(path)
        .map_err(|e| e.to_string())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Daemon(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            store_device_token,
            get_device_token,
            reveal_in_os
        ])
        .setup(|app| {
            let state: State<Daemon> = app.state();
            let (port, secret) = spawn_sidecar(&app.handle(), &state)
                .expect("failed to start the Tangent Agent core");
            // The webview navigates to the loopback daemon; an init script injects the
            // shared secret as the X-Tangent-Local-Token header on every mutating fetch.
            let url = format!("http://127.0.0.1:{port}/");
            let win = app.get_webview_window("main").unwrap();
            win.eval(&format!("window.__LOCAL_TOKEN__ = '{secret}';"))?;
            win.navigate(url.parse().unwrap())?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // Graceful shutdown: kill the sidecar so no orphaned loopback server survives.
            if let tauri::WindowEvent::Destroyed = event {
                let state: State<Daemon> = window.state();
                if let Some(mut child) = state.0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Tangent Agent");
}
