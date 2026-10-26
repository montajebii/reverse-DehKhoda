// Dehkhoda RAG — Tauri v2 shell.
//
// Responsibilities:
//   1. resolve the per-user data dir (no hardcoded paths)
//   2. read setup-state.json — decide wizard vs main UI
//   3. spawn the PyInstaller sidecar (bundled onedir resource) with --port
//   4. poll GET /health until the FastAPI app reports ready
//   5. expose `backend_port` / `setup_state` / `start_backend` /
//      `bootstrap_run` to the UI (wizard streams JSONL via `wizard-event`)
//   6. kill the sidecar when the app exits
//
// No console window is ever created for child processes on Windows
// (CREATE_NO_WINDOW on every spawn).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager, RunEvent, State};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

struct SidecarState {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
}

#[derive(Serialize)]
struct BackendStatus {
    port: Option<u16>,
    ready: bool,
}

// ---------------------------------------------------------------------------
// paths (mirror bootstrap/paths.py — the same conventions, no hardcoded hosts)
// ---------------------------------------------------------------------------
fn data_dir() -> std::path::PathBuf {
    if let Ok(v) = std::env::var("DEHKHODA_DATA_DIR") {
        let p = std::path::PathBuf::from(v);
        let _ = std::fs::create_dir_all(&p);
        return p;
    }
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var("LOCALAPPDATA")
            .unwrap_or_else(|_| {
                format!(
                    "{}\\AppData\\Local",
                    std::env::var("USERPROFILE").unwrap_or_default()
                )
            });
        let p = std::path::PathBuf::from(base).join("Dehkhoda");
        let _ = std::fs::create_dir_all(&p);
        p
    }
    #[cfg(not(target_os = "windows"))]
    {
        let base = std::env::var("XDG_DATA_HOME").unwrap_or_else(|_| {
            format!(
                "{}/.local/share",
                std::env::var("HOME").unwrap_or_else(|_| ".".into())
            )
        });
        let p = std::path::PathBuf::from(base).join("dehkhoda");
        let _ = std::fs::create_dir_all(&p);
        p
    }
}

fn state_file() -> std::path::PathBuf {
    data_dir().join("setup-state.json")
}

fn read_state() -> Value {
    std::fs::read_to_string(state_file())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Null)
}

// ---------------------------------------------------------------------------
// sidecar
// ---------------------------------------------------------------------------
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

fn logs_dir() -> std::path::PathBuf {
    let d = data_dir().join("logs");
    let _ = std::fs::create_dir_all(&d);
    d
}

fn open_backend_log() -> std::fs::File {
    let path = logs_dir().join("backend.log");
    std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&path)
        .unwrap_or_else(|e| panic!("cannot open backend log {}: {e}", path.display()))
}

fn spawn_cmd(bin: &std::path::Path, args: &[String]) -> Command {
    let mut cmd = Command::new(bin);
    cmd.args(args);
    cmd.env("DEHKHODA_DATA_DIR", data_dir());
    // The bundled Qdrant engine (sidecars/qdrant/qdrant.gz resource) is
    // resolved + sha256-verified by the Python launcher itself
    // (bootstrap/backend_launcher._qdrant_from_bundled_gz) — the shell
    // deliberately knows nothing about it.
    #[cfg(target_os = "windows")]
    {
        // 0x08000000 = CREATE_NO_WINDOW — never flash a shell window
        cmd.creation_flags(0x0800_0000);
    }
    cmd
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
    let args = vec!["--port".to_string(), port.to_string()];
    let mut cmd = spawn_cmd(&bin, &args);
    if let Some(d) = cwd {
        cmd.current_dir(d);
    }
    // stdout/stderr -> data_dir/logs/backend.log so engine failures are
    // diagnosable from the UI (never a bare "engine is not available").
    let log = open_backend_log();
    let err_log = log.try_clone().map_err(|e| format!("clone log handle: {e}"))?;
    cmd.stdout(Stdio::from(log)).stderr(Stdio::from(err_log));
    cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))
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

/// setup-state.json as raw JSON (wizard boot decision + idempotency).
#[tauri::command]
fn setup_state() -> Value {
    read_state()
}

/// Spawn the HTTP sidecar now (called by the wizard's final step).
#[tauri::command]
fn start_backend(app: tauri::AppHandle, state: State<SidecarState>) -> BackendStatus {
    {
        let child = state.child.lock().unwrap();
        if child.is_some() {
            let port = *state.port.lock().unwrap();
            return BackendStatus { port, ready: port.map(health_ok).unwrap_or(false) };
        }
    }
    let port = pick_free_port();
    match spawn_backend(&app, port) {
        Ok(c) => {
            *state.child.lock().unwrap() = Some(c);
            *state.port.lock().unwrap() = Some(port);
            let handle = app.clone();
            std::thread::spawn(move || {
                let deadline = Instant::now() + Duration::from_secs(300);
                while Instant::now() < deadline {
                    if health_ok(port) {
                        println!("dehkhoda-server ready on 127.0.0.1:{port}");
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(500));
                }
                eprintln!("backend health check timed out on port {port}");
                let _ = handle;
            });
        }
        Err(e) => eprintln!("failed to start backend: {e}"),
    }
    BackendStatus { port: Some(port), ready: false }
}

/// Tail of data_dir/logs/backend.log (engine diagnostics for the UI).
#[tauri::command]
fn backend_log() -> String {
    let path = logs_dir().join("backend.log");
    match std::fs::read(&path) {
        Ok(bytes) => {
            const MAX: usize = 48 * 1024;
            let slice = if bytes.len() > MAX { &bytes[bytes.len() - MAX..] } else { &bytes[..] };
            String::from_utf8_lossy(slice).into_owned()
        }
        Err(_) => String::new(),
    }
}

/// Run one wizard bootstrap step: spawn the sidecar in bootstrap mode and
/// stream its JSONL events to the UI via the `wizard-event` channel.
/// Returns the terminal {"t":"result",...} line (raw JSON string) or an
/// error JSON string.
///
/// Async + spawn_blocking: steps can run for hours (ingest) — a sync command
/// would block the Tauri main thread.
#[tauri::command]
async fn bootstrap_run(
    app: tauri::AppHandle,
    step: String,
    args: Option<Vec<String>>,
) -> String {
    tauri::async_runtime::spawn_blocking(move || {
        run_bootstrap_blocking(app, step, args.unwrap_or_default())
    })
    .await
    .unwrap_or_else(|e| json_err(&format!("join: {e}")))
}

fn run_bootstrap_blocking(
    app: tauri::AppHandle,
    step: String,
    extra: Vec<String>,
) -> String {
    let state = app.state::<SidecarState>();
    // never run wizard steps and the HTTP server sidecar simultaneously —
    // a running sidecar is killed first (it is stateless across restarts)
    kill_child(&state);

    let dir = match sidecar_dir(&app) {
        Ok(d) => d,
        Err(e) => return json_err(&e),
    };
    let exe = sidecar_exe(&dir);
    let (bin, cwd) = if exe.exists() {
        (exe, Some(dir))
    } else {
        match find_dev_sidecar(std::env::current_dir().unwrap_or_default()) {
            Some(b) => (b, None),
            None => {
                return json_err(&format!("sidecar binary not found at {}", exe.display()))
            }
        }
    };

    let mut cargs: Vec<String> = vec!["bootstrap".into(), "--step".into(), step];
    cargs.extend(extra);
    let mut cmd = spawn_cmd(&bin, &cargs);
    if let Some(d) = cwd {
        cmd.current_dir(d);
    }
    cmd.stdout(Stdio::piped()).stderr(Stdio::from(open_backend_log()));

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return json_err(&format!("spawn: {e}")),
    };
    let stdout = child.stdout.take();
    let mut final_line = json_err("no result event emitted");
    if let Some(out) = stdout {
        let reader = BufReader::new(out);
        for line in reader.lines().map_while(Result::ok) {
            let is_result = line.contains("\"t\":\"result\"");
            let payload = line.clone();
            let _ = app.emit("wizard-event", payload);
            if is_result {
                final_line = line;
            }
        }
    }
    let _ = child.wait();
    final_line
}

fn json_err(msg: &str) -> String {
    serde_json::json!({"t": "result", "ok": false, "error_code": "shell",
                       "error_fa": "خطای پوستهٔ برنامه", "detail": msg})
        .to_string()
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
            // Only auto-start the HTTP sidecar on a completed install; a
            // first-run user lands in the wizard which drives steps instead.
            let complete = read_state()
                .get("complete")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if complete {
                let handle = app.handle().clone();
                std::thread::spawn(move || backend_thread(handle));
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_port,
            backend_status,
            setup_state,
            start_backend,
            bootstrap_run,
            backend_log
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                kill_child(&app_handle.state::<SidecarState>());
            }
        });
}
