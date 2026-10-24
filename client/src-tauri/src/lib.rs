//! Tauri 外壳：负责窗口、托盘、贴边引擎与 Python sidecar 的生命周期。
//!
//! 为什么用 sidecar 而不是纯前端：PS 只能通过 Windows COM 调，
//! 那是 Python(pywin32) 的活，前端碰不到。
//!
//! 窗口模型（迷你化改版后，一主两副）：
//! - main     迷你主窗：只放录用表单 + 今日状态，默认可置顶、可贴左/右/上三缘
//! - history  记录窗：  按需打开，全量记录 + 筛选 + 行内详情
//! - settings 设置窗：  按需打开，账号/模板/生成/外观与窗口/诊断
//! 三窗通过事件总线（broadcast_event）同步：生成完成、配置变更。
//!
//! 托盘：左键切换主窗显隐，右键菜单（记录/设置/退出）。
//! 关闭主窗 = 隐藏到托盘（防误关），真正退出只在托盘菜单或生成中确认。
//!
//! 单实例：监听 127.0.0.1:48732。首实例持有监听；双开时第二个实例连上
//! 发一个 "show" 通知首实例显示主窗，然后自己退出——否则两个 sidecar
//! 会抢 8765 端口，后起的那个必然启动失败。端口被别的程序占用时
//! （连不上）退化为"当首实例"，不影响使用。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::json;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::WebviewWindowBuilder;
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, Position, WebviewUrl, WebviewWindow,
};

/// 单实例通知端口（sidecar 用 8765，错开）
const INSTANCE_PORT: &str = "127.0.0.1:48732";
/// 首实例的监听器：run() 里绑定，setup() 里起接收线程
static INSTANCE_LISTENER: OnceLock<TcpListener> = OnceLock::new();

struct Sidecar(Mutex<Option<Child>>);

/// 是否正在生成（前端生成任务开始/结束时同步过来）。
/// 生成中直接关窗会中断 PS 任务，还可能让 Photoshop 留着打开的模板，
/// 所以 CloseRequested 时要拦下来问一句。
#[derive(Default)]
struct BusyFlag(Mutex<bool>);

/// 主窗置顶偏好。辅助窗打开时主窗临时让出置顶，辅助窗关闭时按这个恢复。
#[derive(Default)]
struct TopPref(Mutex<bool>);

/// 关闭主窗是否入托盘（设置里可关，默认开）。
#[derive(Default)]
struct CloseToTray(Mutex<bool>);

/// 托盘图标句柄：状态变化时更新 tooltip（今日 N · 待补传 M）。
#[derive(Default)]
struct TrayHandle(Mutex<Option<tauri::tray::TrayIcon>>);

/// 贴边状态。放 Mutex 里供命令与轮询线程共享。
#[derive(Default)]
struct DockState {
    side: Option<String>,
    stop: Option<Arc<AtomicBool>>,
    /// 滑出/缩回动画进行中：此时不判定"用户把窗口拖走了"
    animating: Arc<AtomicBool>,
    /// 最近一次取消贴边的时间：刚拉回窗口时给个宽限，防止立刻被吸回边缘
    last_undock: Option<Instant>,
}

/// 贴边后露出的窄条宽度（物理像素）。太窄点不中，太宽挡内容。
const DOCK_STRIP_PX: i32 = 6;
/// 唤出区域的边缘容差
const EDGE_SLACK_PX: f64 = 8.0;
/// 鼠标离开窗口多久后缩回
const HIDE_DEBOUNCE_MS: u64 = 800;
/// 轮询间隔
const POLL_INTERVAL_MS: u64 = 250;
/// 拖到屏幕边缘多少像素内松手算自动贴边
const AUTO_DOCK_EDGE_PX: i32 = 24;
/// 取消贴边后多久内不再自动贴边
const UNDOCK_GRACE_MS: u128 = 1200;
/// 滑出/缩回动画时长：瞬移太生硬，150ms 缓出刚好
const SLIDE_MS: u64 = 150;

/// Windows 下不给子进程弹黑框
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 把本进程 pid 传给子进程：父进程一死，子进程自己退出。
///
/// 不传的后果：Tauri 被任务管理器强杀后 sidecar 还在跑、还占着 8765，下次启动的
/// sidecar 绑不上端口、静默死在 DEVNULL，前端连到的是那个僵尸（busy/配置全是上一
/// 轮的）。约定与 Python 侧 `client/sidecar/parent_watch.py` 的
/// `PARENT_PID_ENV` 必须一致。
const PARENT_PID_ENV: &str = "PSLUYONG_PARENT_PID";

fn parent_pid_env() -> (String, String) {
    (PARENT_PID_ENV.to_string(), std::process::id().to_string())
}

fn tag_parent_pid(cmd: &mut Command) {
    let (key, value) = parent_pid_env();
    cmd.env(key, value);
}

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

/// 找 sidecar 可执行文件/命令。**所有返回路径都经过 tag_parent_pid**，
/// 分散在调用点去加的话早晚漏一处。
fn resolve_sidecar(app: &tauri::AppHandle) -> Option<Command> {
    let mut cmd = resolve_sidecar_raw(app)?;
    tag_parent_pid(&mut cmd);
    Some(cmd)
}

fn resolve_sidecar_raw(app: &tauri::AppHandle) -> Option<Command> {
    // 1) 环境变量直指可执行文件（打包后优先）
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
        // 开发模式不走 command_for：sidecar 日志继承到终端，
        // `npm run tauri:dev` 能直接看到 COM 报错，排错不用猜。
        // （打包模式保持 Stdio::null，员工不该看到控制台窗口）
        let mut cmd = Command::new(python);
        cmd.current_dir(&repo_root)
            .args(["-m", "client.sidecar.server"]);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        return Some(cmd);
    }

    None
}

// ==================== 窗口三件套：置顶 / 贴边 / 位置记忆 ====================

/// 主窗置顶（设置页与主窗按钮都走这个；偏好记在 TopPref 里，
/// 辅助窗关闭时按它恢复）
#[tauri::command]
fn set_main_always_on_top(app: AppHandle, on: bool) -> Result<(), String> {
    if let Some(main) = app.get_webview_window("main") {
        main.set_always_on_top(on).map_err(|e| e.to_string())?;
    }
    if let Some(state) = app.try_state::<TopPref>() {
        if let Ok(mut guard) = state.0.lock() {
            *guard = on;
        }
    }
    Ok(())
}

/// 前端把「是否在生成」同步过来，供关窗拦截与贴边缩回判断
#[tauri::command]
fn set_generating(state: tauri::State<'_, BusyFlag>, busy: bool) {
    if let Ok(mut guard) = state.0.lock() {
        *guard = busy;
    }
}

/// 员工确认「生成中也要退出」后调用：直接退出进程（sidecar 由
/// ExitRequested 统一回收）
#[tauri::command]
fn force_close(app: AppHandle) {
    app.exit(0);
}

/// 读取窗口位置与尺寸（物理像素），前端定期存进 sidecar 配置
#[tauri::command]
fn get_window_rect(window: WebviewWindow) -> Result<(i32, i32, u32, u32), String> {
    let pos = window.outer_position().map_err(|e| e.to_string())?;
    let size = window.outer_size().map_err(|e| e.to_string())?;
    Ok((pos.x, pos.y, size.width, size.height))
}

/// 恢复上次的位置。带屏幕边界校验：拔掉显示器后窗口不能"消失"。
fn restore_rect_checked(window: &WebviewWindow, x: i32, y: i32) -> Result<(), String> {
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

#[tauri::command]
fn restore_window_rect(window: WebviewWindow, x: i32, y: i32) -> Result<(), String> {
    restore_rect_checked(&window, x, y)
}

/// 隐藏主窗（Esc 键 / 托盘都用它）
#[tauri::command]
fn hide_main_window(window: WebviewWindow) -> Result<(), String> {
    window.hide().map_err(|e| e.to_string())
}

/// 主窗按内容自适应高度（宽度不动）。
///
/// 迷你主窗只有「模板+合作伙伴 / 作者 / 标题 / 生成按钮」四行，高度写死必然
/// 留死白：380 高时下方空出 140px 没有任何用途，260 高又装不下生成结果条。
/// 所以高度交给前端量出来，这里只做两件事：逻辑像素换算 + 兜底夹紧。
///
/// 员工手动拖过高度后前端会停止调用（App.vue 里的 autoFit），
/// 因此这里不需要记偏好，也不会和用户的拖拽抢。
#[tauri::command]
fn set_main_height(window: WebviewWindow, height: f64) -> Result<(), String> {
    if !(160.0..=900.0).contains(&height) {
        return Err("height 超出合理范围".into());
    }
    // inner_size 是物理像素，换算回逻辑宽度，避免高 DPI 下窗口被横向拉伸
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let size = window.inner_size().map_err(|e| e.to_string())?;
    let width = size.width as f64 / scale;
    window
        .set_size(tauri::LogicalSize::new(width, height))
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn set_close_to_tray(state: tauri::State<'_, CloseToTray>, on: bool) {
    if let Ok(mut guard) = state.0.lock() {
        *guard = on;
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

/// 贴到屏幕左/右/上边缘：窗口大部分移出屏幕，只露 6px 窄条。
/// 鼠标碰到窄条滑出；离开窗口 0.8s 缩回（生成中不缩）；把窗口拖离边缘 = 取消贴边。
#[tauri::command]
fn dock_window(app: AppHandle, window: WebviewWindow, side: String) -> Result<(), String> {
    if side != "left" && side != "right" && side != "top" {
        return Err("side 只能是 left / right / top".into());
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

    // 三缘的隐藏位/唤出位：横向贴边只改 x，顶部贴边只改 y
    let (hidden_x, hidden_y, peek_x, peek_y) = match side.as_str() {
        "left" => (mpos.x - wsize.width as i32 + DOCK_STRIP_PX, pos.y, mpos.x, pos.y),
        "right" => (
            mpos.x + msize.width as i32 - DOCK_STRIP_PX,
            pos.y,
            mpos.x + msize.width as i32 - wsize.width as i32,
            pos.y,
        ),
        _ => (
            pos.x,
            mpos.y - wsize.height as i32 + DOCK_STRIP_PX,
            pos.x,
            mpos.y,
        ),
    };

    window
        .set_position(Position::Physical(PhysicalPosition::new(hidden_x, hidden_y)))
        .map_err(|e| e.to_string())?;

    let stop = Arc::new(AtomicBool::new(false));
    let animating = state.lock().ok().map(|s| s.animating.clone()).unwrap_or_default();
    if let Ok(mut s) = state.lock() {
        s.side = Some(side.clone());
        s.stop = Some(stop.clone());
    }
    // 贴上也广播一次：前端靠它记住"这次贴的是哪一边"（下次启动还原用），
    // 方向是 Rust 按距离挑出来的，前端本来并不知道，只有这里能告诉它
    let _ = app.emit("dock-changed", json!({ "side": side.clone() }));

    let app2 = app.clone();
    let win = window.clone();
    let side2 = side.clone();
    thread::spawn(move || {
        dock_loop(
            &app2,
            &win,
            &side2,
            (hidden_x, hidden_y),
            (peek_x, peek_y),
            stop,
            animating,
        );
    });
    Ok(())
}

/// 窗口中心离哪条屏缘最近就贴哪条。
///
/// 底部不算候选：贴底会压住 Windows 任务栏，员工还要点别处。
/// 三边距离相等时取前面的（left），保证结果是确定的。
fn nearest_side(
    cx: f64,
    cy: f64,
    mpos: &PhysicalPosition<i32>,
    msize: &PhysicalSize<u32>,
) -> &'static str {
    let candidates = [
        ("left", cx - mpos.x as f64),
        ("right", (mpos.x + msize.width as i32) as f64 - cx),
        ("top", cy - mpos.y as f64),
    ];
    candidates
        .iter()
        .fold(None, |best: Option<(&str, f64)>, cur| match best {
            Some(b) if b.1 <= cur.1 => Some(b),
            _ => Some(*cur),
        })
        .map(|(s, _)| s)
        .unwrap_or("right")
}

/// 主窗贴到"离得最近的那一条屏缘"。
///
/// 贴哪一边不该由员工在设置里选——跟 QQ 的自动隐藏一样，窗口在哪条边就贴哪条。
/// 手动拖到 24px 内松手走 check_auto_dock（有阈值判断），点主窗上的贴边按钮走这里
/// （窗口可能停在屏幕中间，没有"最近的屏缘"概念，就按窗口中心到各边的距离挑）。
#[tauri::command]
fn dock_main_nearest(app: AppHandle) -> Result<(), String> {
    let Some(main) = app.get_webview_window("main") else {
        return Ok(());
    };
    let monitor = main
        .current_monitor()
        .map_err(|e| e.to_string())?
        .ok_or("找不到窗口所在的显示器")?;
    let pos = main.outer_position().map_err(|e| e.to_string())?;
    let size = main.outer_size().map_err(|e| e.to_string())?;

    let cx = pos.x as f64 + size.width as f64 / 2.0;
    let cy = pos.y as f64 + size.height as f64 / 2.0;
    let side = nearest_side(cx, cy, monitor.position(), monitor.size());
    dock_window(app, main, side.to_string())
}

/// 主窗贴边开关：空串 = 取消贴边。设置页改贴边方向也走这里
/// （命令操作的是主窗，不是调用者所在的窗口）。
#[tauri::command]
fn set_main_dock(app: AppHandle, side: String) -> Result<(), String> {
    let Some(main) = app.get_webview_window("main") else {
        return Ok(());
    };
    if side.is_empty() {
        return undock_window(app, main);
    }
    dock_window(app, main, side)
}

#[tauri::command]
fn undock_window(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    let state = app.state::<Mutex<DockState>>();
    let side = state.lock().ok().and_then(|s| s.side.clone());
    stop_dock_thread(&state);
    if let Ok(mut s) = state.lock() {
        s.last_undock = Some(Instant::now());
    }

    // 从当前位滑到完全可见位（按原来的贴边方向），带动画
    if let Some(side) = side {
        if let (Ok(wsize), Ok(pos)) = (window.outer_size(), window.outer_position()) {
            if let Ok(Some(monitor)) = window.current_monitor() {
                let mpos = monitor.position();
                let msize = monitor.size();
                let (peek_x, peek_y) = match side.as_str() {
                    "left" => (mpos.x, pos.y),
                    "right" => (mpos.x + msize.width as i32 - wsize.width as i32, pos.y),
                    _ => (pos.x, mpos.y),
                };
                slide_window(
                    &window,
                    (pos.x, pos.y),
                    (peek_x, peek_y),
                    Arc::new(AtomicBool::new(false)),
                    Arc::new(AtomicBool::new(false)),
                );
            }
        }
    }
    let _ = app.emit("dock-changed", json!({ "side": null }));
    Ok(())
}

/// 拖拽结束后的自动贴边判定（前端监听 tauri://move，防抖后调用）。
/// 已贴边 / 刚取消贴边 / 最大化 都不判；取最靠近且 24px 内的一缘贴上。
#[tauri::command]
fn check_auto_dock(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    if window.is_maximized().unwrap_or(false) {
        return Ok(());
    }
    let state = app.state::<Mutex<DockState>>();
    {
        let s = state.lock().map_err(|_| "状态锁异常")?;
        if s.side.is_some() {
            return Ok(()); // 已经贴着
        }
        if let Some(t) = s.last_undock {
            if t.elapsed().as_millis() < UNDOCK_GRACE_MS {
                return Ok(()); // 刚拉回来，别立刻吸回去
            }
        }
    }

    let Some(monitor) = window.current_monitor().map_err(|e| e.to_string())? else {
        return Ok(());
    };
    let pos = window.outer_position().map_err(|e| e.to_string())?;
    let size = window.outer_size().map_err(|e| e.to_string())?;
    let mpos = monitor.position();
    let msize = monitor.size();

    let dist_left = pos.x - mpos.x;
    let dist_right = mpos.x + msize.width as i32 - (pos.x + size.width as i32);
    let dist_top = pos.y - mpos.y;

    let mut best: Option<(&str, i32)> = None;
    for (side, dist) in [("left", dist_left), ("right", dist_right), ("top", dist_top)] {
        if dist <= AUTO_DOCK_EDGE_PX && (best.is_none() || dist < best.unwrap().1) {
            best = Some((side, dist));
        }
    }
    if let Some((side, _)) = best {
        dock_window(app, window, side.to_string())?;
    }
    Ok(())
}

/// 窗口位移动画：150ms 缓出（ease-out cubic）。
/// stop 被置位时立刻收手，不硬跳到目标位——用户正在拖窗。
fn slide_window(
    window: &WebviewWindow,
    from: (i32, i32),
    to: (i32, i32),
    animating: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
) {
    animating.store(true, Ordering::Relaxed);
    let window = window.clone();
    thread::spawn(move || {
        let steps = 10u32;
        for i in 1..=steps {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let t = i as f64 / steps as f64;
            let e = 1.0 - (1.0 - t).powi(3);
            let x = from.0 + ((to.0 - from.0) as f64 * e).round() as i32;
            let y = from.1 + ((to.1 - from.1) as f64 * e).round() as i32;
            let _ = window.set_position(Position::Physical(PhysicalPosition::new(x, y)));
            thread::sleep(Duration::from_millis(SLIDE_MS / steps as u64));
        }
        if !stop.load(Ordering::Relaxed) {
            let _ = window.set_position(Position::Physical(PhysicalPosition::new(to.0, to.1)));
        }
        animating.store(false, Ordering::Relaxed);
    });
}

fn dock_loop(
    app: &AppHandle,
    window: &WebviewWindow,
    side: &str,
    hidden: (i32, i32),
    peek: (i32, i32),
    stop: Arc<AtomicBool>,
    animating: Arc<AtomicBool>,
) {
    let mut outside_since: Option<Instant> = None;
    while !stop.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_millis(POLL_INTERVAL_MS));
        if stop.load(Ordering::Relaxed) {
            return;
        }

        // 生成中不自动缩回：员工要盯着进度条，窗口缩走等于看不见了
        let busy = app
            .try_state::<BusyFlag>()
            .and_then(|s| s.0.lock().ok().map(|g| *g))
            .unwrap_or(false);

        let Ok(cursor) = app.cursor_position() else { continue };
        let Ok(wpos) = window.outer_position() else { continue };
        let Ok(wsize) = window.outer_size() else { continue };

        // 动画进行中：位置既不在隐藏位也不在唤出位，跳过判定
        if animating.load(Ordering::Relaxed) {
            outside_since = None;
            continue;
        }

        // 用户拖动了窗口（既不在隐藏位也不在唤出位）→ 视为取消贴边
        let at_hidden = (wpos.x - hidden.0).abs() < 4 && (wpos.y - hidden.1).abs() < 8;
        let at_peek = (wpos.x - peek.0).abs() < 4 && (wpos.y - peek.1).abs() < 8;
        if !at_hidden && !at_peek {
            stop_dock_thread(&app.state::<Mutex<DockState>>());
            let _ = app.emit("dock-changed", json!({ "side": null }));
            return;
        }

        if at_hidden {
            // 鼠标碰到露出的窄条 → 滑出
            let in_show_zone = match side {
                "left" => {
                    cursor.y >= hidden.1 as f64 - EDGE_SLACK_PX
                        && cursor.y <= (hidden.1 + wsize.height as i32) as f64 + EDGE_SLACK_PX
                        && cursor.x <= (hidden.0 + wsize.width as i32 + DOCK_STRIP_PX) as f64
                            + EDGE_SLACK_PX
                }
                "right" => {
                    cursor.y >= hidden.1 as f64 - EDGE_SLACK_PX
                        && cursor.y <= (hidden.1 + wsize.height as i32) as f64 + EDGE_SLACK_PX
                        && cursor.x >= hidden.0 as f64 - EDGE_SLACK_PX
                }
                _ => {
                    // 上缘：横向落在窗口范围内、纵向碰到顶部窄条
                    cursor.x >= hidden.0 as f64 - EDGE_SLACK_PX
                        && cursor.x <= (hidden.0 + wsize.width as i32) as f64 + EDGE_SLACK_PX
                        && cursor.y <= (hidden.1 + wsize.height as i32 + DOCK_STRIP_PX) as f64
                            + EDGE_SLACK_PX
                }
            };
            if in_show_zone {
                let win2 = window.clone();
                let stop2 = stop.clone();
                let anim2 = animating.clone();
                thread::spawn(move || slide_window(&win2, (wpos.x, wpos.y), peek, anim2, stop2));
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
            } else if !busy {
                let since = *outside_since.get_or_insert(Instant::now());
                if since.elapsed() >= Duration::from_millis(HIDE_DEBOUNCE_MS) {
                    let win2 = window.clone();
                    let stop2 = stop.clone();
                    let anim2 = animating.clone();
                    thread::spawn(move || slide_window(&win2, (wpos.x, wpos.y), hidden, anim2, stop2));
                    outside_since = None;
                }
            }
        }
    }
}

// ==================== 多窗口工厂：记录窗 / 设置窗 ====================

/// 打开（或聚焦）辅助窗口。主窗会临时让出置顶——辅助窗是要操作的，
/// 被迷你窗压住没法用；辅助窗关闭时按 TopPref 恢复。
#[tauri::command]
fn open_aux_window(app: AppHandle, kind: String, x: i32, y: i32) -> Result<(), String> {
    open_aux_impl(&app, &kind, x, y)
}

fn open_aux_impl(app: &AppHandle, kind: &str, x: i32, y: i32) -> Result<(), String> {
    // 尺寸按"最长的那一屏内容"定：记录窗一屏约 11 行，设置窗最高的
    // 「高级」分组 403px + 标签 34 + 保存栏 53 ≈ 490，给到 540 就不需要滚了。
    let (label, title, w, h) = match kind {
        "history" => ("history", "录用记录", 840.0, 640.0),
        "settings" => ("settings", "设置", 640.0, 540.0),
        _ => return Err("kind 只能是 history 或 settings".into()),
    };
    if let Some(win) = app.get_webview_window(label) {
        win.show().map_err(|e| e.to_string())?;
        win.set_focus().map_err(|e| e.to_string())?;
        return Ok(());
    }

    if let Some(main) = app.get_webview_window("main") {
        let _ = main.set_always_on_top(false);
    }

    let url = WebviewUrl::App(PathBuf::from(format!("index.html#/{kind}")));
    let builder = WebviewWindowBuilder::new(app, label, url)
        .title(title)
        .inner_size(w, h)
        .min_inner_size(560.0, 440.0)
        .decorations(true)
        .skip_taskbar(true)
        .visible(false);
    let win = builder.build().map_err(|e| e.to_string())?;

    if x >= 0 && y >= 0 {
        let _ = restore_rect_checked(&win, x, y);
    } else {
        let _ = win.center();
    }
    win.show().map_err(|e| e.to_string())?;
    let _ = win.set_focus();
    Ok(())
}

// ==================== 托盘 ====================

fn toggle_main_window(app: &AppHandle) {
    if let Some(main) = app.get_webview_window("main") {
        if main.is_minimized().unwrap_or(false) {
            let _ = main.unminimize();
        }
        if main.is_visible().unwrap_or(false) {
            let _ = main.hide();
        } else {
            let _ = main.show();
            let _ = main.set_focus();
        }
    }
}

/// 把主窗"拿给员工看"要做哪几步。
///
/// 抽成纯函数是因为窗口操作在测试里跑不起来（需要真窗口），而这几步的**顺序
/// 与"绝不隐藏"**才是要紧的地方：先还原再显示，最后一定聚焦。
#[derive(Debug, PartialEq, Eq)]
enum ShowStep {
    Unminimize,
    Show,
    Focus,
}

fn show_steps(minimized: bool, visible: bool) -> Vec<ShowStep> {
    let mut steps = Vec::new();
    if minimized {
        steps.push(ShowStep::Unminimize);
    }
    if !visible {
        steps.push(ShowStep::Show);
    }
    // 已经显示着也要聚焦：窗口可能被别的程序压在下面，
    // 员工双击图标就是想让它到前面来
    steps.push(ShowStep::Focus);
    steps
}

/// 显示并聚焦主窗。**永远不隐藏。**
///
/// 与 `toggle_main_window` 的区别不是风格问题：双击桌面图标的意思是"把窗口给
/// 我看"。用 toggle 的写法，员工上一次用托盘/快捷键把窗口收起来之后，再双击
/// 图标反而会把它继续藏着——看起来就是"软件点了没反应"，然后再双击一次又
/// 冒出来，来回横跳。托盘菜单里的「显示/隐藏主窗」才该是 toggle 语义。
fn show_main_window(app: &AppHandle) {
    if let Some(main) = app.get_webview_window("main") {
        let steps = show_steps(
            main.is_minimized().unwrap_or(false),
            main.is_visible().unwrap_or(false),
        );
        for step in steps {
            let _ = match step {
                ShowStep::Unminimize => main.unminimize(),
                ShowStep::Show => main.show(),
                ShowStep::Focus => main.set_focus(),
            };
        }
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let menu = Menu::with_items(
        app,
        &[
            &MenuItem::with_id(app, "toggle", "显示/隐藏主窗", true, None::<&str>)?,
            &MenuItem::with_id(app, "history", "打开记录", true, None::<&str>)?,
            &MenuItem::with_id(app, "settings", "打开设置", true, None::<&str>)?,
            &MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?,
        ],
    )?;
    // 没有图标就不建托盘（宁缺勿崩）：托盘只是便利入口，不能因为它挡住启动
    let Some(icon) = app.default_window_icon().cloned() else {
        eprintln!("缺少应用图标，跳过托盘创建");
        return Ok(());
    };

    let tray = TrayIconBuilder::new()
        .icon(icon)
        .tooltip("期刊录用助手")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => toggle_main_window(app),
            "history" => {
                let _ = open_aux_impl(app, "history", -1, -1);
            }
            "settings" => {
                let _ = open_aux_impl(app, "settings", -1, -1);
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    if let Some(state) = app.try_state::<TrayHandle>() {
        if let Ok(mut guard) = state.0.lock() {
            *guard = Some(tray);
        }
    }
    Ok(())
}

/// 托盘气泡文案：今日进度 + 待补传，员工不开窗也知道状态
#[tauri::command]
fn set_tray_tooltip(app: AppHandle, text: String) -> Result<(), String> {
    // State 借用要绑在局部变量上：直接链式返回会让 guard 悬垂
    let state = app.try_state::<TrayHandle>();
    let guard = match state.as_ref().map(|s| s.0.lock()) {
        Some(Ok(g)) => g,
        _ => return Ok(()),
    };
    if let Some(tray) = guard.as_ref() {
        let _ = tray.set_tooltip(Some(&text));
    }
    Ok(())
}

// ==================== 跨窗口事件总线 ====================

/// 前端调它广播事件（生成完成、配置变更），Rust 转发给所有窗口
#[tauri::command]
fn broadcast_event(app: AppHandle, name: String, payload: serde_json::Value) {
    let _ = app.emit(&name, payload);
}

// ==================== 单实例 ====================

/// 首实例监听双开通知：第二个实例连上发 "show"，这里把主窗拿给员工看
fn spawn_instance_listener(app: &AppHandle) {
    let Some(listener) = INSTANCE_LISTENER.get() else {
        return;
    };
    let Ok(listener) = listener.try_clone() else {
        return;
    };
    let app2 = app.clone();
    thread::spawn(move || {
        for stream in listener.incoming() {
            if let Ok(mut stream) = stream {
                let mut buf = [0u8; 16];
                let _ = stream.read(&mut buf);
                // 只能是"显示"，不能是 toggle：双击图标时窗口若正好是显示的，
                // toggle 会把它藏起来，员工看到的是"点了没反应"
                show_main_window(&app2);
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 单实例：先占端口。占不上就尝试通知首实例后退出；
    // 端口被别的程序占用时（连不上）当首实例照常运行。
    match TcpListener::bind(INSTANCE_PORT) {
        Ok(listener) => {
            let _ = INSTANCE_LISTENER.set(listener);
        }
        Err(_) => {
            if let Ok(addr) = INSTANCE_PORT.parse() {
                if let Ok(mut stream) =
                    TcpStream::connect_timeout(&addr, Duration::from_millis(300))
                {
                    let _ = stream.write_all(b"show");
                    let _ = stream.flush();
                }
            }
            std::process::exit(0);
        }
    }

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
            app.manage(BusyFlag::default());
            app.manage(TopPref::default());
            app.manage(CloseToTray(Mutex::new(true)));
            app.manage(TrayHandle::default());

            // 托盘失败只记日志：没有托盘也还能用主窗，不该让启动直接失败
            if let Err(e) = build_tray(&handle) {
                eprintln!("托盘创建失败: {e}");
            }
            spawn_instance_listener(&handle);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let label = window.label();
                if label == "main" {
                    // 生成中关窗：拦下来交给前端确认，避免一次误点中断整轮 PS 导出
                    let busy = window
                        .state::<BusyFlag>()
                        .0
                        .lock()
                        .map(|guard| *guard)
                        .unwrap_or(false);
                    if busy {
                        api.prevent_close();
                        let _ = window.emit("close-requested-while-busy", ());
                        return;
                    }
                    // 关闭 = 入托盘：员工误点不丢进程，退出走托盘菜单
                    let close_to_tray = window
                        .state::<CloseToTray>()
                        .0
                        .lock()
                        .map(|guard| *guard)
                        .unwrap_or(true);
                    if close_to_tray {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                    // 否则放行：真关（辅助窗还开着时进程不退出，最后一个窗关掉才退）
                } else {
                    // 辅助窗关闭：主窗恢复置顶偏好（打开辅助窗时临时让出过）
                    if let Some(main) = window.app_handle().get_webview_window("main") {
                        let pref = window
                            .state::<TopPref>()
                            .0
                            .lock()
                            .map(|guard| *guard)
                            .unwrap_or(false);
                        let _ = main.set_always_on_top(pref);
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            set_main_always_on_top,
            set_generating,
            force_close,
            get_window_rect,
            restore_window_rect,
            dock_window,
            undock_window,
            set_main_dock,
            dock_main_nearest,
            check_auto_dock,
            open_aux_window,
            hide_main_window,
            set_main_height,
            set_close_to_tray,
            set_tray_tooltip,
            broadcast_event
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

// ==================== 贴边方向的纯函数测试 ====================
// 挑哪条边是"窗口在哪就贴哪"的关键一步：贴错了员工看到的是窗口跑去了另一头，
// 而这玩意儿在 Tauri 里跑起来需要一个真窗口，所以把计算抽成纯函数单独验。
#[cfg(test)]
mod dock_tests {
    use super::*;

    fn monitor(x: i32, y: i32, w: u32, h: u32) -> (PhysicalPosition<i32>, PhysicalSize<u32>) {
        (PhysicalPosition::new(x, y), PhysicalSize::new(w, h))
    }

    #[test]
    fn 贴到离得最近的那一条边() {
        let (mpos, msize) = monitor(0, 0, 1920, 1080);
        // 窗口中心落在左上区域 → 左
        assert_eq!(nearest_side(120.0, 300.0, &mpos, &msize), "left");
        // 靠右 → 右
        assert_eq!(nearest_side(1800.0, 300.0, &mpos, &msize), "right");
        // 靠上 → 上
        assert_eq!(nearest_side(900.0, 20.0, &mpos, &msize), "top");
        // 屏幕正中（900,540）：距顶 540 < 距左 900 < 距右 1020 → 收上去。
        // 居中时贴顶部比贴左右更安全：左右容易挡住旁边的窗口
        assert_eq!(nearest_side(900.0, 540.0, &mpos, &msize), "top");
    }

    #[test]
    fn 距离相等时结果确定() {
        let (mpos, msize) = monitor(0, 0, 1920, 1080);
        // 窗口中心在屏幕水平中点且偏下：左右距离相等（960），且都比距上近，
        // 这时必须稳定取 left，不能随遍历顺序变来变去
        assert_eq!(nearest_side(960.0, 1000.0, &mpos, &msize), "left");
        assert_eq!(nearest_side(960.0, 1000.0, &mpos, &msize), "left");
    }

    #[test]
    fn 副屏坐标偏移也算对() {
        // 主屏 1920 宽，副屏从左往右排 → 副屏原点 x=1920
        let (mpos, msize) = monitor(1920, 0, 1920, 1080);
        assert_eq!(nearest_side(1950.0, 500.0, &mpos, &msize), "left");
        assert_eq!(nearest_side(3800.0, 500.0, &mpos, &msize), "right");
    }

    #[test]
    fn 底部永远不是候选() {
        let (mpos, msize) = monitor(0, 0, 1920, 1080);
        // 窗口死死贴住底边：贴底会压住 Windows 任务栏，员工还要点别处，
        // 所以只能在左/右/上里挑
        for (cx, cy) in [(960.0, 1070.0), (100.0, 1070.0), (1800.0, 1070.0)] {
            let side = nearest_side(cx, cy, &mpos, &msize);
            assert!(
                side == "left" || side == "right" || side == "top",
                "底部不该成为候选，实际返回 {side}"
            );
        }
    }
}

// ==================== 单实例：第二个实例只许"显示" ====================
// 员工双击桌面图标 = "把窗口给我看"。原来这里复用了托盘的 toggle：
// 窗口正好显示着时，双击图标会把它藏起来，看起来就是"点了没反应"，
// 再双击一次又冒出来，来回横跳。窗口操作要真窗口，所以把步骤抽成纯函数验。
#[cfg(test)]
mod instance_tests {
    use super::*;

    #[test]
    fn 四种可见性组合都以聚焦收尾() {
        for (minimized, visible) in [(false, false), (false, true), (true, false), (true, true)] {
            let steps = show_steps(minimized, visible);
            assert_eq!(
                steps.last(),
                Some(&ShowStep::Focus),
                "minimized={minimized} visible={visible} 的最后一步必须是聚焦"
            );
        }
    }

    #[test]
    fn 已经显示且未最小化时只聚焦不重复show() {
        // 这一条就是 bug 的形状：旧实现走 toggle 分支会去 hide
        assert_eq!(show_steps(false, true), vec![ShowStep::Focus]);
    }

    #[test]
    fn 最小化时先还原再聚焦() {
        assert_eq!(show_steps(true, true), vec![ShowStep::Unminimize, ShowStep::Focus]);
    }

    #[test]
    fn 藏起来时必须show() {
        assert_eq!(show_steps(false, false), vec![ShowStep::Show, ShowStep::Focus]);
    }

    #[test]
    fn 最小化且不可见时还原_显示_聚焦() {
        assert_eq!(
            show_steps(true, false),
            vec![ShowStep::Unminimize, ShowStep::Show, ShowStep::Focus]
        );
    }

    #[test]
    fn 双开监听器不许调到托盘那个切换() {
        // 别的地方改不回来：这一条盯的是"用错函数"，纯逻辑测试看不出来
        let src = include_str!("lib.rs").replace("\r\n", "\n");
        let bad = format!("toggle_main_window(&{})", "app2");
        assert!(
            !src.contains(&bad),
            "第二个实例必须显示主窗；复用 toggle 会让双击图标把窗口藏起来"
        );
        let good = format!("show_main_window(&{})", "app2");
        assert!(src.contains(&good), "第二个实例应当调用 show_main_window");
    }
}

// ==================== 父进程 pid 必须传下去 ====================
// sidecar 靠这个 pid 判断"我的爹还在不在"。漏传的话它永远不会自己退出：
// Tauri 被强杀后僵尸 sidecar 一直占着 8765，下次启动的新 sidecar 绑不上端口、
// 静默死在 DEVNULL，前端连到的是上一轮那个（busy 永远 true）。
#[cfg(test)]
mod parent_pid_tests {
    use super::*;

    #[test]
    fn 环境变量名与_python_侧一致() {
        // Python: client/sidecar/parent_watch.py 的 PARENT_PID_ENV
        assert_eq!(parent_pid_env().0, "PSLUYONG_PARENT_PID");
    }

    #[test]
    fn pid_是当前进程且合法() {
        let pid: u32 = parent_pid_env().1.parse().expect("pid 必须能解析成数字");
        assert!(pid > 0);
        assert_eq!(pid, std::process::id());
    }

    #[test]
    fn 找_sidecar_的地方必须带上_pid() {
        // "忘了调 tag_parent_pid"是这类接线的典型错法，逻辑测试看不出来。
        // needle 里带上换行和缩进：否则断言文案自己就把 needle 喂给了 contains
        // （踩过一次：文案里写了那句调用，守卫于是永远为真）
        //
        // 还要先统一行尾：这个文件是 CRLF，needle 里写 \n 而磁盘上是 \r\n，
        // contains 永远为假 —— 守卫会一直红（或者反过来永远绿），比没有更糟。
        let src = include_str!("lib.rs").replace("\r\n", "\n");
        let call = format!("\n    {}(&mut cmd);\n", "tag_parent_pid");
        assert!(src.contains(&call), "resolve_sidecar 包装层漏了 pid 传递");
        let wrapper = format!("resolve_sidecar_raw({})?", "app");
        assert!(src.contains(&wrapper), "所有返回路径都要经过同一个包装函数");
    }
}
