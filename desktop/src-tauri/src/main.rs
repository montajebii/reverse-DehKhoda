// Dehkhoda RAG — Tauri v2 shell.
//
// Responsibilities:
//   1. pick a free 127.0.0.1 port
//   2. spawn the PyInstaller sidecar (bundled onedir resource) with --port
//   3. poll GET /health until the FastAPI app reports ready
//   4. expose the port to the UI via the `backend_port` command
//   5. kill the sidecar when the app exits

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, State};

struct SidecarState {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
}

#[derive(Serialize)]
struct BackendStatus {
    port: Option<u16>,
    ready: bool,
}

fn sidecar_dir(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    // tauri.conf.json -> bundle.resources maps "resources/dehkhoda-server/"
    // into the resource dir as "dehkhoda-server/".
    app.path()
        .resource_dir()
        .map(|r| r.join("dehkhoda-server"))
        .map_err(|e| format!("resource_dir: {e}"))
}

fn sidecar_exe(dir: &std::path::Path) -> std::path::PathBuf {
    #[cfg(target_os = "windows")]
    let name = "dehkhoda-server.exe";
    #[cfg(not(target_os = "windows"))]
    let name = "dehkhoda-server";
    dir.join(name)
}

fn find_dev_sidecar(mut p: std::path::PathBuf) -> Option<std::path::PathBuf> {
    // Dev fallback: backend/dist/dehkhoda-server/ relative to repo root.
    for _ in 0..4 {
        let cand = if cfg!(target_os = "windows") {
            p.join("backend/dist/dehkhoda-server/dehkhoda-server.exe")
        } else {
            p.join("backend/dist/dehkhoda-server/dehkhoda-server")
        };
        if cand.exists() {
            return Some(cand);
        }
        p = p.parent()?.to_path_buf();
    }
    None
}

fn spawn_backend(app: &tauri::AppHandle, port: u16) -> Result<Child, String> {
    let dir = sidecar_dir(app)?;
    let exe = sidecar_exe(&dir);
    let (bin, cwd) = if exe.exists() {
        (exe, Some(dir))
    } else {
        (
            find_dev_sidecar(std::env::current_dir().unwrap_or_default())
                .ok_or_else(|| format!("sidecar binary not found at {}", exe.display()))?,
            None,
        )
    };
    let mut cmd = Command::new(bin);
    cmd.arg("--port").arg(port.to_string());
    if let Some(d) = cwd {
        cmd.current_dir(d);
    }
    cmd.stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn sidecar: {e}"))
}

fn http_status(port: u16, path: &str, timeout: Duration) -> Option<u16> {
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let req = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = String::new();
    stream.read_to_string(&mut buf).ok()?;
    buf.split_whitespace().nth(1)?.parse().ok()
}

fn health_ok(port: u16) -> bool {
    matches!(
        http_status(port, "/health", Duration::from_secs(3)),
        Some(200)
    )
}

#[tauri::command]
fn backend_port(state: State<SidecarState>) -> u16 {
    state.port.lock().unwrap().unwrap_or(8300)
}

#[tauri::command]
fn backend_status(state: State<SidecarState>) -> BackendStatus {
    let port = *state.port.lock().unwrap();
    BackendStatus {
        port,
        ready: port.map(health_ok).unwrap_or(false),
    }
}

fn pick_free_port() -> u16 {
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8300)
}

fn backend_thread(handle: tauri::AppHandle) {
    let port = pick_free_port();
    match spawn_backend(&handle, port) {
        Ok(child) => {
            {
                let state = handle.state::<SidecarState>();
                *state.child.lock().unwrap() = Some(child);
                *state.port.lock().unwrap() = Some(port);
            }
            // Up to 5 min: first launch may also hydrate caches.
            let deadline = Instant::now() + Duration::from_secs(300);
            while Instant::now() < deadline {
                if health_ok(port) {
                    println!("dehkhoda-server ready on 127.0.0.1:{port}");
                    return;
                }
                std::thread::sleep(Duration::from_millis(500));
            }
            eprintln!("backend health check timed out on port {port}");
        }
        Err(e) => eprintln!("failed to start backend: {e}"),
    }
}

fn kill_child(state: &SidecarState) {
    if let Some(mut c) = state.child.lock().unwrap().take() {
        let _ = c.kill();
        let _ = c.wait();
    }
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            app.manage(SidecarState {
                child: Mutex::new(None),
                port: Mutex::new(None),
            });
            let handle = app.handle().clone();
            std::thread::spawn(move || backend_thread(handle));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_port, backend_status])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                kill_child(&app_handle.state::<SidecarState>());
            }
        });
}
