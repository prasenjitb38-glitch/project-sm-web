#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{net::TcpStream, thread, time::Duration};
use tauri::{menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem}, Manager};
use tauri_plugin_shell::ShellExt;

const SERVER_URL: &str = "http://127.0.0.1:5000";

#[cfg(not(debug_assertions))]
fn start_local_server(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let (_rx, _child) = app.shell().sidecar("project-sm-server")?.spawn()?;
    Ok(())
}

fn wait_for_server() -> bool {
    for _ in 0..60 {
        if TcpStream::connect("127.0.0.1:5000").is_ok() { return true; }
        thread::sleep(Duration::from_millis(250));
    }
    false
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            #[cfg(not(debug_assertions))]
            start_local_server(app)?;
            let refresh = MenuItemBuilder::with_id("refresh", "Refresh").build(app)?;
            let quit = PredefinedMenuItem::quit(app, Some("Quit Project SM"))?;
            let menu = MenuBuilder::new(app).item(&refresh).separator().item(&quit).build()?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, event| {
                if event.id().as_ref() == "refresh" {
                    if let Some(window) = app.get_webview_window("main") { let _ = window.eval("window.location.reload()"); }
                }
            });
            let window = app.get_webview_window("main").expect("main window missing");
            if wait_for_server() {
                let url = SERVER_URL.parse().expect("valid local URL");
                window.navigate(url)?;
            } else {
                window.eval(r#"document.body.innerHTML = `<main style="font-family:Arial;padding:2rem"><h1>Project SM could not start</h1><p>Please close the app and try again.</p></main>`;"#);
            }
            window.show()?;
            window.set_focus()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Project SM");
}
