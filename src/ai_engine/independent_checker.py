"""独立检查模块 — 校验生成报告的质量。

实现：
- 从 models.yaml 加载 checker 模型
- check(report_content) -> dict
- 校验三部分分类、链接有效性、增量逻辑
- token 上限 500，超时 15s
"""

import json
import logging
import os
from typing import Any

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)

CHECKER_PROMPT = """请检查以下信息简报的质量，返回 JSON 格式结果。

检查项：
1. 三部分分类（Part1 关键词命中、Part2 AI 推荐、Part3 其余）是否严格符合规则
2. 每条信息是否包含有效总结和可访问的原文链接
3. 增量逻辑是否正确（不应包含已处理过的旧内容）

返回格式（严格 JSON）：
{"passed": true/false, "errors": ["错误描述1", "错误描述2"], "warnings": ["警告1"]}

如果检查通过但发现分类错误，errors 中列出错误条目编号。
如果链接失效超过 50%，passed 设为 false。
"""


class IndependentChecker:
    """独立检查器，校验报告合规性."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "models.yaml"
            )
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        checker_cfg = config.get("checker", {})
        self.model_name = checker_cfg.get("model_name", "gpt-4o-mini")
        self.api_base = checker_cfg.get("api_base", "https://api.openai.com/v1")
        self.api_key_env = checker_cfg.get("api_key_env", "OPENAI_API_KEY")

    def check(self, report_content: str) -> dict[str, Any] | None:
        """检查报告内容.

        Args:
            report_content: Markdown 报告内容

        Returns:
            {"passed": bool, "errors": [...], "warnings": [...]} 或 None（检查失败）
        """
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            logger.warning("Checker API key not set (%s), skipping check", self.api_key_env)
            return None

        # 截断报告内容以控制 token
        if len(report_content) > 3000:
            truncated = report_content[:3000] + "\n\n...(truncated)"
        else:
            truncated = report_content

        try:
            client = OpenAI(api_key=api_key, base_url=self.api_base, timeout=15)
            resp = client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": CHECKER_PROMPT},
                    {"role": "user", "content": truncated},
                ],
                temperature=0.1,
                max_tokens=500,
            )
            content = resp.choices[0].message.content or ""
            # 解析 JSON
            result = json.loads(content)
            logger.info("Checker result: %s", result)
            return result
        except json.JSONDecodeError:
            # 尝试修复
            try:
                from json_repair import repair_json
                content = repair_json(content)
                result = json.loads(content)
                return result
            except Exception:
                logger.warning("Checker returned invalid JSON, skipping")
                return None
        except Exception as e:
            logger.warning("Checker call failed: %s", e)
            return None