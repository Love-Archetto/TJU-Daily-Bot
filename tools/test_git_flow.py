"""回归测试：自动流程的「提交 + 推送」不再静默假成功。

背景（原 bug）：
    收尾的自动提交推送会「看着成功、远端却没动」。根因是提交作用于当前分支而 push
    写死 `origin main` —— HEAD 不在 main 时 `git push origin main` 报
    `Everything up-to-date`、退出码 0，被当成成功。修完的代码在 tui/local_git.py
    与 src/main.py。

本脚本做什么：
    在 tempfile.mkdtemp() 里造一个**完全离线的**裸库当 origin + 一份工作副本，
    把 tui.local_git.PROJECT_ROOT 指向副本（_run_git 调用时读该模块级全局），
    逐条跑断言式场景。零网络、零鉴权。

绝不触碰：
    真实仓库的 .git、真实 origin。脚本开头就断言 PROJECT_ROOT 指向临时目录，
    否则直接拒绝运行。结束时 shutil.rmtree(ignore_errors=True)。

用法：
    .venv/Scripts/python.exe tools/test_git_flow.py

退出码：0 = ALL PASS；1 = 有 [FAIL]。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_BEIJING_TZ = timezone(timedelta(hours=8))

# Windows 控制台默认 GBK，中文/emoji 会崩；先切 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tui import local_git as lg  # noqa: E402

# 这些是 _run_git 用的 git 环境变量（含禁交互提示）；为避免"改了 HOME 却漏了
# 某个 git 自己发现的身份文件"，统一在每次 git 调用上重写为固定值。
_ENV_NAME = "TJU Test Bot"
_ENV_EMAIL = "test-bot@example.invalid"

_RESULTS = []  # [(ok: bool, name: str, detail: str)]


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def record(ok: bool, name: str, detail: str = "") -> bool:
    _RESULTS.append((ok, name, detail))
    if ok:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name}: {detail}")
    return ok


def check(name: str, condition: bool, detail: str = "") -> bool:
    return record(bool(condition), name, detail)


def git(*args, cwd=None, timeout=30):
    """对副本仓库跑真 git（显式环境，绕开 _run_git，避免 monkeypatch 干扰夹具构建）."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "GIT_AUTHOR_NAME": _ENV_NAME,
        "GIT_AUTHOR_EMAIL": _ENV_EMAIL,
        "GIT_COMMITTER_NAME": _ENV_NAME,
        "GIT_COMMITTER_EMAIL": _ENV_EMAIL,
    }
    r = subprocess.run(
        ["git"] + list(args), cwd=cwd or lg.PROJECT_ROOT, text=True,
        capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def bare_ref_sha(bare: str, ref: str = "main") -> str:
    rc, out, _ = git("-C", bare, "rev-parse", f"refs/heads/{ref}", cwd=bare)
    return out if rc == 0 else ""


def bare_all_commits(bare: str) -> list[str]:
    """裸库所有分支可达提交的 subject（用于证明 rebase/合并而非 force）."""
    rc, out, _ = git("-C", bare, "log", "--all", "--format=%s", cwd=bare)
    return [ln for ln in out.splitlines() if ln] if rc == 0 else []


def bare_commit_subjects(bare: str, ref: str = "main") -> list[str]:
    rc, out, _ = git("-C", bare, "log", "--format=%s", f"refs/heads/{ref}", cwd=bare)
    return [ln for ln in out.splitlines() if ln] if rc == 0 else []


def bare_tree_files(bare: str, ref: str = "main") -> list[str]:
    rc, out, _ = git("-C", bare, "ls-tree", "-r", "--name-only", f"refs/heads/{ref}", cwd=bare)
    return [ln for ln in out.splitlines() if ln] if rc == 0 else []


def commit_files(sha: str) -> list[str]:
    rc, out, _ = git("show", "--name-only", "--format=", sha)
    return [ln for ln in out.splitlines() if ln] if rc == 0 else []


def make_other_clone(label: str) -> str:
    """从裸库再 clone 一份（模拟"别处"），返回其路径。"""
    dest = tempfile.mkdtemp(prefix=f"tju-test-{label}-")
    r = subprocess.run(["git", "clone", _bare, dest], cwd=_TMP_ROOT, text=True,
                       capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"clone {label} 失败: {r.stderr}")
    return dest


def seed_and_clone(root: str) -> tuple[str, str]:
    """建裸库（origin）+ 工作副本，返回 (bare, work).

    副本里预置 output/、state.json、config/ 与 .gitignore，模拟真实仓库形状。
    """
    bare = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")

    r = subprocess.run(["git", "init", "--bare", "-b", "main", bare],
                       text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git init --bare 失败: {r.stderr}")

    r = subprocess.run(["git", "clone", bare, work],
                       text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git clone 失败: {r.stderr}")

    old_root = lg.PROJECT_ROOT
    lg.PROJECT_ROOT = work
    try:
        write(os.path.join(work, ".gitignore"), "history/\n")
        write(os.path.join(work, "output", "seed.md"), "# seed\n")
        write(os.path.join(work, "state.json"), '{"processed_links": []}\n')
        write(os.path.join(work, "config", "sources.yaml"), "sources: []\n")
        write(os.path.join(work, "README.md"), "seed\n")
        git("add", "-A")
        git("commit", "-m", "seed: initial layout")
        git("push", "-u", "origin", "main")
    finally:
        lg.PROJECT_ROOT = old_root
    return bare, work


_TMP_ROOT = None      # 临时根目录
_bare = ""            # 裸库（origin）
_work = ""            # 工作副本


def _force_rmtree(path: str) -> None:
    """删临时目录（Windows 上 git 对象是只读的，plain rmtree 会失败留垃圾）.

    注意：**不能**传 ignore_errors=True —— 它会吞掉异常并且**不调用** onerror，
    只读对象就永远清不掉。只读问题必须靠 onerror 里 chmod 再重试。
    """
    def _onexc(func, p, exc):
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:
            pass

    for _ in range(3):  # 少数文件可能被短时占用，重试几轮
        if not os.path.exists(path):
            return
        try:
            shutil.rmtree(path, onerror=_onexc)
        except Exception:
            pass


def _require_temp_root() -> None:
    """安全闸：PROJECT_ROOT 必须指向临时目录，否则拒绝运行。

    这是「绝不触碰真实仓库」的最后一道防线 —— 一旦 _run_git 用的 PROJECT_ROOT
    指向真实仓库，本脚本的 push 会真的打到真实 origin。
    """
    if not _TMP_ROOT:
        raise SystemExit("内部错误: 临时根目录未初始化")
    real = os.path.realpath(lg.PROJECT_ROOT).replace("\\", "/").rstrip("/")
    tmp = os.path.realpath(_TMP_ROOT).replace("\\", "/").rstrip("/")
    # 不能用 os.path.commonpath: 盘符不同(如 C: vs D:)会抛 ValueError
    if real == tmp or not real.startswith(tmp + "/"):
        raise SystemExit(
            f"拒绝运行: lg.PROJECT_ROOT({real}) 不在临时目录({tmp}) 内。"
            " 这会让 push 打到真实 origin。"
        )
    if real != os.path.realpath(_work).replace("\\", "/").rstrip("/"):
        raise SystemExit(
            f"拒绝运行: lg.PROJECT_ROOT({real}) != 工作副本({_work})"
        )


def _reset_work(*, keep_dirty: bool = False) -> None:
    """把工作副本恢复到与 origin/main 一致的干净状态（保留未跟踪文件由调用方清理）."""
    git("checkout", "-f", "main")
    # 清掉未跟踪文件（除了 .gitignore 里的），保证每个场景从同一张白纸开始
    git("clean", "-fd")
    git("reset", "--hard", "origin/main")
    if not keep_dirty:
        pass


# --------------------------------------------------------------------------
# 场景 A：正常推 main + 路径收窄
# --------------------------------------------------------------------------
def scenario_a() -> None:
    name = "A. 正常推 main（提交只含 output/ 与 state.json，config/ 不被偷渡）"
    _reset_work()

    # 本轮的产物
    write(os.path.join(_work, "output", "2026-09-16_12-00-00.md"), "# report A\n")
    write(os.path.join(_work, "state.json"), '{"last_run": "a"}\n')
    # 故意改脏 config/ —— 它绝不能被卷进这个 data: 提交
    write(os.path.join(_work, "config", "sources.yaml"), "sources:\n  - name: dirty\n")

    before = bare_ref_sha(_bare)
    r = lg.commit_data_and_push("test A")

    if not check(f"{name} — commit_data_and_push 成功", r.get("success") is True, str(r)):
        return

    head = git("rev-parse", "HEAD")[1]
    after = bare_ref_sha(_bare)
    check(f"{name} — 裸库 main SHA == 副本 HEAD", after == head and head != "",
          f"remote={after[:8]} head={head[:8]}")
    check(f"{name} — 裸库 main 确实前进", after != before, f"before={before[:8]} after={after[:8]}")

    files = commit_files(head)
    bad = [f for f in files if not (f.startswith("output/") or f == "state.json")]
    check(f"{name} — 提交只含 output/ 与 state.json", not bad,
          f"多出: {bad} (全部: {files})")

    # config/ 的改动必须原样留在工作区（收窄的证据，而不是被提交掉）
    rc, out, _ = git("status", "--porcelain", "--", "config/sources.yaml")
    # 未提交的已跟踪修改 = "M ..."（helper 已 strip 前导空格；若被提交掉则为空/未跟踪）
    xcode = out[:1]
    check(f"{name} — config/sources.yaml 仍留在工作区（未被提交）",
          xcode in ("M", "A", "T"), f"porcelain={out!r} xcode={xcode!r}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 B：commit_only / commit_and_push 的 -A 语义
# --------------------------------------------------------------------------
def scenario_b() -> None:
    name = "B. commit_only -A 语义（已跟踪改动 + 未跟踪新文件都要进提交）"
    _reset_work()

    # 已跟踪文件的修改
    write(os.path.join(_work, "output", "seed.md"), "# seed (modified)\n")
    # 未跟踪新文件
    write(os.path.join(_work, "output", "brand_new.md"), "# brand new\n")

    r = lg.commit_only("test B")
    if not check(f"{name} — commit_only 成功（rc=0，不是 add -A 被当路径的 128）",
                 r.get("success") is True, str(r)):
        return

    head = git("rev-parse", "HEAD")[1]
    files = commit_files(head)
    check(f"{name} — 已跟踪文件的改动进了提交", "output/seed.md" in files, f"files={files}")
    check(f"{name} — 未跟踪新文件也进了提交", "output/brand_new.md" in files, f"files={files}")
    check(f"{name} — 工作区已干净", git("status", "--porcelain")[1] == "",
          f"porcelain={git('status', '--porcelain')[1]!r}")

    # commit_and_push 也走同一条 _commit_with_add(["-A"]) 路径 + 推 main
    write(os.path.join(_work, "output", "second.md"), "# second\n")
    r2 = lg.commit_and_push("test B2")
    check(f"{name} — commit_and_push 成功", r2.get("success") is True, str(r2))
    if r2.get("success"):
        head2 = git("rev-parse", "HEAD")[1]
        check(f"{name} — commit_and_push 后裸库 main == 本地 HEAD",
              bare_ref_sha(_bare) == head2, f"remote={bare_ref_sha(_bare)[:8]} head={head2[:8]}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 C：非 main + 工作区干净 -> 自动切回 main
# --------------------------------------------------------------------------
def scenario_c() -> None:
    name = "C. 非 main + 干净 -> ensure_on_main 自动切回 main（特性分支指针不动）"
    _reset_work()

    git("checkout", "-b", "tmp-feature")
    feat_before = git("rev-parse", "tmp-feature")[1]
    before_remote = bare_ref_sha(_bare)

    r = lg.ensure_on_main()
    if not check(f"{name} — ensure_on_main success=True & switched=True",
                 r.get("success") is True and r.get("switched") is True, str(r)):
        _reset_work()
        return

    cur = git("branch", "--show-current")[1]
    check(f"{name} — 当前分支已切到 main", cur == "main", f"branch={cur!r}")
    check(f"{name} — tmp-feature 指针未变", git("rev-parse", "tmp-feature")[1] == feat_before,
          f"before={feat_before[:8]} after={git('rev-parse', 'tmp-feature')[1][:8]}")

    # 随后提交推送，裸库 main 必须前进
    write(os.path.join(_work, "output", "c.md"), "# c\n")
    r2 = lg.commit_data_and_push("test C")
    check(f"{name} — 切回后可正常提交推送", r2.get("success") is True, str(r2))
    check(f"{name} — 裸库 main 前进", bare_ref_sha(_bare) not in ("", before_remote),
          f"before={before_remote[:8]} after={bare_ref_sha(_bare)[:8]}")

    git("checkout", "-f", "main")
    git("branch", "-D", "tmp-feature")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 D：非 main + 已跟踪文件有改动 -> 拒绝
# --------------------------------------------------------------------------
def scenario_d() -> None:
    name = "D. 非 main + 已跟踪文件改动 -> 拒绝（不切换、不提交、远端不动）"
    _reset_work()

    # 让 README.md 在两个分支上内容不同，再在 tmp-dirty 上把它改脏。
    # 未跟踪文件不会阻塞 checkout，所以必须用【已跟踪 + 两分支内容不同】才能构造成
    # "checkout 会被拒" 的局面。
    write(os.path.join(_work, "README.md"), "on main\n")
    git("commit", "-am", "feat: readme on main")
    git("push", "origin", "main")

    git("checkout", "-b", "tmp-dirty")
    write(os.path.join(_work, "README.md"), "on tmp-dirty\n")
    git("commit", "-am", "feat: readme on tmp-dirty")
    # 现在把工作区的 README.md 再改脏（未提交）
    write(os.path.join(_work, "README.md"), "dirty uncommitted\n")

    remote_before = bare_ref_sha(_bare)
    r = lg.ensure_on_main()

    check(f"{name} — ensure_on_main success=False", r.get("success") is False, str(r))
    check(f"{name} — switched=False", r.get("switched") is False, str(r))
    cur = git("branch", "--show-current")[1]
    check(f"{name} — 仍在原分支 tmp-dirty", cur == "tmp-dirty", f"branch={cur!r}")
    check(f"{name} — 裸库 main SHA 未变", bare_ref_sha(_bare) == remote_before,
          f"before={remote_before[:8]} after={bare_ref_sha(_bare)[:8]}")
    msg = str(r.get("message", ""))
    check(f"{name} — message 含脏文件清单(README.md)", "README.md" in msg, f"msg={msg[:200]!r}")
    # 守卫必须在 add 之前：工作区不应凭空多出已暂存内容
    rc, out, _ = git("diff", "--cached", "--name-only")
    check(f"{name} — 没有内容被 add（守卫早于 add）", out == "", f"staged={out!r}")

    git("checkout", "-f", "main")
    git("branch", "-D", "tmp-dirty")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 E：non-fast-forward 恢复（rebase 而非 force）
# --------------------------------------------------------------------------
def scenario_e() -> None:
    name = "E. non-fast-forward -> rebase 后重推成功（历史含双方提交，非 force）"
    _reset_work()

    # 副本先做一个本地提交（尚未推）
    write(os.path.join(_work, "output", "local.md"), "# local side\n")
    git("add", "-A")
    git("commit", "-m", "data: local side commit")

    # 另一个 clone 先推进 origin/main（模拟"别处"）
    other = make_other_clone("other")
    try:
        write(os.path.join(other, "output", "remote.md"), "# remote side\n")
        _run_other = lambda *a: subprocess.run(
            ["git"] + list(a), cwd=other, text=True, capture_output=True,
            timeout=60, stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_AUTHOR_NAME": _ENV_NAME,
                 "GIT_AUTHOR_EMAIL": _ENV_EMAIL,
                 "GIT_COMMITTER_NAME": _ENV_NAME,
                 "GIT_COMMITTER_EMAIL": _ENV_EMAIL})
        _run_other("add", "-A")
        _run_other("commit", "-m", "data: remote side commit")
        rr = _run_other("push", "origin", "main")
        if rr.returncode != 0:
            check(f"{name} — 夹具构造（别处推进 origin）", False, rr.stderr)
            return
    finally:
        shutil.rmtree(other, ignore_errors=True)

    r = lg.commit_data_and_push("test E")
    if not check(f"{name} — commit_data_and_push 成功", r.get("success") is True, str(r)):
        _reset_work()
        return

    subjects = bare_all_commits(_bare)
    check(f"{name} — 裸库历史含副本侧提交", "data: local side commit" in subjects, f"{subjects}")
    check(f"{name} — 裸库历史含别处侧提交", "data: remote side commit" in subjects, f"{subjects}")

    # 两侧的产物文件都还在 main 上 —— 强推会丢掉其中一侧
    files = bare_tree_files(_bare)
    check(f"{name} — main 同时含两侧产物(output/local.md & output/remote.md)",
          "output/local.md" in files and "output/remote.md" in files, f"files={files}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 F：假成功回归（原 bug 的守门人）
# --------------------------------------------------------------------------
def scenario_f() -> None:
    name = "F. 假成功回归：push 返回 0 但远端没动 -> 必须 success=False"
    _reset_work()

    write(os.path.join(_work, "output", "f.md"), "# f\n")
    write(os.path.join(_work, "state.json"), '{"last_run": "f"}\n')

    real_run_git = lg._run_git
    calls = []

    def fake_run_git(args, capture=True, timeout=lg.LOCAL_TIMEOUT):
        calls.append(list(args))
        if args and args[0] == "push":
            # 模拟 HEAD 不在 main 时的真实行为：push 退出码 0，远端纹丝不动
            print(f"    (mock) git {' '.join(args)} -> (0, 'Everything up-to-date', '')")
            return 0, "Everything up-to-date", ""
        return real_run_git(args, capture=capture, timeout=timeout)

    lg._run_git = fake_run_git
    try:
        before = bare_ref_sha(_bare)
        r = lg.commit_data_and_push("test F")
    finally:
        lg._run_git = real_run_git

    check(f"{name} — success=False", r.get("success") is False, str(r))
    check(f"{name} — 本地确实产生了提交（是「推失败」而非「没提交」）",
          bare_ref_sha(_bare) == before and git("rev-parse", "HEAD")[1] != before,
          f"remote={bare_ref_sha(_bare)[:8]} head={git('rev-parse', 'HEAD')[1][:8]}")
    check(f"{name} — message 点明远端未前进",
          "ls-remote" in str(r.get("message", "")) or "远端" in str(r.get("message", "")),
          f"msg={r.get('message')!r}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 G：远端校验失败 -> 保守判失败
# --------------------------------------------------------------------------
def scenario_g() -> None:
    name = "G. ls-remote 失败 -> 不许因「无法确认」而宣称成功"
    _reset_work()

    write(os.path.join(_work, "output", "g.md"), "# g\n")
    real_run_git = lg._run_git

    def fake_run_git(args, capture=True, timeout=lg.LOCAL_TIMEOUT):
        if args and args[0] == "ls-remote":
            return 128, "", "fatal: unable to access remote"
        return real_run_git(args, capture=capture, timeout=timeout)

    lg._run_git = fake_run_git
    try:
        r = lg.commit_data_and_push("test G")
    finally:
        lg._run_git = real_run_git

    check(f"{name} — success=False", r.get("success") is False, str(r))
    check(f"{name} — message 说明无法校验远端",
          "无法校验" in str(r.get("message", "")), f"msg={r.get('message')!r}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 H：静态绊线 —— 不得出现 --force / --force-with-lease
# --------------------------------------------------------------------------
def scenario_h() -> None:
    name = "H. 静态绊线：local_git.py 里 --force / force-with-lease 未被当参数传入"
    path = os.path.join(REPO_ROOT, "tui", "local_git.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    # 词法判断：只看「字符串字面量里，作为 git 参数出现」的 force 开关。
    # 注释/文档里提到 "--force" 属正常说明文字，不能误伤。
    offenders = []
    for m in re.finditer(r"""["']([^"'\n]*)["']""", src):
        lit = m.group(1)
        if lit.strip() in ("--force", "-f", "--force-with-lease"):
            offenders.append(lit)
    check(f"{name} — 没有把 force 开关当参数传入", not offenders,
          f"found={offenders}")

    # 印证判据本身有效：若把说明文字也算进去，源码里确实有 "--force" 字样
    check(f"{name} — 判据自检（源码注释里确实出现该字样，证明不是空匹配）",
          "--force" in src, "源码里连注释都没有该字样（判据可能失效）")


# --------------------------------------------------------------------------
# 场景 I：退出码契约（_finalize_and_push）
# --------------------------------------------------------------------------
def scenario_i() -> None:
    global _commit_result
    name = "I. 退出码契约：成功->0 / 推送失败->2 / 非 CI->0，且顺序为 汇总->save->commit"
    from src import main as m
    from src import notifier as nt

    order = []
    sent_alerts = []

    def fake_summary(state):
        order.append("summary")
        return None

    def fake_save(state):
        order.append("save")

    def fake_commit(msg):
        order.append("commit")
        return _commit_result

    def fake_alert(subject, body, image_path=None):
        sent_alerts.append(subject)
        return True

    _saved = (m._maybe_daily_summary, m.save_state, m.commit_data_and_push)
    _alert_saved = getattr(nt, "send_alert", None)
    try:
        m._maybe_daily_summary = fake_summary
        m.save_state = fake_save
        m.commit_data_and_push = fake_commit
        nt.send_alert = fake_alert

        # i-1 成功 -> 0
        _commit_result = {"success": True, "message": "ok"}
        order.clear()
        rc_ok = m._finalize_and_push({}, True, "msg")
        check(f"{name} — 推送成功返回 0", rc_ok == 0, f"rc={rc_ok}")
        check(f"{name} — 顺序为 汇总->save->commit", order == ["summary", "save", "commit"],
              f"order={order}")

        # i-2 推送失败 -> 2，且发告警
        _commit_result = {"success": False, "message": "boom"}
        sent_alerts.clear()
        rc_fail = m._finalize_and_push({}, True, "msg")
        check(f"{name} — 推送失败返回 2", rc_fail == 2, f"rc={rc_fail}")
        check(f"{name} — 推送失败时发了告警邮件", len(sent_alerts) == 1,
              f"alerts={sent_alerts}")

        # i-3 非 CI -> 0（不推送）
        order.clear()
        rc_local = m._finalize_and_push({}, False, "msg")
        check(f"{name} — 非 CI 返回 0", rc_local == 0, f"rc={rc_local}")
        check(f"{name} — 非 CI 不调用 commit", "commit" not in order, f"order={order}")

        # i-4 告警发送抛异常也不影响退出码（邮件只是辅助）
        def boom_alert(*a, **k):
            raise RuntimeError("smtp down")
        nt.send_alert = boom_alert
        _commit_result = {"success": False, "message": "boom"}
        rc_boom = m._finalize_and_push({}, True, "msg")
        check(f"{name} — 告警异常时仍返回 2", rc_boom == 2, f"rc={rc_boom}")
    finally:
        (m._maybe_daily_summary, m.save_state, m.commit_data_and_push) = _saved
        if _alert_saved is not None:
            nt.send_alert = _alert_saved

    # 静态：main.py 必须把 main() 的退出码传出去
    with open(os.path.join(REPO_ROOT, "src", "main.py"), "r", encoding="utf-8") as f:
        main_src = f.read()
    check(f"{name} — main.py 存在 sys.exit(main())",
          re.search(r"sys\.exit\(\s*main\(\)\s*\)", main_src) is not None,
          "未找到 sys.exit(main())")
    check(f"{name} — main() 声明了 int 返回类型",
          re.search(r"def\s+main\(\)\s*->\s*int\s*:", main_src) is not None,
          "main() 没有 -> int")


# --------------------------------------------------------------------------
# 场景 J：汇总确实赶上当次提交（plan 验证 #7）
# --------------------------------------------------------------------------
def scenario_j() -> None:
    name = "J. 汇总赶上当次 data: 提交（output/summary/<today>.md 出现在裸库 main）"
    from src import main as m

    _reset_work()
    today = datetime.now().strftime("%Y-%m-%d")
    rel = f"output/summary/{today}.md"

    saved = m._maybe_daily_summary
    saved_state_path = m.STATE_PATH

    def fake_summary(state):
        write(os.path.join(_work, rel), f"# summary {today}\n")
        return os.path.join(_work, rel)

    m._maybe_daily_summary = fake_summary
    # STATE_PATH 也必须重定向进夹具: _finalize_and_push 会调**真实**的 save_state,
    # 而它写的是 m.STATE_PATH。只 patch _maybe_daily_summary 而漏掉这一条, 真实
    # state.json 就会被那份空 state 覆盖成 `{}` —— 这正是本仓库发生过的事故
    # (202251 字节 -> 2 字节), 而且当时套件仍然报 ALL PASS。
    m.STATE_PATH = os.path.join(_work, "state.json")
    try:
        rc = m._finalize_and_push({}, True, "test J")
    finally:
        m._maybe_daily_summary = saved
        m.STATE_PATH = saved_state_path

    check(f"{name} — _finalize_and_push 返回 0", rc == 0, f"rc={rc}")
    files = bare_tree_files(_bare)
    check(f"{name} — 裸库 main 的树包含 {rel}", rel in files, f"files={files}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 K（附加，计划外）：non-ff 但 rebase 冲突 -> 回滚干净 + 返回失败
# --------------------------------------------------------------------------
def scenario_k() -> None:
    name = "K. rebase 冲突 -> 已 rebase --abort（不留半截 rebase）且 success=False"
    _reset_work()

    # 本地与别处都改同一行，制造冲突
    write(os.path.join(_work, "output", "shared.md"), "local version\n")
    git("add", "-A")
    git("commit", "-m", "data: local shared")

    other = make_other_clone("conflict")
    try:
        write(os.path.join(other, "output", "shared.md"), "remote version\n")

        def _o(*a):
            return subprocess.run(["git"] + list(a), cwd=other, text=True,
                                  capture_output=True, timeout=60,
                                  stdin=subprocess.DEVNULL,
                                  env={**os.environ, "GIT_AUTHOR_NAME": _ENV_NAME,
                                       "GIT_AUTHOR_EMAIL": _ENV_EMAIL,
                                       "GIT_COMMITTER_NAME": _ENV_NAME,
                                       "GIT_COMMITTER_EMAIL": _ENV_EMAIL})
        _o("add", "-A")
        _o("commit", "-m", "data: remote shared")
        rr = _o("push", "origin", "main")
        if rr.returncode != 0:
            check(f"{name} — 夹具构造（别处推进 origin）", False, rr.stderr)
            return
    finally:
        shutil.rmtree(other, ignore_errors=True)

    r = lg.commit_data_and_push("test K")
    check(f"{name} — success=False（冲突不该被当成成功）", r.get("success") is False, str(r))
    # 半截 rebase 状态会让整个仓库不可用 —— 必须已 abort
    rebase_dir = os.path.join(_work, ".git", "rebase-merge")
    rebase_dir2 = os.path.join(_work, ".git", "rebase-apply")
    check(f"{name} — 没有残留 .git/rebase-merge",
          not os.path.exists(rebase_dir), f"exists={rebase_dir}")
    check(f"{name} — 没有残留 .git/rebase-apply",
          not os.path.exists(rebase_dir2), f"exists={rebase_dir2}")
    _reset_work()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 场景 L：state.json 缺失时 output/ 仍要推得上去
# --------------------------------------------------------------------------
def scenario_l() -> None:
    name = "L. state.json 缺失 -> output/ 仍能推上去（不被不存在的 pathspec 连累）"
    _reset_work()

    # 删掉 state.json（真实事故：新 clone / 首次运行 / 被前一轮删过都会缺它），
    # 只产出报告
    os.remove(os.path.join(_work, "state.json"))
    write(os.path.join(_work, "output", "new-report.md"), "# new report\n")

    before = bare_ref_sha(_bare)
    r = lg.commit_data_and_push("test L")

    if not check(f"{name} — success 为真", r.get("success") is True, str(r)):
        _reset_work()
        return

    head = git("rev-parse", "HEAD")[1]
    check(f"{name} — 裸库 main == 本地 HEAD", bare_ref_sha(_bare) == head,
          f"remote={bare_ref_sha(_bare)[:8]} head={head[:8]}")
    check(f"{name} — 裸库 main 确实前进", bare_ref_sha(_bare) != before,
          f"before={before[:8]} after={bare_ref_sha(_bare)[:8]}")

    files = commit_files(head)
    check(f"{name} — 本次提交只含 output/new-report.md", files == ["output/new-report.md"],
          f"files={files}")
    check(f"{name} — 没有伪造出 state.json（不在提交里，也不在工作区）",
          "state.json" not in files
          and not os.path.exists(os.path.join(_work, "state.json")),
          f"files={files} exists={os.path.exists(os.path.join(_work, 'state.json'))}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 M：两条路径都不存在 -> 必须判失败
# --------------------------------------------------------------------------
def scenario_m() -> None:
    name = "M. output/ 与 state.json 都不存在 -> 判失败（不静默当成功）"
    _reset_work()

    shutil.rmtree(os.path.join(_work, "output"), ignore_errors=True)
    os.remove(os.path.join(_work, "state.json"))

    before = bare_ref_sha(_bare)
    r = lg.commit_data_and_push("test M")

    check(f"{name} — success 必须为 False", r.get("success") is False, str(r))
    check(f"{name} — 裸库 main SHA 未变", bare_ref_sha(_bare) == before,
          f"before={before[:8]} after={bare_ref_sha(_bare)[:8]}")
    _reset_work()


# --------------------------------------------------------------------------
# 场景 N：save_state 原子写
# --------------------------------------------------------------------------
def scenario_n() -> None:
    name = "N. save_state 原子写（先 .tmp 再换名；不追加、不留 .tmp）"
    from src import main as m

    # 重定向 STATE_PATH 到临时目录 —— 绝不碰真实 state.json
    tmp_dir = os.path.join(_TMP_ROOT, "state-redirect")
    os.makedirs(tmp_dir, exist_ok=True)
    fake_state = os.path.join(tmp_dir, "state.json")
    _assert_under_tmp(fake_state, "重定向的 STATE_PATH")
    # 再确认一次：与真实仓库里的 state.json 不是同一个文件
    real_state = os.path.realpath(os.path.join(REPO_ROOT, "state.json"))
    if os.path.realpath(fake_state) == real_state:
        raise SystemExit("拒绝运行: 重定向后的 STATE_PATH 指向真实 state.json")

    old_state_path = m.STATE_PATH
    m.STATE_PATH = fake_state
    try:
        # (1) 500 个元素的 processed_links 回读必须完整相等
        links = [f"https://example.invalid/a/{i}" for i in range(500)]
        m.save_state({"processed_links": links, "gen": 1})
        with open(fake_state, "r", encoding="utf-8") as f:
            back = json.load(f)
        check(f"{name} — 500 条 processed_links 回读完整相等",
              back.get("processed_links") == links and len(back["processed_links"]) == 500,
              f"len={len(back.get('processed_links', []))}")

        # (2) 写完 .tmp 不残留
        check(f"{name} — 写完无 .tmp 残留",
              not os.path.exists(fake_state + ".tmp"),
              f"exists={os.path.exists(fake_state + '.tmp')}")

        # (3) 覆盖写第二次后，回读不含第一次的内容（证明是换名而非追加）
        m.save_state({"processed_links": ["only-second"], "gen": 2})
        with open(fake_state, "r", encoding="utf-8") as f:
            back2 = json.load(f)
        check(f"{name} — 第二次写覆盖了第一次（非追加）",
              back2.get("processed_links") == ["only-second"] and back2.get("gen") == 2,
              f"back2={back2}")
        raw = open(fake_state, "r", encoding="utf-8").read()
        check(f"{name} — 旧内容未残留在文件里", "example.invalid" not in raw,
              f"size={len(raw)}")

        # (4) 仍无 .tmp 残留
        check(f"{name} — 二次写后仍无 .tmp 残留",
              not os.path.exists(fake_state + ".tmp"),
              f"exists={os.path.exists(fake_state + '.tmp')}")
    finally:
        m.STATE_PATH = old_state_path


# --------------------------------------------------------------------------
# 场景 O：提交信息用窗口日而非日历日
# --------------------------------------------------------------------------
def scenario_o() -> None:
    name = "O. 提交信息用窗口日 today_window 而非日历日 beijing_now"
    with open(os.path.join(REPO_ROOT, "src", "main.py"), "r", encoding="utf-8") as f:
        src = f.read()

    window_msg = re.findall(r'f"daily report \{today_window\}"', src)
    check(f"{name} — f\"daily report {{today_window}}\" 恰好出现 3 次",
          len(window_msg) == 3, f"count={len(window_msg)}")

    # 日历日写法必须一处都没有（北京 00:00-04:00 时 today_window != 日历日，
    # 用日历日会把上一个窗口遗留的产物标成新的一天）
    check(f"{name} — 不存在 daily report {{beijing_now...}} 的日历日写法",
          not re.search(r'daily report \{beijing_now', src),
          "发现日历日写法")

    # 变异自检式说明：确认 _daily_window_date 与日历日确实是两码事
    # （00:00-04:00 时 _daily_window_date() 返回前一天，日历日写法会错标）
    variant = src.replace('f"daily report {today_window}"',
                          'f"daily report {beijing_now().strftime(\'%Y-%m-%d\')}"')
    variant_hits = len(re.findall(r'daily report \{beijing_now', variant))
    check(f"{name} — 判据自检（把窗口日改回日历日则能被检出）",
          variant_hits == 3,
          f"variant_hits={variant_hits}（应为 3，否则正则失效）")

    # 行为验证：把"现在"钉在北京时间 02:00，窗口日必须回退到前一天，
    # 且与日历日不同 —— 这正是"用错日期会标错天"的实证。
    from src import main as m
    real_beijing_now = m.beijing_now
    try:
        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 16, 2, 0, 0, tzinfo=tz)
        m.beijing_now = lambda: _FakeDatetime(2026, 9, 16, 2, 0, 0, tzinfo=_BEIJING_TZ)
        window_day = m._daily_window_date()
        calendar_day = m.beijing_now().strftime("%Y-%m-%d")
    finally:
        m.beijing_now = real_beijing_now

    check(f"{name} — 北京 02:00 时窗口日=09-15 而日历日=09-16（两者确实不同）",
          window_day == "2026-09-15" and calendar_day == "2026-09-16",
          f"window={window_day} calendar={calendar_day}")


def _assert_under_tmp(path: str, what: str) -> str:
    """安全闸：给定路径必须落在临时根目录内，否则抛 SystemExit。

    用于任何"把模块级全局重定向到临时文件"的场景（如 src.main.STATE_PATH）——
    真实 state.json 绝不能被测试碰到。
    """
    real = os.path.realpath(path).replace("\\", "/").rstrip("/")
    tmp = os.path.realpath(_TMP_ROOT or "").replace("\\", "/").rstrip("/")
    if not tmp or not real.startswith(tmp + "/"):
        raise SystemExit(
            f"拒绝运行: {what}({real}) 不在临时目录({tmp}) 内")
    return real


# --------------------------------------------------------------------------
# 真实仓库数据护栏
#
# 缘起: 本仓库已两次出现「测试把真实 state.json 清空」的事故 —— 202251 字节、
# 3216 条 processed_links 只剩一个 `{}`, 而两次测试套件都报 **ALL PASS**:
# 绿的自检完全没察觉自己顺手毁了被测对象。
#   第一次: 为了模拟 _finalize_and_push, import 了 src.main 却没 patch
#           commit_data_and_push, 结果真的 commit + push 到真实远端。
#   第二次: 场景 J patch 了 _maybe_daily_summary, 却漏了 save_state ——
#           _finalize_and_push({}) 里的真实 save_state 把 `{}` 写进真实仓库。
# 结论: 靠"每个场景自觉重定向"是不可靠的。必须有一道全局闸门, 让"写真实数据
# 文件"这件事直接抛异常。所以下面在 main() 一开始就装上它。
# --------------------------------------------------------------------------
_REAL_STATE = os.path.realpath(os.path.join(REPO_ROOT, "state.json"))
_REAL_OUTPUT = os.path.realpath(os.path.join(REPO_ROOT, "output")) + os.sep


def _is_real_repo_data(path) -> bool:
    """该路径是否指向真实仓库的数据文件(state.json 及其 .tmp / output/ 下)."""
    try:
        real = os.path.realpath(os.fspath(path))
    except (TypeError, ValueError):
        return False
    return (real == _REAL_STATE or real.startswith(_REAL_STATE + ".")
            or real.startswith(_REAL_OUTPUT))


def real_state_fingerprint() -> str:
    """真实仓库 state.json 的原始字节指纹(绕开 checkout 的行尾转换)."""
    return git("-C", REPO_ROOT, "hash-object", "state.json", cwd=REPO_ROOT)[1].strip()


def install_real_repo_write_guard() -> None:
    """把对真实仓库数据文件的写入/删除/覆盖变成异常, 而不是静默得逞."""
    import builtins

    real_open, real_remove = builtins.open, os.remove
    real_replace, real_rename = os.replace, os.rename

    def guarded_open(file, mode="r", *a, **kw):
        if any(c in str(mode) for c in "wax+") and _is_real_repo_data(file):
            raise RuntimeError(f"护栏拦下对真实仓库数据文件的写入: {file}")
        return real_open(file, mode, *a, **kw)

    def guarded_remove(path, *a, **kw):
        if _is_real_repo_data(path):
            raise RuntimeError(f"护栏拦下对真实仓库数据文件的删除: {path}")
        return real_remove(path, *a, **kw)

    def guarded_replace(src, dst, *a, **kw):
        if _is_real_repo_data(dst):
            raise RuntimeError(f"护栏拦下对真实仓库数据文件的覆盖: {dst}")
        return real_replace(src, dst, *a, **kw)

    def guarded_rename(src, dst, *a, **kw):
        if _is_real_repo_data(dst):
            raise RuntimeError(f"护栏拦下对真实仓库数据文件的重命名: {dst}")
        return real_rename(src, dst, *a, **kw)

    builtins.open = guarded_open
    os.remove = guarded_remove
    os.replace = guarded_replace
    os.rename = guarded_rename


def scenario_p() -> None:
    """护栏自检: 证明它真的会拦, 而不是一段永远不触发的摆设.

    自检本身绝不能成为事故源: 第二步把护栏的目标临时挪到"金丝雀"文件上再触发,
    这样万一护栏失效, 被毁的是金丝雀而不是真实数据。
    """
    global _REAL_STATE
    name = "P. 真实仓库写入护栏自检"
    fingerprint_before = real_state_fingerprint()

    # 第一步(真实绑定): 判据必须认得真实路径, 也必须放过临时路径
    probe = os.path.join(_TMP_ROOT, "guard-probe.txt")
    judge_ok = (_is_real_repo_data(_REAL_STATE)
                and _is_real_repo_data(_REAL_STATE + ".tmp")
                and _is_real_repo_data(os.path.join(REPO_ROOT, "output", "x.md"))
                and not _is_real_repo_data(probe)
                and not _is_real_repo_data(_TMP_ROOT))
    check(f"{name} — 判据认得真实路径 / 放过临时路径", judge_ok)

    # 第二步(金丝雀绑定): 四类写入都必须被拦下
    canary = os.path.realpath(os.path.join(_TMP_ROOT, "canary-state.json"))
    real_binding = _REAL_STATE
    blocked = []
    try:
        _REAL_STATE = canary
        for label, fn in (
            ("open(w)", lambda: open(canary, "w").close()),
            ("os.remove", lambda: os.remove(canary)),
            ("os.replace", lambda: os.replace(probe, canary)),
            ("os.rename", lambda: os.rename(probe, canary)),
        ):
            try:
                fn()
                blocked.append(f"{label}:未拦")
            except RuntimeError:
                blocked.append(f"{label}:拦下")
            except Exception as e:
                blocked.append(f"{label}:{type(e).__name__}")
    finally:
        _REAL_STATE = real_binding
    check(f"{name} — 四类写入全部被拦下", all(b.endswith(":拦下") for b in blocked),
          f"{blocked}")

    # 第三步: 临时目录的正常写入不受影响(护栏不能误伤夹具)
    ok = False
    try:
        write(probe, "ok")
        with open(probe, encoding="utf-8") as f:
            ok = f.read() == "ok"
        os.remove(probe)
    except Exception as e:
        print(f"  (护栏误伤临时路径: {e})")
    check(f"{name} — 临时目录写入不受影响", ok)

    # 第四步: 走完这一圈, 真实 state.json 必须一个字节都没变
    check(f"{name} — 真实 state.json 指纹未变",
          real_state_fingerprint() == fingerprint_before,
          f"{fingerprint_before} -> {real_state_fingerprint()}")


def main() -> int:
    global _TMP_ROOT, _bare, _work, _commit_result
    _commit_result = {"success": True, "message": "ok"}

    # 第一件事就是装上护栏: 任何场景(含将来新加的)想写真实 state.json / output/
    # 都会当场抛异常。放在最前面, 免得后面哪个 import 或准备动作先动到手。
    install_real_repo_write_guard()
    state_fingerprint_before = real_state_fingerprint()

    print("=" * 72)
    print("回归测试：自动流程「提交 + 推送」不再静默假成功")
    print("=" * 72)

    print(f"真实仓库 : {REPO_ROOT}")
    print(f"local_git: {lg.__file__}")
    print(f"当前分支 : {git('-C', REPO_ROOT, 'branch', '--show-current', cwd=REPO_ROOT)[1]}")

    _TMP_ROOT = tempfile.mkdtemp(prefix="tju-gitflow-test-")
    print(f"临时根目录: {_TMP_ROOT}")
    print("-" * 72)

    old_root = lg.PROJECT_ROOT
    try:
        _bare, _work = seed_and_clone(_TMP_ROOT)
        lg.PROJECT_ROOT = _work

        # 安全闸 1：全局指向临时目录（系统级异常也走 finally，保证 /tmp 被清）
        if os.path.realpath(lg.PROJECT_ROOT) == os.path.realpath(REPO_ROOT):
            print("[FATAL] PROJECT_ROOT 指向真实仓库，拒绝运行")
            return 1
        _require_temp_root()

        # 安全闸 2：origin 必须是临时裸库，且绝无可能指向真实 origin
        rc, out, _ = git("remote", "get-url", "origin")
        _o = os.path.realpath(out).replace("\\", "/") if out else ""
        _t = os.path.realpath(_TMP_ROOT).replace("\\", "/").rstrip("/")
        if not _o or not _o.startswith(_t + "/"):
            print(f"[FATAL] origin 指向 {out!r}，不是临时裸库，拒绝运行")
            return 1
        print(f"origin   : {out}")
        print(f"work copy: {_work}")
        print("-" * 72)

        scenario_a()
        scenario_b()
        scenario_c()
        scenario_d()
        scenario_e()
        scenario_f()
        scenario_g()
        scenario_h()
        scenario_i()
        scenario_j()
        scenario_k()
        scenario_l()
        scenario_m()
        scenario_n()
        scenario_o()
        scenario_p()
    except Exception as e:
        import traceback
        traceback.print_exc()
        record(False, "运行期未捕获异常", f"{type(e).__name__}: {e}")
    finally:
        # 先还原全局，再删临时目录（顺序不能反：真实仓库的 PROJECT_ROOT 必须回来）
        lg.PROJECT_ROOT = old_root
        _force_rmtree(_TMP_ROOT)

    # 结束不变量: 跑完整套之后, 真实仓库的 state.json 必须与开跑前**逐字节相同**。
    # 这条和场景 P 分工不同 —— P 证明护栏本身有效, 这条证明所有场景都没触发过它。
    # 本仓库两次数据事故(202251 字节 -> 2 字节)都是整套报 ALL PASS 时发生的:
    # 绿的自检抓不到"测试把自己被测的数据改了", 只有这个跨全场的比对能抓到。
    try:
        after = real_state_fingerprint()
        check("Q. 结束不变量: 真实 state.json 全程未被改动",
              after == state_fingerprint_before,
              f"{state_fingerprint_before} -> {after}")
    except Exception as e:
        check("Q. 结束不变量: 真实 state.json 全程未被改动", False, f"无法取证: {e}")

    print("=" * 72)
    n_fail = sum(1 for ok, _, _ in _RESULTS if not ok)
    n_pass = sum(1 for ok, _, _ in _RESULTS if ok)
    if n_fail:
        print(f"FAILED: {n_fail}  (通过 {n_pass} / 共 {len(_RESULTS)})")
        for ok, nm, det in _RESULTS:
            if not ok:
                print(f"  - [FAIL] {nm}: {det}")
        return 1
    print(f"ALL PASS  ({n_pass} 项断言)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
