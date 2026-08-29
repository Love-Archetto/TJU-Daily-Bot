"""Agent 引擎 — 处理工具调用、降级、JSON 纠错、对话历史。

实现：
- 加载 config/tui_agent.yaml
- prefer_function_calling=true: 携带 tools 参数
- API 报错 tools 不支持: 自动移除 tools 并重试
- JSON 纠错: json.loads → json_repair → 正则提取
- 对话历史保存与轮转
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import yaml
from openai import OpenAI

from .tools import (
    append_keyword,
    get_tju_wiki_response,
    git_commit_only,
    git_commit_push,
    list_outputs,
    open_report,
    read_file,
    search,
    update_profile,
    write_file,
)

# 加载 .env（本地运行时读取 TUI 模型 Key）
from dotenv import load_dotenv
PROJECT_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(PROJECT_ROOT_DIR, ".env"))

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "tui_agent.yaml")

# 工具函数映射
TOOL_MAP = {
    "read_file": read_file,
    "write_file": write_file,
    "append_keyword": append_keyword,
    "update_profile": update_profile,
    "list_outputs": list_outputs,
    "open_report": open_report,
    "search": search,
    "tju_wiki_query": get_tju_wiki_response,
    "git_commit_only": git_commit_only,
    "git_commit_push": git_commit_push,
}

# 常见意图预判（跳过 AI 的高频操作）
INTENT_PATTERNS = {
    r"添加关键词[:：\s]*(\S+)": ("append_keyword", "word"),
    r"搜索[:：\s]*(.+)": ("search", "query"),
    r"查看报告[:：\s]*(.+)": ("open_report", "filename"),
    r"列出报告": ("list_outputs", None),
}


class Agent:
    """TUI Agent 引擎."""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or CONFIG_PATH
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        self.prefer_fc = self.config.get("prefer_function_calling", True)
        self.model_cfg = self.config.get("model", {})
        self.system_prompt = self.config.get("system_prompt", "")
        self.temperature = self.config.get("temperature", 0.3)
        self.max_tokens = self.config.get("max_tokens", 800)
        self.max_history_turns = self.config.get("max_history_turns", 10)
        self.history_dir = os.path.join(PROJECT_ROOT, self.config.get("history_dir", "history"))
        self.max_history_files = self.config.get("max_history_files", 30)
        self.tools_schema = self.config.get("tools_schema", [])

        self.messages: list[dict[str, Any]] = []
        self._init_client()

    def _init_client(self) -> None:
        """初始化 OpenAI 客户端."""
        api_key = os.environ.get(self.model_cfg.get("api_key_env", ""), "")
        self.api_base = self.model_cfg.get("api_base", "")
        self.model_name = self.model_cfg.get("model_name", "")
        self.client = OpenAI(api_key=api_key, base_url=self.api_base) if api_key else None

    def _try_parse_json(self, text: str) -> dict[str, Any] | None:
        """三层 JSON 解析：json.loads → json_repair → 正则提取."""
        # 1. 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. json_repair 修复
        try:
            from json_repair import repair_json
            repaired = repair_json(text)
            return json.loads(repaired)
        except Exception:
            pass

        # 3. 正则提取
        try:
            match = re.search(r'\{"tool"\s*:\s*"(\w+)"\s*,\s*"args"\s*:\s*(\{[^}]+\})\}', text)
            if match:
                tool_name = match.group(1)
                args = json.loads(match.group(2))
                return {"tool": tool_name, "args": args}
        except Exception:
            pass

        return None

    def _execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """执行工具调用."""
        func = TOOL_MAP.get(tool_name)
        if func is None:
            return {"success": False, "message": f"Unknown tool: {tool_name}"}

        try:
            if args is None:
                args = {}
            return func(**args)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _precheck_intent(self, user_input: str) -> dict[str, Any] | None:
        """常见意图预判，跳过 AI 解析."""
        for pattern, (tool_name, arg_key) in INTENT_PATTERNS.items():
            match = re.search(pattern, user_input)
            if match:
                if arg_key:
                    return {"tool": tool_name, "args": {arg_key: match.group(1)}}
                else:
                    return {"tool": tool_name, "args": {}}
        return None

    def _call_model(self, retry_count: int = 0) -> dict[str, Any] | None:
        """调用模型，支持 Function Calling 降级和最多 2 次重试."""
        if self.client is None:
            return None

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model_name,
                    "messages": self.messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }

                if self.prefer_fc and self.tools_schema:
                    try:
                        kwargs["tools"] = self.tools_schema
                        kwargs["tool_choice"] = "auto"
                        resp = self.client.chat.completions.create(**kwargs)
                        return resp
                    except Exception as e:
                        if "tools" in str(e).lower() or "tool" in str(e).lower():
                            logger.info("Model does not support tools, retrying without")
                            kwargs.pop("tools", None)
                            kwargs.pop("tool_choice", None)
                            self.prefer_fc = False  # 永久降级
                        else:
                            raise

                resp = self.client.chat.completions.create(**kwargs)
                return resp

            except Exception as e:
                logger.warning("Model call attempt %d failed: %s", attempt + 1, e)
                if attempt < max_retries:
                    self.messages.append({
                        "role": "system",
                        "content": f"上次调用失败: {e}。请重试。",
                    })
                else:
                    return None

        return None

    def chat(self, user_input: str) -> str:
        """处理用户输入，返回助手回复.

        Args:
            user_input: 用户消息

        Returns:
            助手回复文本
        """
        # 1. 意图预判
        precheck = self._precheck_intent(user_input)
        if precheck:
            result = self._execute_tool(precheck["tool"], precheck.get("args", {}))
            self.messages.append({"role": "user", "content": user_input})
            self.messages.append({
                "role": "assistant",
                "content": json.dumps(result, ensure_ascii=False),
            })
            return result.get("message", str(result))

        # 2. 添加用户消息
        self.messages.append({"role": "user", "content": user_input})

        # 3. 裁剪历史轮数
        if len(self.messages) > self.max_history_turns * 2:
            self.messages = self.messages[-(self.max_history_turns * 2):]

        # 4. 调用模型
        resp = self._call_model()
        if resp is None:
            return "抱歉，所有模型调用均失败，请检查 API 配置和网络连接。"

        choice = resp.choices[0]

        # 5. 处理 Function Calling 响应
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result = self._execute_tool(tool_name, args)
                self.messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            # 获取最终回复
            final_resp = self._call_model()
            if final_resp:
                reply = final_resp.choices[0].message.content or "操作完成。"
            else:
                reply = "工具已执行，但无法获取后续回复。"
        else:
            reply = choice.message.content or ""

        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def load_history(self) -> dict[str, Any] | None:
        """加载最近一次对话历史.

        Returns:
            历史数据字典，或 None
        """
        if not os.path.isdir(self.history_dir):
            return None

        files = sorted(
            [f for f in os.listdir(self.history_dir) if f.startswith("conversation_")],
            reverse=True,
        )
        if not files:
            return None

        latest = os.path.join(self.history_dir, files[0])
        try:
            with open(latest, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.messages = data.get("messages", [])
            return data
        except Exception as e:
            logger.error("Failed to load history: %s", e)
            return None

    def save_history(self) -> str | None:
        """保存当前对话到 history/ 目录，并轮转清理.

        Returns:
            保存的文件名，或 None
        """
        os.makedirs(self.history_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"conversation_{timestamp}.json"
        filepath = os.path.join(self.history_dir, filename)

        data = {
            "timestamp": datetime.now().isoformat(),
            "model": self.model_name,
            "messages": self.messages,
        }

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save history: %s", e)
            return None

        # 轮转清理
        self._rotate_history()
        return filename

    def _rotate_history(self) -> None:
        """删除最旧的历史文件，保留最近 max_history_files 个."""
        files = sorted(
            [f for f in os.listdir(self.history_dir) if f.startswith("conversation_")],
        )
        while len(files) > self.max_history_files:
            oldest = files.pop(0)
            os.remove(os.path.join(self.history_dir, oldest))
            logger.info("Rotated out old history: %s", oldest)