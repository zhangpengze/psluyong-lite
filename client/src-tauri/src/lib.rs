//! Tauri 外壳：负责窗口与 Python sidecar 的生命周期。
//!
//! 为什么用 sidecar 而不是纯前端：PS 只能通过 Windows COM 调，
//! 那是 Python(pywin32) 的活，前端碰不到。
//!
//! 窗口三件套（借自旧项目迷你界面）：
//! 置顶开关 / 贴边隐藏（露出 6px 窄条，鼠标碰到唤出，离开 0.8s 缩回）/ 位置记忆。

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, Position, WebviewWindow};

struct Sidecar(Mutex<Option<Child>>);

/// 贴边状态。放 Mutex 里供命令与轮询线程共享。
#[derive(Default)]
struct DockState {
    side: Option<String>,
    stop: Option<Arc<AtomicBool>>,
}

/// 贴边后露出的窄条宽度（物理像素）。太窄点不中，太宽挡内容。
const DOCK_STRIP_PX: i32 = 6;
/// 唤出区域的边缘容差
const EDGE_SLACK_PX: f64 = 8.0;
/// 鼠标离开窗口多久后缩回
const HIDE_DEBOUNCE_MS: u64 = 800;
/// 轮询间隔
const POLL_INTERVAL_MS: u64 = 250;

/// Windows 下不给子进程弹黑框
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn command_for(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut cmd = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    cmd
}

fn resolve_sidecar(app: &tauri::AppHandle) -> Option<Command> {    // 1) 环境变量直指可执行文件（打包后优先）
    if let Ok(exe) = std::env::var("PSLUYONG_SIDECAR_EXE") {
        let path = PathBuf::from(exe);
        if path.exists() {
            return Some(command_for(path));
        }
    }

    // 2) 资源目录 / 可执行文件同级
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join("sidecar-dist").join("psluyong-sidecar.exe"));
        candidates.push(resource_dir.join("psluyong-sidecar.exe"));
    }
    if let Ok(exe_dir) = std::env::current_exe().map(|p| p.parent().map(PathBuf::from)) {
        if let Some(dir) = exe_dir {
            candidates.push(dir.join("psluyong-sidecar.exe"));
            candidates.push(dir.join("sidecar").join("psluyong-sidecar.exe"));
        }
    }
    for path in candidates {
        if path.exists() {
            return Some(command_for(path));
        }
    }

    // 3) 开发模式：直接跑源码
    let repo_root = std::env::var("PSLUYONG_REPO_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            // src-tauri -> client -> 仓库根
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(|p| p.parent())
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."))
        });

    if repo_root.exists() {
        let python = std::env::var("PSLUYONG_PYTHON").unwrap_or_else(|_| "python".to_string());
        let mut cmd = command_for(python);
        cmd.current_dir(&repo_root)
            .args(["-m", "client.sidecar.server"]);
        return Some(cmd);
    }

    None
}

// ==================== 窗口三件套：置顶 / 贴边 / 位置记忆 ====================

#[tauri::command]
fn set_always_on_top(window: WebviewWindow, on: bool) -> Result<(), String> {
    window.set_always_on_top(on).map_err(|e| e.to_string())
}

/// 读取窗口位置与尺寸（物理像素），前端定期存进 sidecar 配置
#[tauri::command]
fn get_window_rect(window: WebviewWindow) -> Result<(i32, i32, u32, u32), String> {
    let pos = window.outer_position().map_err(|e| e.to_string())?;
    let size = window.outer_size().map_err(|e| e.to_string())?;
    Ok((pos.x, pos.y, size.width, size.height))
}

/// 恢复上次的位置。带屏幕边界校验：拔掉显示器后窗口不能"消失"。
#[tauri::command]
fn restore_window_rect(window: WebviewWindow, x: i32, y: i32) -> Result<(), String> {
    let monitors = window.available_monitors().map_err(|e| e.to_string())?;
    let visible_on_any_screen = monitors.iter().any(|m| {
        let mp = m.position();
        let ms = m.size();
        // 允许标题栏留 100px 出头在屏幕外（贴边状态本来就大部分在屏幕外）
        x >= mp.x - 100 && y >= mp.y - 100 && x < mp.x + ms.width as i32 && y < mp.y + ms.height as i32
    });
    if visible_on_any_screen {
        window
            .set_position(Position::Physical(PhysicalPosition::new(x, y)))
            .map_err(|e| e.to_string())
    } else {
        window.center().map_err(|e| e.to_string())
    }
}

fn stop_dock_thread(state: &Mutex<DockState>) {
    if let Ok(mut s) = state.lock() {
        if let Some(stop) = s.stop.take() {
            stop.store(true, Ordering::Relaxed);
        }
        s.side = None;
    }
}

/// 贴到屏幕左/右边缘：窗口大部分移出屏幕，只露 6px 窄条。
/// 鼠标碰到窄条滑出；离开窗口 0.8s 缩回；把窗口拖离边缘 = 取消贴边。
#[tauri::command]
fn dock_window(app: AppHandle, window: WebviewWindow, side: String) -> Result<(), String> {
    if side != "left" && side != "right" {
        return Err("side 只能是 left 或 right".into());
    }
    let state = app.state::<Mutex<DockState>>();
    stop_dock_thread(&state);

    let monitor = window
        .current_monitor()
        .map_err(|e| e.to_string())?
        .ok_or("找不到窗口所在的显示器")?;
    let mpos = monitor.position();
    let msize = monitor.size();
    let wsize = window.outer_size().map_err(|e| e.to_string())?;
    let pos = window.outer_position().map_err(|e| e.to_string())?;

    let (hidden_x, peek_x) = match side.as_str() {
        "left" => (mpos.x - wsize.width as i32 + DOCK_STRIP_PX, mpos.x),
        _ => (
            mpos.x + msize.width as i32 - DOCK_STRIP_PX,
            mpos.x + msize.width as i32 - wsize.width as i32,
        ),
    };
    let hidden_y = pos.y;

    window
        .set_position(Position::Physical(PhysicalPosition::new(hidden_x, hidden_y)))
        .map_err(|e| e.to_string())?;

    let stop = Arc::new(AtomicBool::new(false));
    if let Ok(mut s) = state.lock() {
        s.side = Some(side.clone());
        s.stop = Some(stop.clone());
    }

    let app2 = app.clone();
    let win = window.clone();
    let side2 = side.clone();
    thread::spawn(move || {
        dock_loop(&app2, &win, &side2, hidden_x, hidden_y, peek_x, stop);
    });
    Ok(())
}

#[tauri::command]
fn undock_window(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    let state = app.state::<Mutex<DockState>>();
    let side = state.lock().ok().and_then(|s| s.side.clone());
    stop_dock_thread(&state);

    // 从隐藏位拉回完全可见位（按原来的贴边方向）
    if let Some(side) = side {
        if let (Ok(wsize), Ok(pos)) = (window.outer_size(), window.outer_position()) {
            if let Ok(Some(monitor)) = window.current_monitor() {
                let mpos = monitor.position();
                let msize = monitor.size();
                let peek_x = match side.as_str() {
                    "left" => mpos.x,
                    _ => mpos.x + msize.width as i32 - wsize.width as i32,
                };
                let _ = window.set_position(Position::Physical(PhysicalPosition::new(peek_x, pos.y)));
            }
        }
    }
    let _ = app.emit("dock-changed", serde_json::json!({ "side": null }));
    Ok(())
}

fn dock_loop(
    app: &AppHandle,
    window: &WebviewWindow,
    side: &str,
    hidden_x: i32,
    hidden_y: i32,
    peek_x: i32,
    stop: Arc<AtomicBool>,
) {
    let mut outside_since: Option<Instant> = None;
    while !stop.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_millis(POLL_INTERVAL_MS));
        if stop.load(Ordering::Relaxed) {
            return;
        }

        let Ok(cursor) = app.cursor_position() else { continue };
        let Ok(wpos) = window.outer_position() else { continue };
        let Ok(wsize) = window.outer_size() else { continue };

        // 用户拖动了窗口（既不在隐藏位也不在唤出位）→ 视为取消贴边
        let at_hidden = (wpos.x - hidden_x).abs() < 4 && (wpos.y - hidden_y).abs() < 8;
        let at_peek = wpos.x == peek_x && wpos.y == hidden_y;
        if !at_hidden && !at_peek {
            stop_dock_thread(&app.state::<Mutex<DockState>>());
            let _ = app.emit("dock-changed", serde_json::json!({ "side": null }));
            return;
        }

        if at_hidden {
            // 鼠标碰到露出的窄条 → 滑出
            let in_show_zone = cursor.y >= hidden_y as f64 - EDGE_SLACK_PX
                && cursor.y <= (hidden_y + wsize.height as i32) as f64 + EDGE_SLACK_PX
                && match side {
                    "left" => cursor.x <= (hidden_x + wsize.width as i32 + DOCK_STRIP_PX) as f64 + EDGE_SLACK_PX,
                    _ => cursor.x >= hidden_x as f64 - EDGE_SLACK_PX,
                };
            if in_show_zone {
                let _ = window.set_position(Position::Physical(PhysicalPosition::new(peek_x, hidden_y)));
                outside_since = None;
            }
        } else {
            // 唤出状态：鼠标离开窗口超过防抖时长 → 缩回
            let inside_window = cursor.x >= wpos.x as f64
                && cursor.x <= (wpos.x + wsize.width as i32) as f64
                && cursor.y >= wpos.y as f64
                && cursor.y <= (wpos.y + wsize.height as i32) as f64;
            if inside_window {
                outside_since = None;
            } else {
                let since = *outside_since.get_or_insert(Instant::now());
                if since.elapsed() >= Duration::from_millis(HIDE_DEBOUNCE_MS) {
                    let _ = window.set_position(Position::Physical(PhysicalPosition::new(hidden_x, hidden_y)));
                    outside_since = None;
                }
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let child = match resolve_sidecar(&handle) {
                Some(mut cmd) => match cmd.spawn() {
                    Ok(child) => {
                        println!("sidecar 已启动，pid={}", child.id());
                        Some(child)
                    }
                    Err(e) => {
                        eprintln!("sidecar 启动失败: {e}");
                        None
                    }
                },
                None => {
                    eprintln!("未找到 sidecar，请确认已打包或设置 PSLUYONG_SIDECAR_EXE");
                    None
                }
            };
            app.manage(Sidecar(Mutex::new(child)));
            app.manage(Mutex::new(DockState::default()));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            set_always_on_top,
            get_window_rect,
            restore_window_rect,
            dock_window,
            undock_window
        ])
        .build(tauri::generate_context!())
        .expect("Tauri 初始化失败")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app_handle.try_state::<Sidecar>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            // 必须显式 kill：否则关窗后 python 还在后台占着 PS 的 COM
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        });
}
