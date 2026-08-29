"""故障转移 AI 客户端 — 按顺序尝试主模型池中的模型。

实现：
- 顺序尝试每个模型（OpenAI 兼容接口）
- 记录失败日志
- 全部失败则抛出异常
- 支持传入 tools 参数
"""

import logging
import os
from typing import Any

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)

# 未显式配置 timeout_seconds 时的默认超时（秒）。openai 请求超时会抛异常，
# 由 call() 捕获后切换到下一个模型（故障转移）。
DEFAULT_TIMEOUT = 120


class FaultTolerantClient:
    """顺序尝试多个模型，故障转移."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "models.yaml"
            )
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.models = config.get("main_models", [])
        if not self.models:
            raise ValueError("No main_models configured in models.yaml")

    def call(
        self,
        prompt: str,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """调用 AI 模型，失败时自动切换下一个模型.

        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            tools: Function Calling 工具定义
            temperature: 温度参数
            max_tokens: 最大 token 数

        Returns:
            OpenAI 兼容的响应字典（含 choices 等）

        Raises:
            RuntimeError: 所有模型都失败
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        last_error = None
        for i, model_cfg in enumerate(self.models):
            model_name = model_cfg["model_name"]
            api_base = model_cfg["api_base"]
            api_key = os.environ.get(model_cfg["api_key_env"], "")

            if not api_key:
                logger.warning(
                    "Skipping %s: env var %s not set", model_name, model_cfg["api_key_env"]
                )
                continue

            try:
                # 每个模型可用各自的超时（如 tju-llm 设 timeout_seconds=5，超时快速切下一个）
                timeout = model_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT
                client = OpenAI(api_key=api_key, base_url=api_base, timeout=timeout)
                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }

                # 尝试 Function Calling
                fc_attempted = False
                if tools:
                    try:
                        kwargs["tools"] = tools
                        kwargs["tool_choice"] = "auto"
                        fc_attempted = True
                        resp = client.chat.completions.create(**kwargs)
                        logger.info("Model %s succeeded with Function Calling", model_name)
                        return resp
                    except Exception as e:
                        error_msg = str(e).lower()
                        if "tools" in error_msg or "tool" in error_msg:
                            logger.info(
                                "Model %s does not support tools, retrying without tools",
                                model_name,
                            )
                            kwargs.pop("tools", None)
                            kwargs.pop("tool_choice", None)
                            fc_attempted = False
                        else:
                            raise

                if not fc_attempted:
                    resp = client.chat.completions.create(**kwargs)
                    logger.info("Model %s succeeded", model_name)
                    return resp

            except Exception as e:
                logger.warning("Model %s failed: %s", model_name, e)
                last_error = e
                continue

        raise RuntimeError(f"All models failed. Last error: {last_error}")