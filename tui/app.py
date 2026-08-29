"""Textual TUI 主界面 — 天津大学每日智能信息简报系统。

布局：
- 左侧: RichLog 对话区
- 右侧: ListView 文件列表
- 底部: 四个 Git 控制按钮
"""

import os
import sys

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListView,
    ListItem,
    RichLog,
    Static,
)
from textual.reactive import reactive

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tui.agent import Agent
from tui.local_git import commit_and_push, commit_only, get_output_files
from tui.tools import list_outputs

# 命令白名单
COMMAND_WHITELIST = {"/load_history", "/save", "/quit"}


class TjuTuiApp(App):
    """天津大学智能信息简报 TUI."""

    CSS = """
    Horizontal {
        height: 1fr;
    }

    #chat-area {
        width: 2fr;
        border: solid $primary;
        margin: 1;
    }

    #file-area {
        width: 1fr;
        border: solid $secondary;
        margin: 1;
    }

    #file-list {
        height: 1fr;
    }

    #button-bar {
        height: 3;
        align: center middle;
    }

    #button-bar Button {
        margin: 0 1;
        min-width: 16;
    }

    #input-area {
        height: 3;
        margin: 1;
    }

    #status-bar {
        height: 1;
        dock: bottom;
        background: $panel;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="chat-area"):
                yield RichLog(id="chat-log", highlight=True, markup=True)
                yield Input(id="chat-input", placeholder="输入消息或命令...")
            with Vertical(id="file-area"):
                yield Static("📁 报告列表", id="file-title")
                yield ListView(id="file-list")
        with Horizontal(id="button-bar"):
            yield Button("仅Commit", id="btn-commit-only", variant="default")
            yield Button("Commit & Push", id="btn-commit-push", variant="primary")
            yield Button("仅Save", id="btn-save", variant="default")
            yield Button("退出", id="btn-quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        """启动时初始化."""
        self.title = "TJU Daily Bot"
        self.sub_title = "天津大学每日智能信息简报"

        self.agent = Agent()
        self.agent.load_history()  # 静默加载

        # 启动检测
        self._startup_check()

        # 刷新文件列表
        self._refresh_file_list()
        self.set_interval(30, self._refresh_file_list)

        # 欢迎消息
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write("[bold green]📰 天津大学每日智能信息简报系统[/bold green]")
        chat_log.write("输入消息与 Agent 对话，或使用命令：")
        chat_log.write("  [bold]/load_history[/bold] - 加载上次对话")
        chat_log.write("  [bold]/save[/bold] - 保存当前对话")
        chat_log.write("  [bold]/quit[/bold] - 保存并退出")
        chat_log.write("")

    def _startup_check(self) -> None:
        """启动环境检查."""
        chat_log = self.query_one("#chat-log", RichLog)

        # 检查 .env 文件
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if not os.path.exists(env_path):
            chat_log.write("[bold yellow]⚠️ 未检测到 .env 文件[/bold yellow]")
            chat_log.write("  请执行: cp .env.example .env 并填入 API 密钥")

        # 提示公众号数据来源：本地不跑 we-mp-rss，公众号内容来自云端 Actions 生成的历史报告
        output_dir = os.path.join(os.path.dirname(__file__), "..", "output")
        repo_has_reports = os.path.isdir(output_dir) and any(
            f.endswith(".md") for f in os.listdir(output_dir)
        )
        if not repo_has_reports:
            chat_log.write("[yellow]📭 本地暂无历史报告[/yellow]")
            chat_log.write("  公众号内容由 GitHub Actions 定时生成并 push（output/ 下 .md）。")
            chat_log.write("  使用「搜索」前建议先 git pull 同步最新历史报告。")

        # 检查历史文件
        history_dir = os.path.join(os.path.dirname(__file__), "..", "history")
        if os.path.isdir(history_dir):
            files = [f for f in os.listdir(history_dir) if f.startswith("conversation_")]
            if files:
                chat_log.write(f"[dim]📝 检测到 {len(files)} 个历史对话，输入 /load_history 加载最近对话[/dim]")

    def _refresh_file_list(self) -> None:
        """刷新右侧文件列表."""
        file_list = self.query_one("#file-list", ListView)
        file_list.clear()
        files = list_outputs()
        for f in files.get("files", []):
            file_list.append(ListItem(Static(f)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """按钮事件处理."""
        chat_log = self.query_one("#chat-log", RichLog)
        button_id = event.button.id

        if button_id == "btn-commit-only":
            result = commit_only("save session")
            chat_log.write(f"[dim]📦 {result['message']}[/dim]")

        elif button_id == "btn-commit-push":
            result = commit_and_push("save session")
            chat_log.write(f"[dim]📤 {result['message']}[/dim]")

        elif button_id == "btn-save":
            filename = self.agent.save_history()
            if filename:
                chat_log.write(f"[dim]💾 对话已保存: {filename}[/dim]")
            else:
                chat_log.write("[dim]💾 保存失败[/dim]")

        elif button_id == "btn-quit":
            self.agent.save_history()
            result = commit_and_push("session end")
            chat_log.write(f"[dim]📤 {result['message']}[/dim]")
            self.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """消息输入处理."""
        user_input = event.value.strip()
        if not user_input:
            return

        event.input.value = ""
        chat_log = self.query_one("#chat-log", RichLog)

        # 命令白名单检测
        if user_input in COMMAND_WHITELIST:
            self._handle_command(user_input, chat_log)
            return

        # 普通对话
        chat_log.write(f"[bold blue]你:[/bold blue] {user_input}")
        try:
            reply = self.agent.chat(user_input)
        except Exception as e:
            reply = f"❌ 错误: {e}"
        chat_log.write(f"[bold green]助手:[/bold green] {reply}")
        chat_log.write("")
        self._refresh_file_list()

    def _handle_command(self, command: str, chat_log: RichLog) -> None:
        """处理白名单命令."""
        if command == "/load_history":
            data = self.agent.load_history()
            if data:
                ts = data.get("timestamp", "未知")
                msg_count = len(data.get("messages", []))
                chat_log.write(f"[dim]📂 已加载历史对话 ({ts}, {msg_count} 条消息)[/dim]")
            else:
                chat_log.write("[dim]📂 没有可加载的历史对话[/dim]")

        elif command == "/save":
            filename = self.agent.save_history()
            if filename:
                chat_log.write(f"[dim]💾 对话已保存: {filename}[/dim]")
            else:
                chat_log.write("[dim]💾 保存失败[/dim]")

        elif command == "/quit":
            self.agent.save_history()
            result = commit_and_push("session end")
            chat_log.write(f"[dim]📤 {result['message']}[/dim]")
            self.exit()


def main():
    """TUI 入口."""
    app = TjuTuiApp()
    app.run()


if __name__ == "__main__":
    main()