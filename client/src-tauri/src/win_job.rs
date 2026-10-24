//! 把 sidecar（以及它后面起的 worker 孙进程）关进一个 Job Object
//!
//! 父进程看门狗（`PSLUYONG_PARENT_PID`）解决的是"sidecar 发现爹没了自己走"，
//! 但它有最多 2 秒的反应窗口，而且**孙进程还是靠 sidecar 先死**才被回收：
//! Tauri 被 `TerminateProcess`（任务管理器结束进程、安装器强杀、蓝屏前的
//! 那种硬关）时，worker 里那个 Photoshop COM 调用不会被任何 Python 代码收拾。
//!
//! Job Object 是让内核替你兜底：`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 打开后，
//! 只要 job 的最后一个句柄被关闭（Tauri 进程无论怎么死，句柄都由 OS 关掉），
//! job 里所有进程连同它们后来生出来的子进程一起被终止。
//!
//! 已知边界（写在这里而不是藏起来）：
//!   - 只能管到"入 job 之后"才生出来的子进程。sidecar 是先入 job、后按需起
//!     worker 的，所以 worker 天生在 job 里；反过来则不成立。
//!   - job 句柄**故意不关闭**：关掉就等于立刻杀掉 sidecar。它的生命周期就是
//!     Tauri 进程的生命周期，进程退出时由 OS 统一关闭——这正是我们要的触发点。
//!   - 失败只记日志不阻断启动：Job Object 是兜底，看门狗才是主路径。为了兜底
//!     失败而让客户端起不来，是本末倒置。

/// 把一个刚 spawn 的子进程放进全局 job。
///
/// 失败返回错误字符串（调用方只记日志）。重复调用是安全的：job 只建一次，
/// 后续只是把新进程加进去。
#[cfg(windows)]
pub fn guard(child: &std::process::Child) -> Result<(), String> {
    imp::guard(child)
}

/// 非 Windows 构建（CI 的 Linux 检查）直接放行
#[cfg(not(windows))]
pub fn guard(_child: &std::process::Child) -> Result<(), String> {
    Ok(())
}

#[cfg(windows)]
mod imp {
    use std::ffi::c_void;
    use std::process::Child;
    use std::sync::{Mutex, OnceLock};
    use windows_sys::Win32::Foundation::{CloseHandle, FALSE, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT};
    use windows_sys::Win32::Storage::FileSystem::SYNCHRONIZE;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
        JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SET_QUOTA,
        PROCESS_TERMINATE,
    };

    /// job 句柄。存成 usize 是因为 HANDLE 是裸指针，不实现 Send/Sync；
    /// **故意永不关闭**，见文件头注释。
    static JOB: OnceLock<usize> = OnceLock::new();
    static INIT: Mutex<()> = Mutex::new(());

    pub(super) fn guard(child: &Child) -> Result<(), String> {
        let job = shared_job()?;
        assign(job, child.id())
    }

    fn shared_job() -> Result<HANDLE, String> {
        if let Some(raw) = JOB.get() {
            return Ok(*raw as HANDLE);
        }
        let _lock = INIT.lock().map_err(|_| "job 初始化锁被毒化".to_string())?;
        if let Some(raw) = JOB.get() {
            return Ok(*raw as HANDLE);
        }
        let job = create_job()?;
        let _ = JOB.set(job as usize);
        Ok(job)
    }

    /// 建一个"最后一个句柄关闭即杀光"的 job
    pub(crate) fn create_job() -> Result<HANDLE, String> {
        create_job_with_limit(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    }

    /// 建 job 并设置 LimitFlags。
    ///
    /// 单独抽出来是为了能建一个**不带** `KILL_ON_JOB_CLOSE` 的对照 job：
    /// 只有对照组也观察到了"关句柄不杀进程"，才能说明杀进程这件事确实是这个
    /// 标志位干的，而不是"进程本来就死了"之类的巧合。
    pub(crate) fn create_job_with_limit(flags: u32) -> Result<HANDLE, String> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err(format!(
                    "CreateJobObject 失败：{}",
                    std::io::Error::last_os_error()
                ));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = flags;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                let err = std::io::Error::last_os_error();
                CloseHandle(job);
                return Err(format!("SetInformationJobObject 失败：{err}"));
            }
            Ok(job)
        }
    }

    /// 读回 job 的 LimitFlags（诊断/测试用）
    pub(crate) fn limit_flags(job: HANDLE) -> Result<u32, String> {
        unsafe {
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            let mut ret: u32 = 0;
            let ok = QueryInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &mut info as *mut _ as *mut c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                &mut ret as *mut u32,
            );
            if ok == 0 {
                return Err(format!(
                    "QueryInformationJobObject 失败：{}",
                    std::io::Error::last_os_error()
                ));
            }
            Ok(info.BasicLimitInformation.LimitFlags)
        }
    }

    /// 把指定 pid 放进 job
    pub(crate) fn assign(job: HANDLE, pid: u32) -> Result<(), String> {
        unsafe {
            // 只用这两个权限：SET_QUOTA 是 AssignProcessToJobObject 的要求，
            // TERMINATE 留给 job 终止时使用。要得越少，被杀软拦下的概率越低。
            let proc = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, FALSE, pid);
            if proc.is_null() {
                return Err(format!(
                    "OpenProcess({pid}) 失败：{}",
                    std::io::Error::last_os_error()
                ));
            }
            let ok = AssignProcessToJobObject(job, proc);
            let err = std::io::Error::last_os_error();
            CloseHandle(proc);
            if ok == 0 {
                return Err(format!("AssignProcessToJobObject 失败：{err}"));
            }
            Ok(())
        }
    }

    /// 诊断用：进程当前在不在某个 job 里
    pub(crate) fn in_job(pid: u32) -> bool {
        unsafe {
            let proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
            if proc.is_null() {
                return false;
            }
            let mut inside: i32 = 0;
            let ok = IsProcessInJob(proc, std::ptr::null_mut(), &mut inside);
            CloseHandle(proc);
            ok != 0 && inside != 0
        }
    }

    /// 等到进程退出（测试用）。
    ///
    /// 返回 `Some(true)` 已退出 / `Some(false)` 超时仍在跑 / `None` **不知道**
    /// （拿不到进程句柄，或者 `WaitForSingleObject` 自己失败了）。
    ///
    /// ⚠️ 三态而不是 bool：这个函数最初写成 `rc != WAIT_TIMEOUT` 返回 bool，
    /// 而 `OpenProcess` 只申请了 `PROCESS_QUERY_LIMITED_INFORMATION`——**没有
    /// `SYNCHRONIZE`**，`WaitForSingleObject` 于是每次都返回 `WAIT_FAILED`，
    /// 被当成"已退出"。结果是两条用例永远绿：它们根本没在等进程退出，
    /// 而是每次都被喂了一个"是"。变异验证时才发现（`LimitFlags` 改成 0
    /// 用例照样过）。
    pub(crate) fn wait_exit(pid: u32, timeout_ms: u32) -> Option<bool> {
        unsafe {
            // SYNCHRONIZE 是 WaitForSingleObject 的硬要求，少了它只会拿到 WAIT_FAILED
            let proc = OpenProcess(SYNCHRONIZE, FALSE, pid);
            if proc.is_null() {
                return None;
            }
            let rc = WaitForSingleObject(proc, timeout_ms);
            CloseHandle(proc);
            match rc {
                WAIT_OBJECT_0 => Some(true),
                WAIT_TIMEOUT => Some(false),
                _ => None,
            }
        }
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::imp;
    use std::process::{Child, Command, Stdio};
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

    /// 起一个 30 秒不退的子进程（`timeout` 在无控制台时不可靠，用 ping 垫时间）
    fn spawn_sleeper() -> Child {
        Command::new("cmd")
            .args(["/c", "ping", "-n", "30", "127.0.0.1"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("起子进程失败")
    }

    /// 「进程还活着」这三个字必须先被验证过，再往下断言它"后来死了"。
    /// 少了这一步，用例在进程根本没起来时照样绿。
    fn assert_alive(pid: u32) {
        assert_eq!(
            imp::wait_exit(pid, 300),
            Some(false),
            "子进程在关句柄之前就已经退出（或 wait_exit 拿不到真实结果），后面那条断言等于没测"
        );
    }

    #[test]
    fn 子进程能进_job() {
        let job = imp::create_job().expect("建 job 失败");
        let mut child = spawn_sleeper();

        imp::assign(job, child.id()).expect("AssignProcessToJobObject 失败");
        assert!(imp::in_job(child.id()), "进程没进 job，兜底就是空的");
        assert_alive(child.id());

        let _ = child.kill();
        let _ = child.wait();
        unsafe { CloseHandle(job) };
    }

    #[test]
    fn job_必须带_kill_on_job_close() {
        // 直接读回内核里的标志位：这是"兜底到底有没有打开"的唯一确凿证据，
        // 不依赖任何时序。
        let job = imp::create_job().expect("建 job 失败");
        let flags = imp::limit_flags(job).expect("QueryInformationJobObject 失败");
        assert_ne!(
            flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            0,
            "LimitFlags=0x{flags:x} 里没有 KILL_ON_JOB_CLOSE，这个 job 是个摆设"
        );
        unsafe { CloseHandle(job) };
    }

    #[test]
    fn 关掉最后一个句柄会带走子进程() {
        // 这条就是 KILL_ON_JOB_CLOSE 的全部意义：Tauri 被强杀时句柄由 OS 关闭，
        // 于是 sidecar 和 worker 一起被内核收走，不需要任何 Python 代码配合。
        let job = imp::create_job().expect("建 job 失败");
        let mut child = spawn_sleeper();
        imp::assign(job, child.id()).expect("AssignProcessToJobObject 失败");
        assert_alive(child.id());

        unsafe { CloseHandle(job) };

        assert_eq!(
            imp::wait_exit(child.id(), 5_000),
            Some(true),
            "关闭 job 句柄之后子进程还活着，KILL_ON_JOB_CLOSE 没生效"
        );
        let _ = child.wait();
    }

    #[test]
    fn 对照组_没有该标志时关句柄不杀进程() {
        // 对照组：同样的流程、同样的进程，只是 LimitFlags=0。
        // 它必须**活下来**——否则"关句柄会杀进程"这个结论根本站不住脚，
        // 上一条用例就只是在观察"进程碰巧自己退了"。
        let job = imp::create_job_with_limit(0).expect("建 job 失败");
        let mut child = spawn_sleeper();
        imp::assign(job, child.id()).expect("AssignProcessToJobObject 失败");
        assert_alive(child.id());

        unsafe { CloseHandle(job) };

        assert_eq!(
            imp::wait_exit(child.id(), 1_500),
            Some(false),
            "没有 KILL_ON_JOB_CLOSE 的 job，关句柄居然也把进程杀了——说明用例测的另有其因"
        );
        let _ = child.kill();
        let _ = child.wait();
    }
}
