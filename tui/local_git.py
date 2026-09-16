"""Git 操作封装 — 提供安全的 Git 操作接口。

实现：
- commit_only(message): git add -A && git commit（只本地提交, 不推远端）
- commit_and_push(message): commit_only + 推送并校验远端
- commit_data_and_push(message): 自动流程专用, 只提交 output/ 与 state.json
- commit_config_and_push(message): 只提交 config/ 并推送
- ensure_on_main(): 启动期守卫, 保证 HEAD 在 main 上
- pull_latest(): git pull --rebase
- get_output_files(): 返回 output/ 下文件列表
- check_conflicts(): 检测冲突文件

推送一律走 _push_main_with_verify(): push 后用 ls-remote 断言远端 main 真的前进了,
因为 `git push` 退出码为 0 并不代表远端 ref 变动过(HEAD 不在 main 时会静默推空)。
"""

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MAIN_BRANCH = "main"
# 自动流程允许提交的路径(带尾斜杠的是目录)。收窄范围是为了堵住 config/ 被
# 顺带卷进 data: 提交 —— config/ 按约定只能由 TUI 的「配置推送」按钮推。
AUTO_PATHS = ("output", "state.json")
# 本地命令无需等网络; 凡是要跟远端说话的(push/pull)用长超时。
# ls-remote 只取 ref advertisement, 不该像 push 那样容忍慢链路 —— 它出现在
# 失败恢复路径上, 反复等 180s 会让本地脚本静默卡住十分钟。
LOCAL_TIMEOUT = 30
NET_TIMEOUT = 180
LS_TIMEOUT = 60

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# git 的交互式提示在无人值守环境里是致命的: 凭据失效时它会挂在原地等输入,
# 直到超时才变成一句含糊的 "Git command timed out"。禁掉后凭据问题会立刻
# 以 "could not read Username ... terminal prompts disabled" 暴露出来。
# LC_ALL/LANG 固定为 C 是为了让 git 的诊断永远走英文: 下面识别
# non-fast-forward 与 "nothing to commit" 靠的是英文子串, 中文 locale 下会静默失效。
_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "LC_ALL": "C",
    "LANG": "C",
}


def _run_git(args: list[str], capture: bool = True,
             timeout: int = LOCAL_TIMEOUT) -> tuple[int, str, str]:
    """运行 git 命令.

    Args:
        args: git 子命令与参数
        capture: 是否捕获输出
        timeout: 超时秒数(网络类命令传 NET_TIMEOUT)

    Returns:
        (returncode, stdout, stderr); 超时/找不到 git 时 returncode 为 -1
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=PROJECT_ROOT,
            capture_output=capture,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,   # 不给 stdin: 交互提示必须立即失败而不是挂住
            env=_GIT_ENV,
            creationflags=_NO_WINDOW,
            # git 输出理论上可能带非 UTF-8 字节(路径/提交信息), 解码失败会抛
            # UnicodeDecodeError —— 而下面的 except 不捕它, 会一路冒到调用方崩掉。
            errors="replace",
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Git command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", "Git not found"


def current_branch() -> str:
    """返回当前分支名; detached HEAD 返回字面量 "HEAD", 失败返回空串."""
    rc, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        return ""
    return out.strip()


def path_in_head(rel_path: str) -> bool:
    """HEAD 里是否存在该路径(仓库根相对). 只读, 不动工作区.

    用于区分"全新仓库"与"文件被误删": git 不可用/不在仓库内/HEAD 无此文件时
    一律返回 False —— 判不出来时按"没有"处理, 让调用方退回原有行为。
    """
    rc, _, _ = _run_git(["cat-file", "-e", f"HEAD:{rel_path}"])
    return rc == 0


def _rev_parse(rev: str = "HEAD") -> str:
    """把 rev 解析成完整 SHA; 失败返回空串."""
    rc, out, _ = _run_git(["rev-parse", rev])
    if rc != 0:
        return ""
    return out.strip()


def _is_non_fast_forward(text: str) -> bool:
    """判断 git 输出是否属于「远端更靠前, 需要先 rebase」这类拒绝."""
    t = text.lower()
    return any(s in t for s in (
        "non-fast-forward",
        "fetch first",
        "failed to push some refs",
        "updates were rejected",
    ))


def _short(sha: str) -> str:
    """取短 SHA, 便于写进日志与告警文案."""
    return (sha or "")[:8]


def _head_sha() -> str:
    """本地 HEAD 的 SHA."""
    return _rev_parse("HEAD")


def _remote_main_sha() -> tuple[bool, str, str]:
    """查询远端 main 的 SHA.

    Returns:
        (查询是否成功, 远端 SHA, 失败原因)
    """
    # --heads + 精确 refspec: 让 tag 永远不可能混进输出(否则同 SHA 的 tag 无害,
    # 但指到别处的 tag 会把校验结果搞反)。下面的解析也要求整行第二个字段
    # 正好是这个 ref, 而不是"扫到第一个 40 位 hex 就当结果"。
    rc, out, err = _run_git(["ls-remote", "--heads", "origin", f"refs/heads/{MAIN_BRANCH}"],
                            timeout=LS_TIMEOUT)
    if rc != 0:
        return False, "", err or "ls-remote failed"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{MAIN_BRANCH}":
            return True, parts[0], ""
    return True, "", ""


def push_main() -> dict[str, Any]:
    """把当前 HEAD 推到 origin main, 并校验远端真的前进了 (见 _push_main_with_verify)."""
    return _push_main_with_verify()


def _require_main() -> str:
    """提交前守卫: 返回原因字符串, 空串表示当前确实在 main 上.

    必须在 git add 之前调用 —— 提交一旦长在错误分支上, 事后拦 push 已经晚了
    (push 写死 origin main, 跟 HEAD 无关, 两边都"成功"却什么都没发生)。
    """
    branch = current_branch()
    if branch == MAIN_BRANCH:
        return ""
    if not branch:
        return "无法确定当前分支(git 不可用或不在仓库内)"
    if branch == "HEAD":
        return "当前处于 detached HEAD 状态(不在任何分支上)"
    return f"当前分支是 {branch}, 不是 {MAIN_BRANCH}"


def _commit_with_add(add_args: list[str], full_message: str) -> dict[str, Any]:
    """先 `git add <add_args>` 再提交; "nothing to commit" 视为成功(无变化不是错误).

    add_args 必须是完整的 add 参数: 全量提交传 ["-A"], 指定路径传 ["--", *paths]。
    不要统一写成 ["--"] + paths —— `git add -- -A` 会把 -A 当成**路径**解析并报
    "pathspec '-A' did not match any files"(rc=128), 选项必须出现在 -- 之前。
    """
    rc, _, err = _run_git(["add"] + list(add_args))
    if rc != 0:
        return {"success": False, "message": f"git add failed: {err}"}

    # 先问"有没有暂存内容", 而不是等 commit 报错再靠英文子串认。
    # `git diff --cached --quiet`: 0=无暂存改动, 1=有暂存改动, >1=出错。
    # 这样判定与 locale/版本无关, 也让"无变化"走的是干净路径而非错误路径。
    drc, _, derr = _run_git(["diff", "--cached", "--quiet"])
    if drc == 0:
        logger.info("Nothing to commit: %s", full_message)
        return {"success": True, "message": "Nothing to commit"}
    if drc != 1:
        return {"success": False, "message": f"无法判断暂存区状态: {derr}"}
    # 索引里有未解决的冲突条目时 `diff --cached --quiet` 同样返回 1, 但此时
    # commit 会以 "cannot commit because you have unmerged files" 失败。
    # 提前判出来, 免得用户对着一句含糊的 Commit failed 猜。
    conflicts = check_conflicts()
    if conflicts:
        return {"success": False,
                "message": (f"仓库有 {len(conflicts)} 个未解决的冲突文件, 请先解决: "
                            f"{', '.join(conflicts[:5])}")}

    rc, out, err = _run_git(["commit", "-m", full_message])
    if rc == 0:
        logger.info("Committed: %s", full_message)
        return {"success": True, "message": f"Committed: {full_message}"}
    if "nothing to commit" in (out + err).lower():
        logger.info("Nothing to commit: %s", full_message)
        return {"success": True, "message": "Nothing to commit"}
    return {"success": False, "message": f"Commit failed: {err}"}


def _commit_paths(paths: list[str], full_message: str) -> dict[str, Any]:
    """add 指定路径后提交(-- 之后一律按路径解析, 防路径名被当成选项)."""
    return _commit_with_add(["--"] + list(paths), full_message)


def _push_main_with_verify() -> dict[str, Any]:
    """把当前 HEAD 推到 origin main, 并验证远端 main 真的前进了.

    三步:
      1. 复核 HEAD 仍在 main(防 add 与 push 之间被切走);
      2. push; 失败时**先查远端**(超时的 push 可能其实已经成功), 确认真没动且
         属于 non-fast-forward 才 pull --rebase 重试一次, 重试前若有冲突必须
         rebase --abort, 绝不留下半截 rebase 状态;
      3. ls-remote 断言远端 SHA == 本地 HEAD, 不等即判失败。
         **这一步是消灭假成功的关键** —— `git push` 返回 0 但远端没动时必须报错。

    绝不使用 --force / --force-with-lease(项目禁则), 靠 rebase 追平远端。
    """
    guard = _require_main()
    if guard:
        return {"success": False, "message": f"拒绝推送: {guard}"}

    rc, out, err = _run_git(["push", "origin", MAIN_BRANCH], timeout=NET_TIMEOUT)
    if rc != 0:
        # `git push` 返回非 0 ≠ 远端没动: 慢链路/代理超时时服务端往往已经收下了,
        # 只是客户端没等到回执。此时唯一可信的判据是远端 ref 本身 —— 先问一句,
        # 真的同步了就是成功, 既不误报失败也不触发一次多余的 rebase。
        verified = _verify_remote_main()
        if verified["success"]:
            logger.warning("git push 返回 %s, 但远端 %s 已确认与本地 HEAD 一致, 判成功",
                           rc, MAIN_BRANCH)
            return verified

        combined = f"{out}\n{err}"
        if not _is_non_fast_forward(combined):
            return {"success": False, "message": f"Push failed: {err or out}"}
        # 远端被别处推进过: rebase 到远端之上再重试一次
        logger.warning("push 被拒(远端更靠前), 尝试 pull --rebase 后重试一次")
        prc, pout, perr = _run_git(["pull", "--rebase", "origin", MAIN_BRANCH],
                                   timeout=NET_TIMEOUT)
        if prc != 0:
            # 冲突时 git 会停在 rebase 中途, 必须回滚干净再返回, 否则整个仓库不可用
            arc, _, aerr = _run_git(["rebase", "--abort"])
            detail = f"{pout}\n{perr}".strip()
            if arc != 0:
                detail += f" (rebase --abort 亦失败: {aerr})"
            return {"success": False,
                    "message": f"rebase origin/{MAIN_BRANCH} 失败, 已回滚: {detail}"}

        rc, out, err = _run_git(["push", "origin", MAIN_BRANCH], timeout=NET_TIMEOUT)
        if rc != 0:
            return {"success": False,
                    "message": f"rebase 后重推仍失败: {err or out}"}
        logger.info("已 rebase 至 origin/%s 后重推成功", MAIN_BRANCH)

    return _verify_remote_main()


def _verify_remote_main(local_sha: str | None = None) -> dict[str, Any]:
    """断言远端 main 的 SHA 与本仓库 HEAD 一致; 不一致一律判失败."""
    local = local_sha or _head_sha()
    ok, remote, why = _remote_main_sha()
    if not ok:
        return {"success": False, "message": f"无法校验远端状态: {why}"}
    if not remote:
        return {"success": False,
                "message": f"远端不存在 {MAIN_BRANCH} 分支, 无法确认推送结果"}
    if remote != local:
        return {"success": False,
                "message": (f"推送后远端 {MAIN_BRANCH}({_short(remote)}) "
                            f"!= 本地 HEAD({_short(local)}), 视为失败")}
    return {"success": True,
            "message": f"已推送并确认远端 {MAIN_BRANCH} 前进到 {_short(remote)}"}


def _dirty_tracked_files() -> list[str] | None:
    """已跟踪文件中有改动(含已暂存/未暂存/已删除)的清单; 不含未跟踪文件.

    **读取失败返回 None, 不是 []** —— "问不出来"和"干净"是两回事, 返回的空列表
    会被调用方当成"可以安全切换分支", 在 git 出问题时正好做出最危险的决定。
    """
    # core.quotePath=false 让中文等非 ASCII 路径按原样显示(默认会被转义成
    # "\344\270\255..." 八进制), 这份清单是给用户照着自救的, 得能读懂。
    rc, out, _ = _run_git(["-c", "core.quotePath=false",
                           "status", "--porcelain", "--untracked-files=no"])
    if rc != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def _untracked_files() -> list[str]:
    """未跟踪文件清单(不阻塞切分支, 只作提示)."""
    rc, out, _ = _run_git(["ls-files", "--others", "--exclude-standard"])
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def ensure_on_main() -> dict[str, Any]:
    """确保 HEAD 在 main 上; 供自动流程启动时调用(早于任何抓取/读取状态).

    - 已在 main: 放行, 不看工作区脏不脏(本轮本来就要提交它)。
    - 非 main 且已跟踪文件有改动: **拒绝**, 不 add/不 commit/不切换, 原样返回失败。
      绝不动用户的改动 —— 自动 stash 再 pop 在 state.json 上必然冲突, 冲突标记
      写进 json 会让下一轮 json.load 直接崩。
    - 非 main 且已跟踪文件干净: checkout main, 并 best-effort pull --rebase
      (失败只 warning: 离线也该能产出报告, 推送那步会大声失败)。

    Returns:
        {"success": bool, "message": str, "switched": bool, "branch": str}
    """
    branch = current_branch()
    if not branch:
        return {"success": False, "switched": False, "branch": branch,
                "message": "无法确定当前分支(git 不可用?), 中止"}

    if branch == MAIN_BRANCH:
        return {"success": True, "switched": False, "branch": branch,
                "message": f"已在 {MAIN_BRANCH} 上"}

    if branch == "HEAD":
        return {"success": False, "switched": False, "branch": branch,
                "message": ("当前处于 detached HEAD, 不在任何分支上。"
                            "请先 `git checkout main` 再重跑")}

    dirty = _dirty_tracked_files()
    if dirty is None:
        return {"success": False, "switched": False, "branch": branch,
                "message": ("无法读取工作区状态(git status 失败), 无法确认改动是否已被"
                            "提交, 为安全起见不切换分支。请先手动确认后重跑")}
    if dirty:
        listing = "\n".join(f"  - {d}" for d in dirty[:50])
        if len(dirty) > 50:
            listing += f"\n  ... 另有 {len(dirty) - 50} 个文件"
        return {
            "success": False, "switched": False, "branch": branch,
            "message": (
                f"HEAD 在 {branch} 且有 {len(dirty)} 个已跟踪文件未提交, "
                f"拒绝自动切换(不会动你的任何改动):\n{listing}\n"
                f"请任选其一后重跑:\n"
                f"  1) 暂存改动: git stash\n"
                f"  2) 先提交到当前分支: git commit -am \"wip\"\n"
                f"  3) 干完活切回 main: git checkout {MAIN_BRANCH}"
            ),
        }

    untracked = _untracked_files()
    if untracked:
        # 未跟踪文件不会阻碍 checkout, 且自动提交路径已收窄到 AUTO_PATHS, 不会被提交
        logger.warning("工作区有 %d 个未跟踪文件(不影响切换, 也不会被自动提交)",
                       len(untracked))

    rc, out, err = _run_git(["checkout", MAIN_BRANCH])
    if rc != 0:
        return {"success": False, "switched": False, "branch": branch,
                "message": f"git checkout {MAIN_BRANCH} 失败: {err or out}"}

    # 这一步是显性行为变更, 用户必须看见"我的分支被自动切了"
    logger.warning("HEAD 原在 %s, 已自动切回 %s", branch, MAIN_BRANCH)

    # best-effort 追平远端: 离线时失败也只警告, 报告仍应产出, 推送那步会大声失败
    prc, _, perr = _run_git(["pull", "--rebase", "origin", MAIN_BRANCH],
                            timeout=NET_TIMEOUT)
    if prc != 0:
        logger.warning("切到 %s 后 pull --rebase 失败(离线可忽略, 推送时会再校验): %s",
                       MAIN_BRANCH, perr)

    return {"success": True, "switched": True, "branch": MAIN_BRANCH,
            "message": f"已从 {branch} 自动切回 {MAIN_BRANCH}"}


def commit_only(message: str) -> dict[str, Any]:
    """仅提交不推送(继续用 -A, 不加分支守卫).

    只本地提交、不推远端, 因此在任何分支上都无害 —— 加守卫反而会毁掉
    "先在特性分支上攒提交"的正常用法。

    Args:
        message: 提交信息（自动加 data: 前缀）

    Returns:
        {"success": bool, "message": str}
    """
    return _commit_with_add(["-A"], f"data: {message}")


def _existing_paths(paths: list[str]) -> list[str]:
    """只保留真实存在的路径.

    `git add -- a b` 遇到**任一**不存在的 pathspec 会整体失败(rc=128), 于是
    state.json 一旦缺失, 连 output/ 里已经产出好的报告都推不上去 —— 而
    `.github/workflows/daily.yml` 的抢救步骤偏偏专门处理了"state.json 缺失",
    说明这是被认可的可能状态, 两侧行为必须一致。

    只给"缺了也该继续"的调用方用: config/ 消失属于异常, 应当继续大声报错。
    """
    return [p for p in paths if os.path.exists(os.path.join(PROJECT_ROOT, p))]


def commit_data_and_push(message: str) -> dict[str, Any]:
    """自动流程专用收尾: 提交 output/ + state.json 并推送到 origin main.

    与 commit_only 的区别是路径收窄为 AUTO_PATHS —— 抓取过程可能改写
    config/sources.yaml(_prune_inactive_gzh), 那类文件按约定只能由 TUI 按钮推。
    """
    guard = _require_main()
    if guard:
        return {"success": False, "message": f"拒绝提交: {guard}"}

    targets = _existing_paths(list(AUTO_PATHS))
    missing = [p for p in AUTO_PATHS if p not in targets]
    if missing:
        # 跳过不存在的路径, 而不是让整条 add 失败。但要大声说出来:
        # state.json 缺失意味着本轮去重记录没落盘, 下一轮会重复处理同样内容。
        logger.warning("以下路径不存在, 已跳过(不影响其余产物推送): %s", ", ".join(missing))
    if not targets:
        # 一个都不在 = 本轮什么产物都没有, 属于异常, 不能静默当成成功
        return {"success": False,
                "message": f"待提交路径全部不存在({', '.join(AUTO_PATHS)}), 无可推送内容"}

    result = _commit_paths(targets, f"data: {message}")
    if not result["success"]:
        return result
    return push_main()


def commit_and_push(message: str) -> dict[str, Any]:
    """提交并推送.

    Args:
        message: 提交信息（自动加 data: 前缀）

    Returns:
        {"success": bool, "message": str}
    """
    guard = _require_main()
    if guard:
        return {"success": False, "message": f"拒绝提交: {guard}"}

    result = commit_only(message)
    if not result["success"]:
        return result
    push = _push_main_with_verify()
    if push["success"]:
        return {"success": True, "message": "Committed and pushed"}
    return push


def commit_config_and_push(message: str = "update config") -> dict[str, Any]:
    """只提交 config/ 目录(画像/关键词/配置)并推送.

    供 TUI 在本地改了用户画像、关键词等配置后固化并同步云端。
    不触碰 output/ 等(云端管理)。
    """
    guard = _require_main()
    if guard:
        return {"success": False, "message": f"拒绝提交: {guard}"}

    result = _commit_paths(["config/"], f"data: {message}")
    if not result["success"]:
        return result

    push = _push_main_with_verify()
    if push["success"]:
        return {"success": True, "message": "配置已提交并推送"}
    return {"success": False, "message": f"Commit ok, 但 {push['message']}"}


def pull_latest() -> dict[str, Any]:
    """拉取最新代码.

    Returns:
        {"success": bool, "message": str}
    """
    rc, out, err = _run_git(["pull", "--rebase", "origin", MAIN_BRANCH],
                            timeout=NET_TIMEOUT)
    if rc == 0:
        return {"success": True, "message": out or "Pulled successfully"}
    else:
        return {"success": False, "message": f"Pull failed: {err}"}


def get_output_files() -> list[str]:
    """返回 output/ 目录下 .md 文件列表."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    if not os.path.isdir(output_dir):
        return []
    files = sorted(
        [f for f in os.listdir(output_dir) if f.endswith(".md")],
        reverse=True,
    )
    return files


def check_conflicts() -> list[str]:
    """检测冲突文件列表.

    Returns:
        冲突文件路径列表
    """
    rc, out, err = _run_git(["diff", "--name-only", "--diff-filter=U"])
    if rc != 0:
        return []
    return [line for line in out.split("\n") if line]
