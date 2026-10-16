//! ffbe-server-app — launcher + dependency doctor for the FFBE synthetic server.
//!
//! WHY THIS SHAPE
//!   The engine is Python and stays the one canonical artifact ('../server/'). This app is a shell:
//!   it never contains protocol logic and never ships game content. It checks that the runtime data
//!   exists, points at the LOCAL tool that produces each missing piece (deps.json), spawns the
//!   engine with --data DIR, and surfaces the engine's own log plus the Android-side steps.
//!
//! ENGINE CONTRACT (see ../server/capture_server.py)
//!   --data DIR --print-paths     resolved layout as key=value lines, then exit
//!   --campaign | --maxroster     the two save modes
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};

const APP_TITLE: &str = "FFBE Server";

// ---------------------------------------------------------------- config

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
enum Mode {
    Campaign,
    MaxRoster,
}

impl Mode {
    fn flag(self) -> &'static str {
        match self {
            Mode::Campaign => "--campaign",
            Mode::MaxRoster => "--maxroster",
        }
    }
    fn label(self) -> &'static str {
        match self {
            Mode::Campaign => "Campaign",
            Mode::MaxRoster => "Max roster",
        }
    }
    fn blurb(self) -> &'static str {
        match self {
            Mode::Campaign => "Normal save: continues the campaign slot if it exists, else a fresh account.",
            Mode::MaxRoster => "Sandbox: every named unit at its top rarity and cap, Brave-Shift EX served.",
        }
    }
}

#[derive(Serialize, Deserialize, Clone)]
struct Config {
    data_root: String,
    engine_dir: String,
    port: u16,
    mode: Mode,
    /// Where to fetch {"version","url","notes"} for the update check; empty disables it.
    #[serde(default)]
    update_url: String,
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
}

fn config_file() -> Option<PathBuf> {
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA").map(PathBuf::from);
    #[cfg(not(windows))]
    let base = home_dir().map(|h| h.join(".config"));
    base.map(|b| b.join("ffbe-server-app").join("config.json"))
}

fn default_engine_dir() -> PathBuf {
    if let Some(p) = std::env::var_os("FFBE_SERVER_ENGINE") {
        return PathBuf::from(p);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for c in [
                dir.join("engine").join(engine_bin_name()),
                dir.join("engine"),
                dir.join("../server"),
                dir.join("../../../server"),
                dir.join("../../../APK_emulator_work/server"),
            ] {
                if is_engine(&c) {
                    return c;
                }
            }
        }
    }
    PathBuf::from("server")
}

fn default_data_root() -> PathBuf {
    if let Some(p) = std::env::var_os("FFBE_DATA") {
        return PathBuf::from(p);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for c in [
                dir.join("data"),
                dir.join("../../APK_emulator_work"),
                dir.join("../../../APK_emulator_work"),
            ] {
                if c.join("mst_decoded").is_dir() {
                    return c;
                }
            }
        }
    }
    PathBuf::from("data")
}

fn load_config() -> Config {
    let mut cfg = Config {
        data_root: default_data_root().to_string_lossy().into_owned(),
        engine_dir: default_engine_dir().to_string_lossy().into_owned(),
        port: 443,
        mode: Mode::Campaign,
        update_url: String::new(),
    };
    if let Some(p) = config_file() {
        if let Ok(s) = std::fs::read_to_string(&p) {
            if let Ok(saved) = serde_json::from_str::<Config>(&s) {
                cfg = saved;
            }
        }
    }
    if let Ok(u) = std::env::var("FFBE_UPDATE_URL") {
        if !u.trim().is_empty() {
            cfg.update_url = u;
        }
    }
    cfg
}

fn save_config(cfg: &Config) {
    if let Some(p) = config_file() {
        if let Some(parent) = p.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(s) = serde_json::to_string_pretty(cfg) {
            let _ = std::fs::write(p, s);
        }
    }
}

fn python_cmd() -> String {
    if let Ok(p) = std::env::var("FFBE_PYTHON") {
        return p;
    }
    if cfg!(windows) {
        "python".to_string()
    } else {
        "python3".to_string()
    }
}

// ---------------------------------------------------------------- checks

#[derive(Clone, Copy, PartialEq, Eq)]
enum Status {
    Ok,
    Warn,
    Missing,
}

impl Status {
    fn color(self) -> egui::Color32 {
        match self {
            Status::Ok => egui::Color32::from_rgb(64, 170, 90),
            Status::Warn => egui::Color32::from_rgb(215, 160, 60),
            Status::Missing => egui::Color32::from_rgb(210, 80, 80),
        }
    }
    fn text(self) -> &'static str {
        match self {
            Status::Ok => "OK",
            Status::Warn => "OPTIONAL",
            Status::Missing => "MISSING",
        }
    }
}

#[derive(Clone)]
struct Check {
    label: String,
    status: Status,
    detail: String,
    fix: String,
}

#[derive(Deserialize)]
struct DepFile {
    entries: Vec<DepEntry>,
}

#[derive(Deserialize)]
struct DepEntry {
    label: String,
    paths: Vec<String>,
    #[serde(default)]
    min_files: usize,
    #[serde(default)]
    required: bool,
    producer: String,
    doc: String,
}

/// Count files in a dir, stopping at limit (a check must not walk a huge tree).
fn count_files(dir: &Path, limit: usize) -> usize {
    let mut n = 0usize;
    if let Ok(rd) = std::fs::read_dir(dir) {
        for e in rd.flatten() {
            if e.path().is_file() {
                n += 1;
                if n >= limit {
                    break;
                }
            }
        }
    }
    n
}

fn command_output(program: &str, args: &[&str]) -> Result<String, String> {
    match Command::new(program).args(args).output() {
        Ok(o) => {
            let mut s = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if s.is_empty() {
                s = String::from_utf8_lossy(&o.stderr).trim().to_string();
            }
            if o.status.success() {
                Ok(s)
            } else {
                Err(format!("exit {}: {}", o.status, s))
            }
        }
        Err(e) => Err(e.to_string()),
    }
}

fn run_checks(cfg: &Config, engine_info: &EngineInfo) -> Vec<Check> {
    let mut out: Vec<Check> = Vec::new();
    let engine = Path::new(&cfg.engine_dir);
    let py = python_cmd();

    match command_output(&py, &["--version"]) {
        Ok(v) => out.push(Check { label: "Python interpreter".into(), status: Status::Ok, detail: v, fix: String::new() }),
        Err(e) => out.push(Check {
            label: "Python interpreter".into(),
            status: Status::Missing,
            detail: format!("python executable \"{py}\" is not runnable ({e})"),
            fix: "Install Python 3.10+ and make it available on PATH (or set FFBE_PYTHON).".into(),
        }),
    }

    match command_output(&py, &["-c", "import yaml"]) {
        Ok(_) => out.push(Check { label: "PyYAML".into(), status: Status::Ok, detail: "import yaml ok".into(), fix: String::new() }),
        Err(_) => out.push(Check {
            label: "PyYAML".into(),
            status: Status::Missing,
            detail: "the server imports yaml (friends.yaml)".into(),
            fix: format!("{py} -m pip install pyyaml"),
        }),
    }

    if is_engine(engine) {
        let what = if engine.is_dir() {
            let cs = engine.join("capture_server.py");
            if cs.is_file() {
                cs.display().to_string()
            } else {
                format!("{} (frozen engine)", engine.display())
            }
        } else {
            engine.display().to_string()
        };
        out.push(Check { label: "Server engine".into(), status: Status::Ok, detail: what, fix: String::new() });
    } else {
        out.push(Check {
            label: "Server engine".into(),
            status: Status::Missing,
            detail: format!("{} is not an engine", engine.display()),
            fix: "Point the engine directory at APK_emulator_work/server in a repo checkout, or at the bundle's engine/ directory.".into(),
        });
    }

    match engine_info.endpoints.clone() {
        Some(ep) if Path::new(&ep).is_file() => out.push(Check {
            label: "Dispatch table".into(),
            status: Status::Ok,
            detail: ep,
            fix: String::new(),
        }),
        Some(ep) => out.push(Check {
            label: "Dispatch table".into(),
            status: Status::Missing,
            detail: format!("{ep} not found"),
            fix: "python3 harness/build_endpoints.py (writes out/endpoints.json; a bundle ships it in the engine)".into(),
        }),
        // No probe result: say nothing rather than guess a path that is wrong in a bundle.
        None => {}
    }

    // The wire AES backend. The release bundle ships `cryptography`; a checkout without it falls back
    // to the `openssl` CLI, which must then exist or not one request can be answered (2026-10-16).
    if let Some((_, backend)) = engine_info.paths.iter().find(|(k, _)| k == "crypto") {
        if backend == "cryptography" {
            out.push(Check { label: "Wire crypto (AES)".into(), status: Status::Ok,
                             detail: "bundled (cryptography)".into(), fix: String::new() });
        } else {
            let have = command_output("openssl", &["version"]).is_ok();
            out.push(Check {
                label: "Wire crypto (AES)".into(),
                status: if have { Status::Ok } else { Status::Missing },
                detail: if have { format!("{backend} backend; openssl present") }
                        else { format!("{backend} backend and openssl is not on PATH") },
                fix: "Install openssl, or use the release bundle (it ships the crypto library).".into(),
            });
        }
    }

    out.extend(check_data_root(Path::new(&cfg.data_root)));

    match std::net::TcpListener::bind(("0.0.0.0", cfg.port)) {
        Ok(l) => {
            drop(l);
            out.push(Check { label: format!("Port {}", cfg.port), status: Status::Ok, detail: "bind test ok".into(), fix: String::new() });
        }
        Err(e) => {
            let fix = if cfg!(windows) {
                "Close whatever holds the port, or change the port.".to_string()
            } else {
                format!("sudo sysctl -w net.ipv4.ip_unprivileged_port_start={}", cfg.port)
            };
            out.push(Check { label: format!("Port {}", cfg.port), status: Status::Missing, detail: e.to_string(), fix });
        }
    }

    if let Ok(v) = command_output("adb", &["version"]) {
        let first = v.lines().next().unwrap_or("adb").to_string();
        out.push(Check { label: "adb (Android side, optional)".into(), status: Status::Ok, detail: first, fix: String::new() });
    } else {
        out.push(Check {
            label: "adb (Android side, optional)".into(),
            status: Status::Warn,
            detail: "adb not on PATH".into(),
            fix: "Install platform-tools if you want help with the CA / hosts steps.".into(),
        });
    }

    out
}

/// The `deps.json` rows checked against a data root. One implementation, two callers -- the doctor's
/// Setup pane and `prepare`'s closing report -- so the helper and the GUI can never disagree about
/// whether a root is usable.
fn check_data_root(root: &Path) -> Vec<Check> {
    let mut out: Vec<Check> = Vec::new();
    let deps: DepFile = serde_json::from_str(include_str!("../deps.json")).unwrap_or(DepFile { entries: vec![] });
    for e in &deps.entries {
        let mut missing: Vec<String> = Vec::new();
        let mut ok_detail = String::new();
        for p in &e.paths {
            let full = root.join(p);
            if !full.exists() {
                missing.push(p.clone());
            } else if full.is_dir() && e.min_files > 0 {
                let n = count_files(&full, e.min_files);
                if n < e.min_files {
                    missing.push(format!("{p} ({n} files, need >= {})", e.min_files));
                } else {
                    ok_detail = format!("{p}: {n}+ files");
                }
            } else {
                ok_detail = p.clone();
            }
        }
        let status = if missing.is_empty() {
            Status::Ok
        } else if e.required {
            Status::Missing
        } else {
            Status::Warn
        };
        out.push(Check {
            label: e.label.clone(),
            status,
            detail: if missing.is_empty() { ok_detail } else { missing.join("; ") },
            fix: format!("produce with: {}   ({})", e.producer, e.doc),
        });
    }
    out
}

// ---------------------------------------------------------------- server process

struct Running {
    child: Child,
}

// ---------------------------------------------------------------- update check

#[derive(Clone)]
struct UpdateInfo {
    version: String,
    url: String,
    notes: String,
}

#[derive(Clone, Default)]
struct UpdateState {
    checking: bool,
    latest: Option<UpdateInfo>,
    error: String,
}

fn version_tuple(v: &str) -> (u32, u32, u32) {
    let cleaned = v.trim().trim_start_matches('v');
    let mut parts = cleaned.split('.');
    let mut next = || parts.next().and_then(|x| x.trim().parse::<u32>().ok()).unwrap_or(0);
    (next(), next(), next())
}

/// Fetch the update manifest in a background thread. curl, not a Rust HTTP client: this is a
/// once-per-launch check, curl ships on every target we run on, and the failure path is silence.
fn spawn_update_check(state: Arc<Mutex<UpdateState>>, url: String) {
    {
        let mut s = state.lock().unwrap();
        s.checking = true;
        s.error.clear();
    }
    std::thread::spawn(move || {
        #[derive(serde::Deserialize)]
        struct Wire {
            version: String,
            #[serde(default)]
            url: String,
            #[serde(default)]
            notes: String,
        }
        let out = Command::new("curl")
            // --proto blocks file:// and friends; -- stops a URL beginning with "-" being read as an option.
            .args(["-sS", "-L", "--max-time", "8", "--proto", "=http,https", "--", &url])
            .output();
        let mut res = UpdateState { checking: false, ..Default::default() };
        match out {
            Ok(o) if o.status.success() => match serde_json::from_slice::<Wire>(&o.stdout) {
                Ok(w) => {
                    res.latest = Some(UpdateInfo { version: w.version, url: w.url, notes: w.notes })
                }
                Err(e) => res.error = format!("bad update JSON: {e}"),
            },
            Ok(o) => res.error = format!("curl exited {}", o.status),
            Err(e) => {
                res.error = format!("could not run curl ({e})");
            }
        }
        *state.lock().unwrap() = res;
    });
}

fn engine_bin_name() -> &'static str {
    if cfg!(windows) {
        "ffbe-engine.exe"
    } else {
        "ffbe-engine"
    }
}

/// Does this path look like an engine we can run? Accepts: a checkout dir (capture_server.py), a
/// bundle dir containing the frozen ffbe-engine, a .py script, or the executable itself.
fn is_engine(p: &Path) -> bool {
    if p.is_file() {
        if p.extension().and_then(|e| e.to_str()) == Some("py") {
            return true;
        }
        return p.file_name().and_then(|n| n.to_str()).map(|n| n.starts_with("ffbe-engine")).unwrap_or(false);
    }
    p.is_dir() && (p.join("capture_server.py").is_file() || p.join(engine_bin_name()).is_file())
}

/// How to invoke the engine: the program to run plus any leading arguments (the script path for a
/// Python checkout; nothing for a frozen bundle binary).
fn engine_invocation(engine: &Path) -> (PathBuf, Vec<String>) {
    if engine.is_dir() {
        let cs = engine.join("capture_server.py");
        if cs.is_file() {
            return (PathBuf::from(python_cmd()), vec![cs.display().to_string()]);
        }
        let bin = engine.join(engine_bin_name());
        if bin.is_file() {
            return (bin, Vec::new());
        }
        return (PathBuf::from(python_cmd()), vec![cs.display().to_string()]);
    }
    if engine.extension().and_then(|e| e.to_str()) == Some("py") {
        return (PathBuf::from(python_cmd()), vec![engine.display().to_string()]);
    }
    (engine.to_path_buf(), Vec::new())
}

#[derive(Clone, Default)]
struct EngineInfo {
    available: bool,
    version: String,
    contract: Option<u32>,
    /// The dispatch table the engine will actually read (engine-shipped, not data-root data).
    endpoints: Option<String>,
    /// Every key=value line the probe printed, so the UI shows the resolved layout from the SAME spawn.
    paths: Vec<(String, String)>,
    error: String,
}

/// One cheap, side-effect-free call: resolves the layout AND reports the engine version + contract.
/// `data` is passed when known; an engine that predates `--data` rejects the probe, and the caller
/// retries bare (repo-layout mode).
fn probe_engine(engine: &Path, data: Option<&str>) -> EngineInfo {
    let (program, pre) = engine_invocation(engine);
    let mut cmd = Command::new(program);
    cmd.args(&pre);
    if let Some(d) = data {
        cmd.arg("--data").arg(d).env("FFBE_DATA", d);
    }
    let out = cmd.arg("--print-paths").stdin(Stdio::null()).output();
    match out {
        Ok(o) if o.status.success() => {
            let text = String::from_utf8_lossy(&o.stdout);
            let mut info = EngineInfo { available: true, ..Default::default() };
            for line in text.lines() {
                if let Some((k, v)) = line.split_once('=') {
                    let k = k.trim().to_string();
                    let v = v.trim().to_string();
                    match k.as_str() {
                        "engine_version" => info.version = v.clone(),
                        "contract" => info.contract = v.parse().ok(),
                        "endpoints_file" => info.endpoints = Some(v.clone()),
                        _ => {}
                    }
                    info.paths.push((k, v));
                }
            }
            info
        }
        Ok(o) => EngineInfo {
            available: false,
            error: format!("engine --print-paths exited {}", o.status),
            ..Default::default()
        },
        Err(e) => EngineInfo { available: false, error: e.to_string(), ..Default::default() },
    }
}

#[derive(Clone, Default)]
struct DoctorState {
    checking: bool,
    engine: EngineInfo,
    checks: Vec<Check>,
}

/// Run the probe + checks OFF the UI thread: every check spawns a process (python twice, adb once) and
/// binds the port, and a wedged interpreter on PATH must not stop the window from ever appearing.
fn spawn_doctor(state: Arc<Mutex<DoctorState>>, cfg: Config) {
    {
        let mut s = state.lock().unwrap();
        s.checking = true;
    }
    std::thread::spawn(move || {
        let data = if cfg.data_root.trim().is_empty() { None } else { Some(cfg.data_root.as_str()) };
        let mut engine = probe_engine(Path::new(&cfg.engine_dir), data);
        if !engine.available {
            // An engine that predates the contract rejects --data; if the bare probe works we are in
            // repo-layout mode and `contract` stays None (which is what the UI keys on).
            let bare = probe_engine(Path::new(&cfg.engine_dir), None);
            if bare.available {
                engine = bare;
            }
        }
        let checks = run_checks(&cfg, &engine);
        *state.lock().unwrap() = DoctorState { checking: false, engine, checks };
    });
}

/// The one place a SERVING argv is built, so the GUI and the CLI cannot drift. `FFBE_DATA` is always
/// exported (harmless for an engine that predates it); `--data` only when the contract is understood.
fn engine_serve(engine: &Path, data: &str, port: u16, bind: &str, mode: Mode, data_contract: bool) -> Command {
    let (program, pre) = engine_invocation(engine);
    let mut cmd = Command::new(program);
    cmd.args(&pre).env("FFBE_DATA", data);
    if data_contract {
        cmd.arg("--data").arg(data);
    }
    cmd.arg("--port").arg(port.to_string()).arg("--bind").arg(bind).arg(mode.flag());
    cmd
}

/// The query half of the contract: `--data` + `--print-paths`.
fn engine_query(engine: &Path, data: &str) -> Command {
    let (program, pre) = engine_invocation(engine);
    let mut cmd = Command::new(program);
    cmd.args(&pre)
        .arg("--data")
        .arg(data)
        .arg("--print-paths")
        .env("FFBE_DATA", data);
    cmd
}

/// Detach so an engine started by the CLI outlives the shell (setsid on unix; a new process group on
/// windows). The GUI deliberately does NOT detach: its child dies with it.
fn detach(cmd: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }
}

/// The one place a log line is appended and the deque trimmed: the GUI's own messages and both
/// reader threads go through it.
fn log_push(log: &Arc<Mutex<VecDeque<String>>>, line: String) {
    let mut l = log.lock().unwrap();
    l.push_back(line);
    while l.len() > 4000 {
        l.pop_front();
    }
}

fn spawn_reader<R: Read + Send + 'static>(r: R, log: Arc<Mutex<VecDeque<String>>>) {
    std::thread::spawn(move || {
        let br = BufReader::new(r);
        for line in br.lines().map_while(Result::ok) {
            log_push(&log, line);
        }
    });
}

// ---------------------------------------------------------------- app

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Setup,
    Server,
    Android,
}

struct App {
    tab: Tab,
    cfg: Config,
    /// Probe + checks, refreshed off-thread (see spawn_doctor) so the window paints immediately.
    doctor: Arc<Mutex<DoctorState>>,
    update: Arc<Mutex<UpdateState>>,
    log: Arc<Mutex<VecDeque<String>>>,
    server: Option<Running>,
    /// A server this GUI did not spawn, discovered through the same pidfile the CLI uses.
    external: Option<u32>,
    saved_note: String,
}

impl App {
    fn new() -> Self {
        let cfg = load_config();
        let doctor = Arc::new(Mutex::new(DoctorState { checking: true, ..Default::default() }));
        spawn_doctor(Arc::clone(&doctor), cfg.clone());
        let update = Arc::new(Mutex::new(UpdateState::default()));
        if !cfg.update_url.is_empty() {
            spawn_update_check(Arc::clone(&update), cfg.update_url.clone());
        }
        Self {
            tab: Tab::Setup,
            cfg,
            doctor,
            update,
            log: Arc::new(Mutex::new(VecDeque::new())),
            server: None,
            external: None,
            saved_note: String::new(),
        }
    }

    fn data_contract(&self) -> bool {
        let d = self.doctor.lock().unwrap();
        d.engine.available && d.engine.contract.is_some()
    }

    fn push_log(&mut self, s: String) {
        log_push(&self.log, s);
    }

    fn poll_server(&mut self) {
        if let Some(r) = self.server.as_mut() {
            match r.child.try_wait() {
                Ok(Some(st)) => {
                    let pid = r.child.id();
                    self.server = None;
                    self.push_log(format!("== server pid {pid} exited: {st}"));
                }
                Ok(None) => {}
                Err(e) => {
                    self.push_log(format!("!! wait failed: {e}"));
                }
            }
        }
        // The pidfile is the handshake: a server started with the CLI (or a previous GUI) is adopted here,
        // so the two front ends cannot disagree about what is running.
        if self.server.is_none() {
            let pidfile = Path::new(&self.cfg.data_root).join("state").join("server.pid");
            self.external = read_pid(&pidfile).filter(|pid| process_alive(*pid));
        } else {
            self.external = None;
        }
    }

    fn start_server(&mut self) {
        let contract = self.data_contract();
        let mut cmd = engine_serve(
            Path::new(&self.cfg.engine_dir),
            &self.cfg.data_root,
            self.cfg.port,
            "0.0.0.0",
            self.cfg.mode,
            contract,
        );
        cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        match cmd.spawn() {
            Ok(mut child) => {
                let pid = child.id();
                if let Some(o) = child.stdout.take() {
                    spawn_reader(o, Arc::clone(&self.log));
                }
                if let Some(e) = child.stderr.take() {
                    spawn_reader(e, Arc::clone(&self.log));
                }
                self.server = Some(Running { child });
                // The pidfile is the CLI/GUI handshake: a server started here is visible to
                // `ffbe-server-app status` and stoppable with `stop` (and vice versa).
                let state_dir = Path::new(&self.cfg.data_root).join("state");
                let _ = std::fs::create_dir_all(&state_dir);
                let _ = std::fs::write(state_dir.join("server.pid"), format!("{pid}\n"));
                self.push_log(format!(
                    "== started pid {pid}  mode={}  port={}  data={}",
                    self.cfg.mode.label(),
                    self.cfg.port,
                    self.cfg.data_root
                ));
            }
            Err(e) => self.push_log(format!("!! could not start the engine: {e}")),
        }
    }

    fn stop_server(&mut self) {
        let pidfile = Path::new(&self.cfg.data_root).join("state").join("server.pid");
        if let Some(mut r) = self.server.take() {
            terminate(r.child.id());
            let _ = r.child.wait();
            if read_pid(&pidfile) == Some(r.child.id()) {
                let _ = std::fs::remove_file(&pidfile);
            }
            self.push_log(format!("== stopped pid {}", r.child.id()));
        } else if let Some(pid) = self.external.take() {
            terminate(pid);
            if read_pid(&pidfile) == Some(pid) {
                let _ = std::fs::remove_file(&pidfile);
            }
            self.push_log(format!("== stopped external pid {pid}"));
        }
    }

    fn check_adb(&mut self) {
        match command_output("adb", &["devices", "-l"]) {
            Ok(v) => self.push_log(format!("== adb devices -l ({v})")),
            Err(e) => self.push_log(format!("!! adb: {e}")),
        }
    }

    // ------------------------------------------------------------ ui

    fn ui_setup(&mut self, ui: &mut egui::Ui) {
        ui.heading("Setup");
        ui.label("The engine is Python; this app only launches it and checks its inputs. No game content is bundled.");
        let (checking, engine, checks) = {
            let d = self.doctor.lock().unwrap();
            (d.checking, d.engine.clone(), d.checks.clone())
        };
        if engine.available && engine.contract.is_none() {
            ui.colored_label(
                egui::Color32::from_rgb(215, 160, 60),
                "This engine predates the data-root contract (--data / --print-paths); running in repo-layout mode.",
            );
        }
        ui.add_space(6.0);

        egui::Grid::new("cfg").num_columns(2).spacing([10.0, 6.0]).show(ui, |ui| {
            ui.label("Data directory");
            ui.add(egui::TextEdit::singleline(&mut self.cfg.data_root).desired_width(560.0));
            ui.end_row();
            ui.label("Engine directory");
            ui.add(egui::TextEdit::singleline(&mut self.cfg.engine_dir).desired_width(560.0));
            ui.end_row();
            ui.label("Port");
            ui.add(egui::DragValue::new(&mut self.cfg.port).range(1..=65535));
            ui.end_row();
        });

        ui.horizontal(|ui| {
            if ui.button("Re-check").clicked() {
                spawn_doctor(Arc::clone(&self.doctor), self.cfg.clone());
            }
            if ui.button("Save config").clicked() {
                save_config(&self.cfg);
                self.saved_note = format!("saved {}", config_file().map(|p| p.display().to_string()).unwrap_or_default());
            }
            if checking {
                ui.small("checking…");
            }
        });
        if !self.saved_note.is_empty() {
            ui.small(&self.saved_note);
        }

        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.label("Engine:");
            if engine.available {
                let ver = if engine.version.is_empty() { "?".to_string() } else { engine.version.clone() };
                let ct = engine.contract.map(|c| c.to_string()).unwrap_or_else(|| "?".to_string());
                ui.small(format!("version {ver}  ·  contract {ct}"));
            } else {
                ui.colored_label(egui::Color32::from_rgb(210, 80, 80), format!("not runnable: {}", engine.error));
            }
            ui.separator();
            ui.label("App:");
            ui.small(format!("v{}", env!("CARGO_PKG_VERSION")));
        });
        ui.horizontal(|ui| {
            ui.label("Updates:");
            let state = self.update.lock().unwrap().clone();
            if state.checking {
                ui.small("checking...");
            } else if let Some(u) = &state.latest {
                if newer_release(&self.update).is_some() {
                    ui.colored_label(egui::Color32::from_rgb(215, 160, 60), format!("{} is available", u.version));
                    if !u.url.is_empty() && ui.button("Open release page").clicked() {
                        let _ = webbrowser::open(&u.url);
                    }
                } else {
                    ui.colored_label(egui::Color32::from_rgb(64, 170, 90), format!("up to date ({})", env!("CARGO_PKG_VERSION")));
                }
                if !u.notes.is_empty() {
                    ui.small(&u.notes);
                }
            } else if !state.error.is_empty() {
                ui.small(&state.error);
            } else {
                ui.small("not checked — set an update URL");
            }
            if ui.button("Check now").clicked() {
                if self.cfg.update_url.is_empty() {
                    self.saved_note = "set an update URL first".into();
                } else {
                    spawn_update_check(Arc::clone(&self.update), self.cfg.update_url.clone());
                }
            }
        });
        ui.horizontal(|ui| {
            ui.label("Update URL");
            ui.add(
                egui::TextEdit::singleline(&mut self.cfg.update_url)
                    .desired_width(560.0)
                    .hint_text("https://…/version.json"),
            );
        });
        ui.add_space(8.0);

        egui::Grid::new("checks").num_columns(3).striped(true).spacing([12.0, 6.0]).show(ui, |ui| {
            ui.strong("Check");
            ui.strong("Status");
            ui.strong("Detail");
            ui.end_row();
            for c in &checks {
                ui.label(&c.label);
                ui.colored_label(c.status.color(), c.status.text());
                ui.vertical(|ui| {
                    ui.label(&c.detail);
                    if !c.fix.is_empty() {
                        ui.small(&c.fix);
                    }
                });
                ui.end_row();
            }
        });

        if !engine.paths.is_empty() {
            ui.add_space(8.0);
            ui.strong("Resolved layout");
            egui::Grid::new("paths").num_columns(2).spacing([12.0, 4.0]).show(ui, |ui| {
                for (k, v) in &engine.paths {
                    ui.monospace(k);
                    ui.monospace(v);
                    ui.end_row();
                }
            });
        }
    }

    fn ui_server(&mut self, ui: &mut egui::Ui) {
        ui.heading("Server");
        ui.horizontal(|ui| {
            ui.radio_value(&mut self.cfg.mode, Mode::Campaign, Mode::Campaign.label());
            ui.radio_value(&mut self.cfg.mode, Mode::MaxRoster, Mode::MaxRoster.label());
        });
        ui.small(self.cfg.mode.blurb());
        ui.add_space(6.0);

        ui.horizontal(|ui| {
            let running = self.server.is_some() || self.external.is_some();
            if ui.add_enabled(!running, egui::Button::new("Start")).clicked() {
                self.start_server();
            }
            if ui.add_enabled(running, egui::Button::new("Stop")).clicked() {
                self.stop_server();
            }
            match (&self.server, self.external) {
                (Some(r), _) => {
                    ui.colored_label(egui::Color32::from_rgb(64, 170, 90), format!("running (pid {})", r.child.id()));
                }
                (None, Some(pid)) => {
                    ui.colored_label(egui::Color32::from_rgb(64, 170, 90), format!("running (external, pid {pid})"));
                }
                (None, None) => {
                    ui.label("stopped");
                }
            }
        });
        ui.add_space(6.0);

        ui.strong("Engine log");
        let lines: Vec<String> = {
            let l = self.log.lock().unwrap();
            let start = l.len().saturating_sub(400);
            l.iter().skip(start).cloned().collect()
        };
        egui::Frame::group(ui.style()).show(ui, |ui| {
            egui::ScrollArea::vertical()
                .stick_to_bottom(true)
                .auto_shrink([false, false])
                .max_height(420.0)
                .show(ui, |ui| {
                    if lines.is_empty() {
                        ui.small("(nothing yet)");
                    }
                    for line in &lines {
                        ui.monospace(line);
                    }
                });
        });
    }

    fn ui_android(&mut self, ui: &mut egui::Ui) {
        ui.heading("Android side");
        ui.label("The client is the unmodified v9.0.0 APK. Two things must be true on the device:");
        ui.add_space(4.0);
        ui.label("1. Our lab CA is in the SYSTEM trust store (a user-store install is ignored: targetSdk 33).");
        ui.label("2. v900-lapis.gumi.sg resolves to THIS machine, and the server listens on port 443.");
        ui.add_space(4.0);
        ui.small("Everything else the client fetches (CDN, banners, notices) is a URL this server hands it in the boot reply, so the one hostname redirect covers it.");
        ui.add_space(8.0);
        ui.label("On a rooted device or emulator:");
        ui.code("hash=$(openssl x509 -in ca.pem -noout -subject_hash_old)   # then copy ca.pem to");
        ui.code("    /system/etc/security/cacerts/$hash.0   (mode 644, reboot)");
        ui.code("echo '<this-machine-ip> v900-lapis.gumi.sg' >> /system/etc/hosts");
        ui.add_space(6.0);
        ui.small("No root? The fallback is a re-signed APK whose network_security_config.xml trusts a user CA; the hostname redirect is still required.");
        ui.add_space(10.0);
        if ui.button("Check adb").clicked() {
            self.check_adb();
        }
        ui.small("adb output lands in the Server tab's log.");
    }
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.poll_server();
        let ctx = ui.ctx().clone();

        egui::Panel::top("tabs").show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.tab, Tab::Setup, "Setup / doctor");
                ui.selectable_value(&mut self.tab, Tab::Server, "Server");
                ui.selectable_value(&mut self.tab, Tab::Android, "Android side");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.small(format!("v{}", env!("CARGO_PKG_VERSION")));
                    if let Some(u) = newer_release(&self.update) {
                        if !u.url.is_empty() && ui.button(format!("update {} available", u.version)).clicked() {
                            let _ = webbrowser::open(&u.url);
                        }
                    }
                });
            });
        });

        egui::Panel::bottom("status").show(ui, |ui| {
            ui.horizontal(|ui| {
                match &self.server {
                    Some(r) => {
                        ui.colored_label(egui::Color32::from_rgb(64, 170, 90), format!("running pid {}", r.child.id()));
                    }
                    None => {
                        ui.label("idle");
                    }
                }
                ui.separator();
                ui.small(format!("data {}", self.cfg.data_root));
                ui.separator();
                ui.small(format!("port {}", self.cfg.port));
            });
        });

        egui::CentralPanel::default().show(ui, |ui| {
            egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| match self.tab {
                Tab::Setup => self.ui_setup(ui),
                Tab::Server => self.ui_server(ui),
                Tab::Android => self.ui_android(ui),
            });
        });

        let busy = self.update.lock().unwrap().checking || self.doctor.lock().unwrap().checking;
        if self.server.is_some() || busy {
            ctx.request_repaint_after(Duration::from_millis(500));
        }
    }
}

impl Drop for App {
    fn drop(&mut self) {
        self.stop_server();
    }
}

// ---------------------------------------------------------------- headless CLI
//
// The GUI is for people without a terminal; this is for scripts and for us: the harness and any
// automated run start/stop the server through the SAME app, so the engine contract, the mode, and
// the log path are decided in one place. Commands: start | stop | status | paths.

struct HeadlessArgs {
    data: String,
    engine: String,
    port: u16,
    mode: Mode,
    bind: String,
    extra_env: Vec<(String, String)>,
    /// `prepare` only: the checkout (data store) to link from; default = derived from the engine.
    from: Option<String>,
    /// `prepare` only: a save slot to seed the root's state dir with (server half only).
    state: Option<String>,
}

fn parse_headless(rest: &[String]) -> Result<HeadlessArgs, String> {
    let cfg = load_config();
    let mut a = HeadlessArgs {
        data: cfg.data_root,
        engine: cfg.engine_dir,
        port: cfg.port,
        mode: cfg.mode,
        bind: "0.0.0.0".to_string(),
        extra_env: Vec::new(),
        from: None,
        state: None,
    };
    let mut i = 0;
    while i < rest.len() {
        let need = |i: usize| rest.get(i + 1).cloned().ok_or_else(|| format!("{} needs a value", rest[i]));
        match rest[i].as_str() {
            "--data" => {
                a.data = need(i)?;
                i += 2;
            }
            "--engine" => {
                a.engine = need(i)?;
                i += 2;
            }
            "--bind" => {
                a.bind = need(i)?;
                i += 2;
            }
            "--port" => {
                a.port = need(i)?.parse().map_err(|_| "--port must be a number".to_string())?;
                i += 2;
            }
            "--mode" => {
                a.mode = match need(i)?.as_str() {
                    "campaign" => Mode::Campaign,
                    "maxroster" => Mode::MaxRoster,
                    other => return Err(format!("unknown mode {other} (campaign | maxroster)")),
                };
                i += 2;
            }
            "--campaign" => {
                a.mode = Mode::Campaign;
                i += 1;
            }
            "--maxroster" => {
                a.mode = Mode::MaxRoster;
                i += 1;
            }
            "--env" => {
                let kv = need(i)?;
                let (k, v) = kv.split_once('=').ok_or_else(|| format!("--env wants K=V, got {kv}"))?;
                a.extra_env.push((k.to_string(), v.to_string()));
                i += 2;
            }
            "--from" => {
                a.from = Some(need(i)?);
                i += 2;
            }
            "--state" => {
                a.state = Some(need(i)?);
                i += 2;
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    Ok(a)
}

fn read_pid(path: &Path) -> Option<u32> {
    std::fs::read_to_string(path).ok()?.trim().parse().ok()
}

#[cfg(unix)]
fn process_alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(windows)]
fn process_alive(pid: u32) -> bool {
    Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/NH"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains(&pid.to_string()))
        .unwrap_or(false)
}

/// Who owns a listening TCP port? Linux/macOS: parse ss; Windows: netstat -ano. Best effort -- None
/// means "cannot tell", which the caller treats as inconclusive rather than as success.
fn port_owner(port: u16) -> Option<u32> {
    #[cfg(unix)]
    let out = Command::new("ss").args(["-ltnp"]).output().ok()?;
    #[cfg(windows)]
    let out = Command::new("netstat").args(["-ano", "-p", "tcp"]).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    #[cfg(unix)]
    {
        for line in text.lines() {
            if line.contains(&format!(":{port} ")) {
                if let Some(i) = line.find("pid=") {
                    let digits: String = line[i + 4..].chars().take_while(|c| c.is_ascii_digit()).collect();
                    if let Ok(p) = digits.parse() {
                        return Some(p);
                    }
                }
            }
        }
    }
    #[cfg(windows)]
    {
        for line in text.lines() {
            let cols: Vec<&str> = line.split_whitespace().collect();
            if cols.len() >= 4 && cols[1].ends_with(&format!(":{port}")) && cols[3] == "LISTENING" {
                if let Ok(p) = cols[4].parse() {
                    return Some(p);
                }
            }
        }
    }
    None
}

/// The parent pid of `pid`, Linux only: `/proc/<pid>/stat` field 4. The `comm` field (2) is wrapped
/// in parentheses and may contain spaces or a `)`, so parse the fields AFTER the last `)`.
#[cfg(target_os = "linux")]
fn parent_of(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let mut fields = stat.rsplit_once(')')?.1.split_whitespace();
    fields.next()?; // state
    fields.next()?.parse().ok()
}

/// Was `pid` started by `root`, directly or through a chain of parents? A PyInstaller onefile
/// bootloader execs the real engine as a CHILD, so a frozen bundle's port owner is not the pid
/// `spawn()` returned. Accepting a descendant keeps the foreign-listener guard (0.1.1) while letting
/// the bundled engine start. No parent walk on macOS/Windows yet: a bundled engine there needs its own.
fn is_descendant(pid: u32, root: u32) -> bool {
    if pid == root {
        return true;
    }
    #[cfg(target_os = "linux")]
    {
        let mut cur = pid;
        for _ in 0..32 {
            match parent_of(cur) {
                Some(p) if p == root => return true,
                Some(p) if p > 1 => cur = p,
                _ => break,
            }
        }
    }
    false
}

/// Where a data root is prepared FROM: `--from DIR`, else walk up from the engine looking for the
/// shape `harness/worktree_prepare.sh` requires (`mst_decoded/` + `APK_emulator_work/out/`). A bundle
/// installed away from a checkout has no store, and then the error names `--from`.
fn find_data_store(engine: &Path, explicit: Option<&str>) -> Result<PathBuf, String> {
    // Canonicalize FIRST: the default engine path is built with `..` components
    // (`.../target/release/../../../server`), and a lexical parent walk on that string would spend its
    // budget popping `..` rather than climbing the real tree. (It did, and this line is why.)
    let engine = std::fs::canonicalize(engine).unwrap_or_else(|_| engine.to_path_buf());
    if let Some(d) = explicit {
        let p = std::fs::canonicalize(d).unwrap_or_else(|_| PathBuf::from(d));
        if p.join("mst_decoded").is_dir() && p.join("APK_emulator_work").is_dir() {
            return Ok(p);
        }
        return Err(format!("--from {d} is not a data store (needs mst_decoded/ and APK_emulator_work/)"));
    }
    let mut cur = Some(if engine.is_dir() {
        engine.clone()
    } else {
        engine.parent().ok_or("engine path has no parent")?.to_path_buf()
    });
    for _ in 0..8 {
        let d = match &cur {
            Some(d) => d.clone(),
            None => break,
        };
        if d.join("mst_decoded").is_dir() && d.join("APK_emulator_work/out").is_dir() {
            return Ok(d);
        }
        cur = d.parent().map(Path::to_path_buf);
    }
    Err(format!("no data store above the engine ({}) -- pass --from <checkout>", engine.display()))
}

#[cfg(unix)]
fn symlink_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(src, dst)
}

#[cfg(windows)]
fn symlink_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::os::windows::fs::symlink_dir(src, dst)
}

/// The one command that turns a checkout into a data root: a symlink farm + an optional save slot,
/// then the same `deps.json` rows the doctor shows, so anything still missing names its producer.
/// Idempotent and never destructive: an existing entry (a live root's state included) is skipped.
fn headless_prepare(a: &HeadlessArgs) -> i32 {
    // The layout the ENGINE resolves under a `--data` root (`server/data_paths.py`; `paths` prints it).
    const LINKS: &[(&str, &str, bool)] = &[
        ("mst_decoded", "mst_decoded", true),
        ("text_decoded", "text_decoded", true),
        ("client/files", "com.square_enix.android_googleplay.FFBEWW/files", true),
        ("client/shared_prefs", "com.square_enix.android_googleplay.FFBEWW/shared_prefs", true),
        ("certs", "APK_emulator_work/harness/certs", true),
        ("cpk_extract", "cpk_extract", false),
        ("overlay", "APK_emulator_work/out/cdn_overlay", false),
        ("gacha_community", "gacha_community", false),
        ("jp_battle_oracle", "jp_battle_oracle", false),
    ];
    let root = PathBuf::from(&a.data);
    let engine = Path::new(&a.engine);
    let store = match find_data_store(engine, a.from.as_deref()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("ffbe-server-app: {e}");
            return 2;
        }
    };
    println!("data store: {}", store.display());
    println!("data root:  {}", root.display());
    if let Err(e) = std::fs::create_dir_all(&root) {
        eprintln!("ffbe-server-app: cannot create {}: {e}", root.display());
        return 2;
    }
    let mut bad: Vec<&str> = Vec::new();
    if !is_engine(engine) {
        println!("  MISSING  engine {} -- point --engine/FFBE_SERVER_ENGINE at a checkout or the bundle", engine.display());
        bad.push("engine");
    }
    for &(link, src_rel, required) in LINKS {
        let dst = root.join(link);
        let src = store.join(src_rel);
        if std::fs::symlink_metadata(&dst).is_ok() {
            println!("  present  {link}");
            continue;
        }
        if !src.exists() {
            if required {
                println!("  MISSING  {link}  ({} not in the data store)", src.display());
                bad.push(src_rel);
            } else {
                println!("  skip     {link}  ({} absent; optional)", src.display());
            }
            continue;
        }
        if let Some(parent) = dst.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match symlink_dir(&src, &dst) {
            Ok(()) => println!("  linked   {link} -> {}", src.display()),
            Err(e) => {
                println!("  ERROR    {link}: {e}");
                bad.push(src_rel);
            }
        }
    }
    // The save slot's SERVER half. The client half (shared_prefs) is the harness's: `ffbe_run.sh --state`.
    let state_dir = root.join("state");
    let _ = std::fs::create_dir_all(&state_dir);
    let slots_dir = store.join("APK_emulator_work/out/state");
    match &a.state {
        Some(slot) => {
            let src = slots_dir.join(format!("{slot}.json"));
            let dst = state_dir.join(format!("{slot}.json"));
            if std::fs::symlink_metadata(&dst).is_ok() {
                println!("  present  state/{slot}.json");
            } else if src.is_file() {
                match std::fs::copy(&src, &dst) {
                    Ok(_) => println!("  copied   state/{slot}.json (server half; client half: harness/ffbe_run.sh --state {slot})"),
                    Err(e) => {
                        println!("  ERROR    state/{slot}.json: {e}");
                        bad.push("save slot");
                    }
                }
            } else {
                println!("  MISSING  state/{slot}.json (no {} in the store)", src.display());
                bad.push("save slot");
            }
        }
        None => {
            let mut slots: Vec<String> = Vec::new();
            if let Ok(rd) = std::fs::read_dir(&slots_dir) {
                for e in rd.flatten() {
                    if let Some(s) = e.file_name().to_str().and_then(|n| n.strip_suffix(".json")) {
                        if !s.contains(".prev") && !s.contains("predelete") && !s.ends_with(".meta") {
                            slots.push(s.to_string());
                        }
                    }
                }
            }
            slots.sort();
            println!("  state/   no slot seeded (--state <slot>); slots in the store: {}", if slots.is_empty() { "<none>".to_string() } else { slots.join(", ") });
        }
    }
    println!("checks (deps.json):");
    for c in check_data_root(&root) {
        println!("  {:<8} {} -- {}", c.status.text(), c.label, c.detail);
    }
    if bad.is_empty() {
        println!("ready: ffbe-server-app start --data {} --mode campaign", root.display());
        0
    } else {
        eprintln!("not ready: {}", bad.join(", "));
        2
    }
}
fn headless_paths(a: &HeadlessArgs) -> i32 {
    let mut cmd = engine_query(Path::new(&a.engine), &a.data);
    for (k, v) in &a.extra_env {
        cmd.env(k, v);
    }
    match cmd.status() {
        Ok(s) if s.success() => 0,
        Ok(s) => s.code().unwrap_or(1),
        Err(e) => {
            eprintln!("could not run the engine: {e}");
            2
        }
    }
}

fn headless_start(a: &HeadlessArgs) -> i32 {
    if a.data.trim().is_empty() {
        eprintln!("start needs a data root: --data DIR (or a saved config)");
        return 2;
    }
    let state_dir = Path::new(&a.data).join("state");
    if let Err(e) = std::fs::create_dir_all(&state_dir) {
        eprintln!("cannot create {}: {e}", state_dir.display());
        return 2;
    }
    let pidfile = state_dir.join("server.pid");
    if let Some(pid) = read_pid(&pidfile) {
        if process_alive(pid) {
            eprintln!("already running (pid {pid}); stop it first or use another data root");
            return 1;
        }
        let _ = std::fs::remove_file(&pidfile);
    }
    let log_path = state_dir.join("server.log");
    let log = match std::fs::OpenOptions::new().create(true).append(true).open(&log_path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("cannot open {}: {e}", log_path.display());
            return 2;
        }
    };
    let mut cmd = engine_serve(Path::new(&a.engine), &a.data, a.port, &a.bind, a.mode, true);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone().unwrap()))
        .stderr(Stdio::from(log));
    for (k, v) in &a.extra_env {
        cmd.env(k, v);
    }
    detach(&mut cmd);
    match cmd.spawn() {
        Ok(mut child) => {
            let pid = child.id();
            let _ = std::fs::write(&pidfile, format!("{pid}\n"));
            let addr = format!("127.0.0.1:{}", a.port);
            let mut up = false;
            let mut exited = None;
            for _ in 0..40 {
                // A child that died on EADDRINUSE/EACCES/missing Python must not be reported as started --
                // and a port that answers may be SOMEONE ELSE's server, so the child is the evidence.
                if let Ok(Some(st)) = child.try_wait() {
                    exited = Some(st);
                    break;
                }
                if let Ok(sock) = addr
                    .parse()
                    .map_err(|_| ())
                    .and_then(|sa| std::net::TcpStream::connect_timeout(&sa, Duration::from_millis(250)).map_err(|_| ()))
                {
                    drop(sock);
                    up = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(250));
            }
            // A connect that succeeded may have been answered by SOMEONE ELSE's listener: give the child
            // a beat, then require it to be alive and to own the port. Both checks are needed -- the child
            // can be breathing while the port belongs to another process, and vice versa.
            if up && exited.is_none() {
                std::thread::sleep(Duration::from_millis(400));
                if let Ok(Some(st)) = child.try_wait() {
                    exited = Some(st);
                } else if let Some(owner) = port_owner(a.port) {
                    if !is_descendant(owner, pid) {
                        eprintln!("port {} is served by pid {owner}, which is not the engine we started (pid {pid}) or any process it spawned -- stopping it",
                                  a.port);
                        terminate(pid);
                        let _ = std::fs::remove_file(&pidfile);
                        return 2;
                    }
                }
            }
            if let Some(st) = exited {
                eprintln!("engine exited immediately ({st}); last log lines:");
                let tail = std::fs::read_to_string(&log_path).unwrap_or_default();
                let lines: Vec<&str> = tail.lines().collect();
                for line in lines.iter().rev().take(6).collect::<Vec<_>>().iter().rev() {
                    eprintln!("  {line}");
                }
                let _ = std::fs::remove_file(&pidfile);
                return 2;
            }
            let meta = serde_json::json!({
                "pid": pid,
                "port": a.port,
                "mode": a.mode.label(),
                "data": a.data,
                "log": log_path.display().to_string(),
            });
            let _ = std::fs::write(state_dir.join("server.json"), meta.to_string());
            if up {
                println!("started pid {pid} on port {} ({}); log {}", a.port, a.mode.label(), log_path.display());
                0
            } else {
                println!("started pid {pid}, but nothing answered on port {} yet; log {}", a.port, log_path.display());
                2
            }
        }
        Err(e) => {
            eprintln!("could not start the engine: {e}");
            2
        }
    }
}

/// The newer release, if the last check found one: one comparison for the Setup pane and the top bar.
fn newer_release(update: &Arc<Mutex<UpdateState>>) -> Option<UpdateInfo> {
    let state = update.lock().unwrap();
    state
        .latest
        .clone()
        .filter(|u| version_tuple(&u.version) > version_tuple(env!("CARGO_PKG_VERSION")))
}

/// One stop semantic for the GUI and the CLI: SIGTERM, wait up to ~5 s, then SIGKILL (Windows: taskkill).
fn terminate(pid: u32) {
    #[cfg(unix)]
    unsafe {
        libc::kill(pid as i32, libc::SIGTERM);
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"]).output();
    }
    for _ in 0..20 {
        if !process_alive(pid) {
            return;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(pid as i32, libc::SIGKILL);
    }
}

fn headless_stop(a: &HeadlessArgs) -> i32 {
    let state_dir = Path::new(&a.data).join("state");
    let pidfile = state_dir.join("server.pid");
    let pid = match read_pid(&pidfile) {
        Some(p) => p,
        None => {
            println!("not running (no {})", pidfile.display());
            return 0;
        }
    };
    if !process_alive(pid) {
        let _ = std::fs::remove_file(&pidfile);
        println!("stale pidfile removed (pid {pid} is gone)");
        return 0;
    }
    terminate(pid);
    let _ = std::fs::remove_file(&pidfile);
    println!("stopped pid {pid}");
    0
}

fn headless_status(a: &HeadlessArgs) -> i32 {
    let state_dir = Path::new(&a.data).join("state");
    let pidfile = state_dir.join("server.pid");
    match read_pid(&pidfile) {
        Some(pid) if process_alive(pid) => {
            // Greppable, one fact per line: scripts read this, not a JSON blob.
            println!("running");
            println!("pid={pid}");
            let meta = std::fs::read_to_string(state_dir.join("server.json")).unwrap_or_default();
            if let Ok(serde_json::Value::Object(map)) = serde_json::from_str::<serde_json::Value>(&meta) {
                for k in ["port", "mode", "data", "log"] {
                    if let Some(v) = map.get(k) {
                        let s = match v {
                            serde_json::Value::String(s) => s.clone(),
                            other => other.to_string(),
                        };
                        println!("{k}={s}");
                    }
                }
            }
            0
        }
        Some(pid) => {
            println!("not running (stale pid {pid})");
            1
        }
        None => {
            println!("not running");
            1
        }
    }
}

fn headless(cmd: &str, rest: &[String]) -> i32 {
    let args = match parse_headless(rest) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("ffbe-server-app: {e}");
            return 2;
        }
    };
    match cmd {
        "paths" => headless_paths(&args),
        "prepare" => headless_prepare(&args),
        "start" => headless_start(&args),
        "stop" => headless_stop(&args),
        "status" => headless_status(&args),
        _ => 2,
    }
}

/// The CLI's front door. Commands are one word; no arguments opens the GUI.
fn print_usage() {
    println!("ffbe-server-app {} — the synthetic FFBE server", env!("CARGO_PKG_VERSION"));
    println!();
    println!("  (no arguments)                                     open the GUI");
    println!("  start   --data DIR [--port 443] [--mode campaign|maxroster] [--bind ADDR] [--env K=V]");
    println!("  stop    --data DIR");
    println!("  status  --data DIR");
    println!("  paths   --data DIR");
    println!("  prepare --data DIR [--from CHECKOUT] [--state SLOT]");
    println!();
    println!("  --engine PATH      engine to run (default: the bundle's engine, or a repo checkout)");
    println!("  --version | --help");
}
fn main() -> eframe::Result<()> {
    // Piping the CLI into `head` / `grep -q` is normal -- the harness does it in `--no-server` runs -- and
    // Rust ignores SIGPIPE, so without this the next println panics on EPIPE and a pipeline's `pipefail`
    // reads a healthy "running" line as a failure. Unix tools die quietly on a closed pipe; this is that.
    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
    let argv: Vec<String> = std::env::args().collect();
    // `--help` anywhere on the line, like `--version`: without a handler it fell through to the GUI and
    // could hang on a headless machine (found by the 2026-10-16 audit).
    if argv.iter().any(|a| a == "--help" || a == "-h") {
        print_usage();
        return Ok(());
    }
    if argv.iter().any(|a| a == "--version" || a == "-V") {
        println!("ffbe-server-app {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if let Some(cmd) = argv.get(1).map(String::as_str) {
        if matches!(cmd, "start" | "stop" | "status" | "paths" | "prepare") {
            std::process::exit(headless(cmd, &argv[2..]));
        }
    }
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1050.0, 760.0])
            .with_min_inner_size([840.0, 560.0])
            .with_title(APP_TITLE),
        ..Default::default()
    };
    eframe::run_native(APP_TITLE, options, Box::new(|_cc| Ok(Box::new(App::new()))))
}
