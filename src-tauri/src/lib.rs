use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, BufWriter, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    thread,
    time::Duration,
};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_updater::UpdaterExt;

const APP_VERSION: &str = "2.0.3";
const MAX_HTTP_HEADER_BYTES: usize = 16 * 1024;
const MAX_HTTP_BODY_BYTES: usize = 1024 * 1024;
static TEMP_FILE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct AppState {
    data_dir: PathBuf,
    worker: Mutex<Worker>,
    browser_url: Mutex<Option<String>>,
}

struct Worker {
    child: Child,
    input: BufWriter<ChildStdin>,
    output: BufReader<ChildStdout>,
    next_id: u64,
}

#[derive(Deserialize)]
struct WorkerResponse {
    id: String,
    status: u16,
    body: Value,
}

#[derive(Serialize)]
struct ApiResponse {
    status: u16,
    body: Value,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AppUpdateStatus {
    available: bool,
    current_version: String,
    version: Option<String>,
    notes: Option<String>,
}

impl Worker {
    fn start(app: &tauri::App) -> Result<Self, String> {
        let mut command;
        if cfg!(debug_assertions) {
            let script = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .ok_or("Could not locate the IDLR project directory.")?
                .join("download_worker.py");
            command = Command::new("python");
            command.arg(script);
            command.current_dir(
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .ok_or("Could not locate the IDLR project directory.")?,
            );
        } else {
            #[cfg(target_os = "windows")]
            let worker_name = "idlr-worker.exe";
            #[cfg(not(target_os = "windows"))]
            let worker_name = "idlr-worker";
            let worker = app
                .path()
                .resource_dir()
                .map_err(|error| format!("Could not locate IDLR's download worker: {error}"))?
                .join("binaries")
                .join(worker_name);
            command = Command::new(worker);
        }
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Could not start IDLR's download worker: {error}"))?;
        let input = child
            .stdin
            .take()
            .ok_or("Could not open the download worker input.")?;
        let output = child
            .stdout
            .take()
            .ok_or("Could not open the download worker output.")?;
        Ok(Self {
            child,
            input: BufWriter::new(input),
            output: BufReader::new(output),
            next_id: 0,
        })
    }

    fn request(
        &mut self,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> Result<ApiResponse, String> {
        if self
            .child
            .try_wait()
            .map_err(|error| error.to_string())?
            .is_some()
        {
            return Err(
                "IDLR's download worker stopped unexpectedly. Restart IDLR and try again.".into(),
            );
        }
        self.next_id += 1;
        let id = self.next_id.to_string();
        let message = json!({"id": id, "method": method, "path": path, "body": body});
        serde_json::to_writer(&mut self.input, &message).map_err(|error| error.to_string())?;
        self.input
            .write_all(b"\n")
            .map_err(|error| error.to_string())?;
        self.input.flush().map_err(|error| error.to_string())?;

        let mut line = String::new();
        self.output
            .read_line(&mut line)
            .map_err(|error| format!("Could not read the download worker response: {error}"))?;
        if line.is_empty() {
            return Err("IDLR's download worker closed its response stream.".into());
        }
        let response: WorkerResponse = serde_json::from_str(&line)
            .map_err(|error| format!("Invalid worker response: {error}"))?;
        if response.id != id {
            return Err("IDLR's download worker returned an out-of-order response.".into());
        }
        Ok(ApiResponse {
            status: response.status,
            body: response.body,
        })
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn app_data_dir() -> Result<PathBuf, String> {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .ok_or("Could not find the current user's home directory.")?;
    let path = PathBuf::from(home).join(".idlr_app");
    fs::create_dir_all(&path)
        .map_err(|error| format!("Could not open IDLR data folder: {error}"))?;
    Ok(path)
}

fn read_json(path: &Path, fallback: Value) -> Result<Value, String> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map_err(|error| format!("Could not read {}: {error}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(fallback),
        Err(error) => Err(format!("Could not read {}: {error}", path.display())),
    }
}

fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|error| error.to_string())?;
    let file_name = path
        .file_name()
        .ok_or_else(|| "Could not determine the IDLR data file name.".to_string())?
        .to_string_lossy();
    let (temp_path, mut file) = (0..8)
        .find_map(|_| {
            let sequence = TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let temp_path = path.with_file_name(format!(
                ".{file_name}.{}.{}.tmp",
                std::process::id(),
                sequence
            ));
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp_path)
            {
                Ok(file) => Some(Ok((temp_path, file))),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => None,
                Err(error) => Some(Err(error)),
            }
        })
        .transpose()
        .map_err(|error| format!("Could not create temporary IDLR data file: {error}"))?
        .ok_or_else(|| "Could not create a unique temporary IDLR data file.".to_string())?;

    if let Err(error) = file.write_all(&bytes).and_then(|()| file.sync_all()) {
        drop(file);
        let _ = fs::remove_file(&temp_path);
        return Err(format!("Could not save IDLR data: {error}"));
    }
    drop(file);
    if let Err(error) = replace_file(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(format!("Could not finish saving IDLR data: {error}"));
    }
    Ok(())
}

#[cfg(not(windows))]
fn replace_file(temp_path: &Path, path: &Path) -> std::io::Result<()> {
    fs::rename(temp_path, path)
}

#[cfg(windows)]
fn replace_file(temp_path: &Path, path: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };

    let temp_path: Vec<u16> = temp_path.as_os_str().encode_wide().chain(Some(0)).collect();
    let path: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let result = unsafe {
        MoveFileExW(
            temp_path.as_ptr(),
            path.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn accounts_path(state: &AppState) -> PathBuf {
    state.data_dir.join("accounts.json")
}

fn history_path(state: &AppState) -> PathBuf {
    state.data_dir.join("history.json")
}

fn read_accounts(state: &AppState) -> Result<Map<String, Value>, String> {
    let value = read_json(&accounts_path(state), json!({}))?;
    value
        .as_object()
        .cloned()
        .ok_or_else(|| "IDLR account data is not a valid object.".to_string())
}

fn save_accounts(state: &AppState, accounts: &Map<String, Value>) -> Result<(), String> {
    write_json(&accounts_path(state), &Value::Object(accounts.clone()))
}

fn api_reply(status: u16, body: Value) -> ApiResponse {
    ApiResponse { status, body }
}

fn error_reply(status: u16, message: impl Into<String>) -> ApiResponse {
    api_reply(status, json!({"error": message.into()}))
}

fn get_string<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

fn user_response(email: &str, account: &Value) -> ApiResponse {
    api_reply(
        200,
        json!({
            "ok": true,
            "name": get_string(account, "name"),
            "email": email,
            "username": account.get("username").and_then(Value::as_str).unwrap_or(""),
            "bio": account.get("bio").and_then(Value::as_str).unwrap_or(""),
            "avatar": account.get("avatar").and_then(Value::as_str).unwrap_or(""),
            "banner": account.get("banner").and_then(Value::as_str).unwrap_or(""),
            "method": account.get("method").and_then(Value::as_str).unwrap_or("email"),
            "prefs": account.get("prefs").cloned().unwrap_or_else(|| json!({})),
            "joined": account.get("joined").and_then(Value::as_str).unwrap_or("")
        }),
    )
}

fn query_value(url: &str, key: &str) -> String {
    url.split_once('?')
        .map(|(_, query)| {
            url::form_urlencoded::parse(query.as_bytes())
                .find(|(name, _)| name == key)
                .map(|(_, value)| value.into_owned())
                .unwrap_or_default()
        })
        .unwrap_or_default()
}

fn request_path(url: &str) -> &str {
    url.split_once('?').map_or(url, |(path, _)| path)
}

fn browser_request_allowed(headers: &[(String, String)], port: u16) -> bool {
    let host = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("host"))
        .map(|(_, value)| value.as_str());
    let origin = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("origin"))
        .map(|(_, value)| value.as_str());
    let fetch_site = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("sec-fetch-site"))
        .map(|(_, value)| value.as_str());
    let allowed_hosts = [format!("127.0.0.1:{port}"), format!("localhost:{port}")];

    host.is_some_and(|host| allowed_hosts.iter().any(|allowed| host == allowed))
        && origin.is_none_or(|origin| {
            allowed_hosts
                .iter()
                .any(|host| origin == format!("http://{host}"))
        })
        && fetch_site.is_none_or(|site| site != "cross-site")
}

fn http_response(
    stream: &mut TcpStream,
    status: u16,
    content_type: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        500 => "Internal Server Error",
        _ => "Error",
    };
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\n\r\n",
        body.len()
    )?;
    stream.write_all(body)
}

fn browser_assets_dir(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    {
        let _ = app;
        Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("Could not locate the IDLR project directory.")?
            .join("frontend-dist"))
    }
    #[cfg(not(debug_assertions))]
    {
        app.path()
            .resource_dir()
            .map(|path| path.join("browser-ui"))
            .map_err(|error| format!("Could not locate IDLR's browser interface: {error}"))
    }
}

fn read_http_request(
    stream: &mut TcpStream,
) -> std::io::Result<(String, String, Vec<(String, String)>, Vec<u8>)> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut request_bytes = Vec::new();
    let mut chunk = [0u8; 4096];
    let header_end = loop {
        if let Some(position) = request_bytes
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
        {
            break position + 4;
        }
        if request_bytes.len() >= MAX_HTTP_HEADER_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "HTTP headers exceed the size limit.",
            ));
        }
        let count = stream.read(&mut chunk)?;
        if count == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "HTTP request ended before its headers.",
            ));
        }
        request_bytes.extend_from_slice(&chunk[..count]);
    };
    if header_end > MAX_HTTP_HEADER_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "HTTP headers exceed the size limit.",
        ));
    }
    let header_text = std::str::from_utf8(&request_bytes[..header_end - 4])
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    let mut lines = header_text.split("\r\n");
    let request_line = lines.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "Missing request line.")
    })?;
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid request line.")
    })?;
    let target = request_parts.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid request target.")
    })?;
    if request_parts.next().is_none() || request_parts.next().is_some() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "Invalid HTTP version.",
        ));
    }
    let headers = lines
        .filter_map(|line| {
            let (name, value) = line.split_once(':')?;
            Some((name.trim().to_string(), value.trim().to_string()))
        })
        .collect::<Vec<_>>();
    if headers
        .iter()
        .any(|(name, value)| name.eq_ignore_ascii_case("transfer-encoding") && !value.is_empty())
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "Chunked requests are not supported.",
        ));
    }
    let body_length = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
        .map(|(_, value)| value.parse::<usize>())
        .transpose()
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?
        .unwrap_or(0);
    if body_length > MAX_HTTP_BODY_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "HTTP request body exceeds the size limit.",
        ));
    }
    let mut body = request_bytes[header_end..].to_vec();
    if body.len() > body_length {
        body.truncate(body_length);
    }
    while body.len() < body_length {
        let remaining = body_length - body.len();
        let mut next = vec![0; remaining.min(chunk.len())];
        let count = stream.read(&mut next)?;
        if count == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "HTTP request body ended early.",
            ));
        }
        body.extend_from_slice(&next[..count]);
    }
    Ok((method.to_uppercase(), target.to_string(), headers, body))
}

fn serve_browser_request(mut stream: TcpStream, app: AppHandle, assets: PathBuf, port: u16) {
    let result = (|| -> Result<(), String> {
        let (method, target, headers, body) = read_http_request(&mut stream)
            .map_err(|error| format!("Could not read browser request: {error}"))?;
        if !browser_request_allowed(&headers, port) {
            http_response(&mut stream, 403, "text/plain; charset=utf-8", b"Forbidden")
                .map_err(|error| error.to_string())?;
            return Ok(());
        }
        if target.starts_with("/api/") {
            if method != "GET" && method != "POST" && method != "DELETE" {
                http_response(
                    &mut stream,
                    405,
                    "application/json; charset=utf-8",
                    br#"{"error":"Method not allowed."}"#,
                )
                .map_err(|error| error.to_string())?;
                return Ok(());
            }
            if method != "GET" {
                let origin_present = headers
                    .iter()
                    .any(|(name, _)| name.eq_ignore_ascii_case("origin"));
                let json_content_type = headers
                    .iter()
                    .find(|(name, _)| name.eq_ignore_ascii_case("content-type"))
                    .is_some_and(|(_, value)| {
                        value.split(';').next().is_some_and(|mime| {
                            mime.trim().eq_ignore_ascii_case("application/json")
                        })
                    });
                if !origin_present || (!body.is_empty() && !json_content_type) {
                    http_response(
                        &mut stream,
                        403,
                        "application/json; charset=utf-8",
                        br#"{"error":"Browser API requests must come from the IDLR page."}"#,
                    )
                    .map_err(|error| error.to_string())?;
                    return Ok(());
                }
            }
            let request_body = if body.is_empty() {
                Value::Null
            } else {
                match serde_json::from_slice::<Value>(&body) {
                    Ok(value) => value,
                    Err(error) => {
                        let response = json!({"error": format!("Invalid JSON request: {error}")});
                        http_response(
                            &mut stream,
                            400,
                            "application/json; charset=utf-8",
                            response.to_string().as_bytes(),
                        )
                        .map_err(|error| error.to_string())?;
                        return Ok(());
                    }
                }
            };
            let response = if request_path(&target) == "/api/app-update" {
                tauri::async_runtime::block_on(app_update_api(&app, &method, request_body))
                    .unwrap_or_else(|error| error_reply(500, error))
            } else {
                api_request_for_state(&app.state::<AppState>(), &method, &target, request_body)
                    .unwrap_or_else(|error| error_reply(500, error))
            };
            http_response(
                &mut stream,
                response.status,
                "application/json; charset=utf-8",
                response.body.to_string().as_bytes(),
            )
            .map_err(|error| error.to_string())?;
            return Ok(());
        }
        if method != "GET" && method != "HEAD" {
            http_response(
                &mut stream,
                405,
                "text/plain; charset=utf-8",
                b"Method not allowed",
            )
            .map_err(|error| error.to_string())?;
            return Ok(());
        }
        let asset_name = match request_path(&target) {
            "/" | "/index.html" => "index.html",
            "/favicon.svg" => "favicon.svg",
            _ => {
                http_response(&mut stream, 404, "text/plain; charset=utf-8", b"Not found")
                    .map_err(|error| error.to_string())?;
                return Ok(());
            }
        };
        let content_type = if asset_name.ends_with(".svg") {
            "image/svg+xml"
        } else {
            "text/html; charset=utf-8"
        };
        let asset = fs::read(assets.join(asset_name))
            .map_err(|error| format!("Could not read browser asset {asset_name}: {error}"))?;
        http_response(
            &mut stream,
            200,
            content_type,
            if method == "HEAD" { &[] } else { &asset },
        )
        .map_err(|error| error.to_string())?;
        Ok(())
    })();
    if let Err(error) = result {
        log::warn!("Browser bridge request failed: {error}");
        let _ = http_response(
            &mut stream,
            400,
            "text/plain; charset=utf-8",
            b"Invalid browser request",
        );
    }
}

fn browser_server(app: AppHandle, listener: TcpListener, assets: PathBuf, port: u16) {
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let app = app.clone();
                let assets = assets.clone();
                if let Err(error) = thread::Builder::new()
                    .name("idlr-browser-request".into())
                    .spawn(move || serve_browser_request(stream, app, assets, port))
                {
                    log::error!("Could not process browser request: {error}");
                }
            }
            Err(error) => log::error!("IDLR browser bridge listener failed: {error}"),
        }
    }
}

fn open_browser(url: &str) -> std::io::Result<()> {
    #[cfg(target_os = "windows")]
    {
        Command::new("cmd")
            .args(["/C", "start", "", url])
            .spawn()
            .map(|_| ())
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg(url).spawn().map(|_| ())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open").arg(url).spawn().map(|_| ())
    }
}

fn image_digest(value: &str) -> Option<String> {
    let (mime, encoded) = value.split_once(";base64,")?;
    let mime = mime.strip_prefix("data:")?;
    if !matches!(mime, "image/png" | "image/jpeg" | "image/webp") || encoded.len() > 350_000 {
        return None;
    }
    let bytes = STANDARD.decode(encoded).ok()?;
    let valid = match mime {
        "image/png" => bytes.starts_with(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg" => bytes.starts_with(b"\xff\xd8\xff"),
        "image/webp" => bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP"),
        _ => false,
    };
    if !valid || bytes.is_empty() || bytes.len() > 250 * 1024 {
        return None;
    }
    let digest = Sha256::digest(value.as_bytes());
    Some(format!("{digest:x}"))
}

fn expand_save_path(raw: &str) -> PathBuf {
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    let mut path = raw.trim().trim_matches('"').trim_matches('\'').to_string();
    path = path
        .replace("%USERPROFILE%", &home)
        .replace("%HOME%", &home);
    if path == "~" {
        path = home.clone();
    } else if let Some(rest) = path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\")) {
        path = PathBuf::from(home)
            .join(rest)
            .to_string_lossy()
            .into_owned();
    }
    PathBuf::from(path)
}

fn validate_save_path(raw: &str) -> ApiResponse {
    if raw.trim().is_empty() {
        return error_reply(400, "Enter a folder path.");
    }
    let path = expand_save_path(raw);
    if let Err(error) = fs::create_dir_all(&path) {
        return error_reply(
            400,
            format!("Cannot create save folder '{}': {error}", path.display()),
        );
    }
    let mut probe = None;
    for attempt in 0..8 {
        let candidate = path.join(format!(
            ".idlr_write_test_{}_{}",
            std::process::id(),
            attempt
        ));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&candidate)
        {
            Ok(mut file) => {
                if let Err(error) = file.write_all(b"ok") {
                    let _ = fs::remove_file(&candidate);
                    return error_reply(400, format!("Save folder is not writable: {error}"));
                }
                probe = Some(candidate);
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return error_reply(400, format!("Save folder is not writable: {error}")),
        }
    }
    let Some(probe) = probe else {
        return error_reply(400, "Could not verify that the save folder is writable.");
    };
    if let Err(error) = fs::remove_file(probe) {
        return error_reply(
            400,
            format!("Could not clean up the save-folder check: {error}"),
        );
    }
    api_reply(200, json!({"path": path.to_string_lossy()}))
}

fn account_request(
    state: &AppState,
    method: &str,
    url: &str,
    path: &str,
    body: &Value,
) -> Result<Option<ApiResponse>, String> {
    let account_route = matches!(
        path,
        "/api/register"
            | "/api/login"
            | "/api/google-login"
            | "/api/accounts"
            | "/api/profile"
            | "/api/profile/images"
            | "/api/prefs"
            | "/api/delete-account"
    );
    if !account_route {
        return Ok(None);
    }
    let mut accounts = read_accounts(state)?;
    let email = get_string(body, "email").trim().to_lowercase();
    let reply = match (method, path) {
        ("POST", "/api/register") => {
            let password = get_string(body, "password");
            let name = get_string(body, "name").trim();
            if !email.contains('@') {
                error_reply(400, "Invalid email")
            } else if password.chars().count() < 6 {
                error_reply(400, "Password must be 6+ characters")
            } else if name.is_empty() {
                error_reply(400, "Name required")
            } else if accounts.contains_key(&email) {
                error_reply(409, "Account already exists")
            } else {
                let account = json!({
                    "name": name,
                    "pw_hash": format!("{:x}", Sha256::digest(password.as_bytes())),
                    "joined": chrono::Local::now().format("%Y-%m-%d").to_string(),
                    "method": "email",
                    "prefs": {},
                    "username": "",
                    "bio": "",
                    "avatar": "",
                    "banner": "",
                    "image_history": []
                });
                accounts.insert(email.clone(), account.clone());
                save_accounts(state, &accounts)?;
                user_response(&email, &account)
            }
        }
        ("POST", "/api/login") => {
            let password_hash = format!(
                "{:x}",
                Sha256::digest(get_string(body, "password").as_bytes())
            );
            match accounts.get(&email) {
                None => error_reply(404, "No account with this email"),
                Some(account) if get_string(account, "pw_hash") != password_hash => {
                    error_reply(401, "Wrong password")
                }
                Some(account) => user_response(&email, account),
            }
        }
        ("POST", "/api/google-login") => {
            if !email.contains('@') {
                error_reply(400, "Invalid Gmail")
            } else {
                if !accounts.contains_key(&email) {
                    let name = email
                        .split('@')
                        .next()
                        .unwrap_or_default()
                        .replace('.', " ")
                        .split_whitespace()
                        .map(|part| {
                            let mut chars = part.chars();
                            chars
                                .next()
                                .map(|first| {
                                    first.to_uppercase().collect::<String>() + chars.as_str()
                                })
                                .unwrap_or_default()
                        })
                        .collect::<Vec<_>>()
                        .join(" ");
                    accounts.insert(
                        email.clone(),
                        json!({
                            "name": name,
                            "pw_hash": "",
                            "joined": chrono::Local::now().format("%Y-%m-%d").to_string(),
                            "method": "google",
                            "prefs": {},
                            "username": "",
                            "bio": "",
                            "avatar": "",
                            "banner": "",
                            "image_history": []
                        }),
                    );
                    save_accounts(state, &accounts)?;
                }
                user_response(&email, &accounts[&email])
            }
        }
        ("GET", "/api/accounts") => {
            let list = accounts
                .iter()
                .map(|(email, account)| {
                    json!({
                        "email": email,
                        "name": get_string(account, "name"),
                        "method": account.get("method").and_then(Value::as_str).unwrap_or("email"),
                        "avatar": account.get("avatar").and_then(Value::as_str).unwrap_or("")
                    })
                })
                .collect::<Vec<_>>();
            api_reply(200, json!(list))
        }
        ("POST", "/api/profile") => {
            let email = get_string(body, "user").trim().to_lowercase();
            match accounts.get_mut(&email) {
                None => error_reply(404, "User not found"),
                Some(account) => {
                    for field in ["name", "username", "bio"] {
                        if let Some(value) = body.get(field) {
                            if !value.is_string() {
                                return Ok(Some(error_reply(400, "Profile fields must be text.")));
                            }
                            let value = value.as_str().unwrap_or_default().trim();
                            let limit = match field {
                                "name" => 40,
                                "username" => 24,
                                _ => 160,
                            };
                            let value: String = value.chars().take(limit).collect();
                            if field != "name" || !value.is_empty() {
                                account[field] = json!(value);
                            }
                        }
                    }
                    for field in ["avatar", "banner"] {
                        if let Some(value) = body.get(field) {
                            let Some(value) = value.as_str() else {
                                return Ok(Some(error_reply(
                                    400,
                                    "Images must be PNG, JPEG, or WebP.",
                                )));
                            };
                            if !value.is_empty() && image_digest(value).is_none() {
                                return Ok(Some(error_reply(
                                    400,
                                    "Choose a valid PNG, JPEG, or WebP image smaller than 250 KB.",
                                )));
                            }
                            account[field] = json!(value);
                        }
                    }
                    if let Some(images) = body.get("image_history") {
                        let Some(images) = images.as_array() else {
                            return Ok(Some(error_reply(400, "Invalid image history entry.")));
                        };
                        if images.len() > 12 {
                            return Ok(Some(error_reply(
                                400,
                                "Image history can contain up to 12 images.",
                            )));
                        }
                        let mut seen = std::collections::HashSet::new();
                        let mut clean = Vec::new();
                        for image in images {
                            let data = get_string(image, "data");
                            let Some(digest) = image_digest(data) else {
                                return Ok(Some(error_reply(400, "Invalid image history entry.")));
                            };
                            if seen.insert(digest.clone()) {
                                clean.push(json!({
                                    "id": &digest[..16],
                                    "data": data,
                                    "added": image.get("added").and_then(Value::as_str).filter(|s| !s.is_empty())
                                        .map(str::to_string)
                                        .unwrap_or_else(|| chrono::Local::now().format("%Y-%m-%d %H:%M").to_string())
                                }));
                            }
                        }
                        account["image_history"] = json!(clean);
                    }
                    let account = account.clone();
                    save_accounts(state, &accounts)?;
                    user_response(&email, &account)
                }
            }
        }
        ("POST", "/api/prefs") => {
            let email = get_string(body, "user").trim().to_lowercase();
            match accounts.get_mut(&email) {
                None => error_reply(404, "User not found"),
                Some(account) => {
                    account["prefs"] = body.get("prefs").cloned().unwrap_or_else(|| json!({}));
                    save_accounts(state, &accounts)?;
                    api_reply(200, json!({"ok": true}))
                }
            }
        }
        ("GET", "/api/profile/images") => {
            let email = query_value(url, "user");
            match accounts.get(&email) {
                Some(account) => api_reply(
                    200,
                    json!({"images": account.get("image_history").cloned().unwrap_or_else(|| json!([]))}),
                ),
                None => error_reply(404, "User not found"),
            }
        }
        ("POST", "/api/delete-account") => {
            let email = get_string(body, "user").trim().to_lowercase();
            if email.is_empty() {
                error_reply(400, "Missing user")
            } else if accounts.remove(&email).is_none() {
                error_reply(404, "Account not found")
            } else {
                save_accounts(state, &accounts)?;
                let history = read_json(&history_path(state), json!([]))?;
                let history = history
                    .as_array()
                    .ok_or("IDLR history data is not a valid list.")?
                    .iter()
                    .filter(|item| get_string(item, "user") != email)
                    .cloned()
                    .collect::<Vec<_>>();
                write_json(&history_path(state), &json!(history))?;
                api_reply(200, json!({"ok": true}))
            }
        }
        _ => return Ok(None),
    };
    Ok(Some(reply))
}

fn history_request(
    state: &AppState,
    method: &str,
    url: &str,
    route: &str,
    body: &Value,
) -> Result<Option<ApiResponse>, String> {
    if !route.starts_with("/api/history") {
        return Ok(None);
    }
    let mut history = read_json(&history_path(state), json!([]))?
        .as_array()
        .cloned()
        .ok_or("IDLR history data is not a valid list.")?;
    let user = if method == "GET" || route.ends_with("/clear") || route.ends_with("/empty-trash") {
        query_value(url, "user")
    } else {
        get_string(body, "user").to_string()
    };
    let url_value = get_string(body, "url");
    let reply = match (method, route) {
        ("GET", "/api/history") => {
            let items = history
                .iter()
                .filter(|item| get_string(item, "user") == user)
                .rev()
                .take(120)
                .cloned()
                .collect::<Vec<_>>();
            api_reply(200, json!(items.into_iter().rev().collect::<Vec<_>>()))
        }
        ("DELETE", "/api/history/clear") => {
            for entry in &mut history {
                if get_string(entry, "user") == user {
                    entry["trashed"] = json!(true);
                }
            }
            write_json(&history_path(state), &json!(history))?;
            api_reply(200, json!({"ok": true}))
        }
        ("DELETE", "/api/history/remove") => {
            for entry in &mut history {
                if get_string(entry, "user") == user && get_string(entry, "url") == url_value {
                    entry["trashed"] = json!(true);
                }
            }
            write_json(&history_path(state), &json!(history))?;
            api_reply(200, json!({"ok": true}))
        }
        ("POST", "/api/history/delete") => {
            history.retain(|entry| {
                get_string(entry, "user") != user || get_string(entry, "url") != url_value
            });
            write_json(&history_path(state), &json!(history))?;
            api_reply(200, json!({"ok": true}))
        }
        ("POST", "/api/history/restore") => {
            for entry in &mut history {
                if get_string(entry, "user") == user && get_string(entry, "url") == url_value {
                    entry["trashed"] = json!(false);
                }
            }
            write_json(&history_path(state), &json!(history))?;
            api_reply(200, json!({"ok": true}))
        }
        ("DELETE", "/api/history/empty-trash") => {
            history.retain(|entry| {
                get_string(entry, "user") != user
                    || !entry
                        .get("trashed")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
            });
            write_json(&history_path(state), &json!(history))?;
            api_reply(200, json!({"ok": true}))
        }
        ("POST", "/api/history/star") => {
            let mut starred = false;
            for entry in &mut history {
                if get_string(entry, "user") == user && get_string(entry, "url") == url_value {
                    starred = !entry
                        .get("starred")
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                    entry["starred"] = json!(starred);
                }
            }
            write_json(&history_path(state), &json!(history))?;
            api_reply(200, json!({"ok": true, "starred": starred}))
        }
        ("POST", "/api/history/rename") => {
            let title = body
                .get("newTitle")
                .or_else(|| body.get("new_title"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim()
                .chars()
                .take(120)
                .collect::<String>();
            if user.is_empty() || url_value.is_empty() {
                error_reply(400, "Missing user or url")
            } else if title.is_empty() {
                error_reply(400, "Title cannot be empty")
            } else if let Some(entry) = history.iter_mut().find(|entry| {
                get_string(entry, "user") == user && get_string(entry, "url") == url_value
            }) {
                entry["title"] = json!(title);
                write_json(&history_path(state), &json!(history))?;
                api_reply(200, json!({"ok": true}))
            } else {
                error_reply(404, "Entry not found")
            }
        }
        _ => return Ok(None),
    };
    Ok(Some(reply))
}

#[tauri::command]
fn api_request(
    state: State<'_, AppState>,
    method: String,
    url: String,
    body: Option<Value>,
) -> Result<ApiResponse, String> {
    api_request_for_state(&state, &method, &url, body.unwrap_or(Value::Null))
}

fn api_request_for_state(
    state: &AppState,
    method: &str,
    url: &str,
    body: Value,
) -> Result<ApiResponse, String> {
    let method = method.to_uppercase();
    let route = request_path(url);

    if method == "POST" && route == "/api/validate-save-path" {
        return Ok(validate_save_path(get_string(&body, "path")));
    }
    if let Some(response) = account_request(state, &method, url, route, &body)? {
        return Ok(response);
    }
    if let Some(response) = history_request(state, &method, url, route, &body)? {
        return Ok(response);
    }

    let worker_route = route == "/api/download"
        || route == "/api/preview"
        || route == "/api/thumbnail"
        || route == "/api/info"
        || route == "/api/update-ytdlp"
        || route.starts_with("/api/progress-state/");
    if !worker_route {
        return Ok(error_reply(404, "Unknown IDLR command."));
    }
    let mut worker = state
        .worker
        .lock()
        .map_err(|_| "IDLR's download worker lock was poisoned.".to_string())?;
    let mut response = worker.request(&method, &url, Some(body))?;
    if route == "/api/info" && response.status == 200 {
        response.body["version"] = json!(APP_VERSION);
        response.body["network"] = state
            .browser_url
            .lock()
            .map_err(|_| "IDLR's browser URL lock was poisoned.".to_string())?
            .as_ref()
            .map(|url| json!(url))
            .unwrap_or(Value::Null);
        response.body["frozen"] = json!(true);
    }
    Ok(response)
}

#[tauri::command]
fn select_folder(current_path: String) -> Option<String> {
    let mut dialog = rfd::FileDialog::new();
    let current = expand_save_path(&current_path);
    if current.is_dir() {
        dialog = dialog.set_directory(current);
    }
    dialog
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

async fn check_app_update_for_app(app: &AppHandle) -> Result<AppUpdateStatus, String> {
    let current_version = app.package_info().version.to_string();
    let update = app
        .updater()
        .map_err(|error| format!("Could not configure the IDLR updater: {error}"))?
        .check()
        .await
        .map_err(|error| format!("Could not check for IDLR updates: {error}"))?;

    Ok(match update {
        Some(update) => AppUpdateStatus {
            available: true,
            current_version,
            version: Some(update.version),
            notes: update.body,
        },
        None => AppUpdateStatus {
            available: false,
            current_version,
            version: None,
            notes: None,
        },
    })
}

async fn install_app_update_for_app(app: &AppHandle) -> Result<String, String> {
    let update = app
        .updater()
        .map_err(|error| format!("Could not configure the IDLR updater: {error}"))?
        .check()
        .await
        .map_err(|error| format!("Could not check for IDLR updates: {error}"))?
        .ok_or_else(|| "IDLR is already up to date.".to_string())?;
    let version = update.version.clone();

    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| format!("Could not install IDLR {version}: {error}"))?;

    #[cfg(not(target_os = "windows"))]
    {
        let executable =
            std::env::current_exe().map_err(|error| format!("Could not locate IDLR: {error}"))?;
        let app = app.clone();
        thread::Builder::new()
            .name("idlr-update-restart".into())
            .spawn(move || {
                thread::sleep(Duration::from_secs(2));
                if let Err(error) = Command::new(executable).spawn() {
                    log::error!("IDLR was updated but could not restart automatically: {error}");
                    return;
                }
                app.exit(0);
            })
            .map_err(|error| {
                format!("IDLR was updated but could not restart automatically: {error}")
            })?;
    }

    Ok(version)
}

async fn app_update_api(app: &AppHandle, method: &str, body: Value) -> Result<ApiResponse, String> {
    if method != "POST" {
        return Ok(error_reply(405, "App updates require a POST request."));
    }

    match get_string(&body, "action") {
        "check" => {
            let status = check_app_update_for_app(app).await?;
            Ok(api_reply(200, json!(status)))
        }
        "install" => {
            let version = install_app_update_for_app(app).await?;
            Ok(api_reply(200, json!({"ok": true, "version": version})))
        }
        _ => Ok(error_reply(
            400,
            "Choose either the check or install update action.",
        )),
    }
}

#[tauri::command]
async fn check_app_update(app: AppHandle) -> Result<AppUpdateStatus, String> {
    check_app_update_for_app(&app).await
}

#[tauri::command]
async fn install_app_update(app: AppHandle) -> Result<String, String> {
    install_app_update_for_app(&app).await
}

pub fn run() {
    tauri::Builder::default()
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let data_dir = app_data_dir().map_err(std::io::Error::other)?;
            let worker = Worker::start(app).map_err(std::io::Error::other)?;
            let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(std::io::Error::other)?;
            let port = listener.local_addr().map_err(std::io::Error::other)?.port();
            let browser_url = format!("http://127.0.0.1:{port}");
            let assets = browser_assets_dir(&app.handle()).map_err(std::io::Error::other)?;
            if !assets.join("index.html").is_file() {
                return Err(std::io::Error::other(format!(
                    "IDLR's browser interface is missing: {}",
                    assets.display()
                ))
                .into());
            }
            app.manage(AppState {
                data_dir,
                worker: Mutex::new(worker),
                browser_url: Mutex::new(Some(browser_url.clone())),
            });
            let app_handle = app.handle().clone();
            thread::Builder::new()
                .name("idlr-browser-server".into())
                .spawn(move || browser_server(app_handle, listener, assets, port))
                .map_err(std::io::Error::other)?;
            if let Err(error) = open_browser(&browser_url) {
                log::error!("Could not open IDLR in the system browser at {browser_url}: {error}");
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            api_request,
            select_folder,
            check_app_update,
            install_app_update
        ])
        .run(tauri::generate_context!())
        .expect("error while building tauri application");
}

#[cfg(test)]
mod tests {
    use super::{
        TEMP_FILE_SEQUENCE, browser_request_allowed, read_http_request, read_json, write_json,
    };
    use serde_json::json;
    use std::{
        fs,
        io::Write,
        net::{Shutdown, TcpListener, TcpStream},
        path::PathBuf,
        sync::atomic::Ordering,
        thread,
    };

    #[test]
    fn write_json_replaces_existing_data_without_leaving_temp_files() {
        let test_dir = PathBuf::from(std::env::temp_dir()).join(format!(
            "idlr-json-write-{}-{}",
            std::process::id(),
            TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&test_dir).expect("create test directory");
        let path = test_dir.join("accounts.json");

        write_json(&path, &json!({"account": "old"})).expect("write initial JSON");
        write_json(&path, &json!({"account": "new"})).expect("replace JSON");
        let actual = read_json(&path, json!(null)).expect("read replaced JSON");
        assert_eq!(actual, json!({"account": "new"}));
        assert_eq!(
            fs::read_dir(&test_dir)
                .expect("list test directory")
                .count(),
            1
        );

        fs::remove_dir_all(test_dir).expect("remove test directory");
    }

    #[test]
    fn browser_bridge_accepts_only_its_local_origin() {
        let allowed = vec![
            ("Host".into(), "127.0.0.1:43210".into()),
            ("Origin".into(), "http://127.0.0.1:43210".into()),
            ("Sec-Fetch-Site".into(), "same-origin".into()),
        ];
        assert!(browser_request_allowed(&allowed, 43210));

        let cross_site = vec![
            ("Host".into(), "127.0.0.1:43210".into()),
            ("Origin".into(), "https://example.com".into()),
            ("Sec-Fetch-Site".into(), "cross-site".into()),
        ];
        assert!(!browser_request_allowed(&cross_site, 43210));

        let invalid_host = vec![("Host".into(), "example.com".into())];
        assert!(!browser_request_allowed(&invalid_host, 43210));
    }

    #[test]
    fn browser_bridge_parses_json_requests() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind test listener");
        let address = listener.local_addr().expect("get test address");
        let client = thread::spawn(move || {
            let mut stream = TcpStream::connect(address).expect("connect test client");
            stream
                .write_all(
                    b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 15\r\n\r\n{\"ok\":true}    ",
                )
                .expect("write test request");
            stream.shutdown(Shutdown::Write).expect("finish request");
        });
        let (mut stream, _) = listener.accept().expect("accept test request");
        let request = read_http_request(&mut stream).expect("parse request");
        client.join().expect("finish test client");

        assert_eq!(request.0, "POST");
        assert_eq!(request.1, "/api/login");
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&request.3).expect("parse JSON body"),
            json!({"ok": true})
        );
    }
}
