"""独立检查模块 — 校验生成报告的质量。

支持故障转移：checker 从 models.yaml 读取（可配置为单个模型或列表），
按顺序尝试；每个模型可用各自的 timeout_seconds（如 tju-llm 设 5s，
云端连不上 TJU 时快速切到下一个模型如 deepseek）。

实现：
- 从 models.yaml 加载 checker 模型列表
- check(report_content) -> dict
- 校验三部分分类、链接有效性、增量逻辑
- token 上限 500
"""

import logging
import os
import json
from typing import Any

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15

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
    """独立检查器，校验报告合规性，支持模型故障转移."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "models.yaml"
            )
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        checker_cfg = config.get("checker")
        # 兼容单个 dict 或列表
        self.models = checker_cfg if isinstance(checker_cfg, list) else [checker_cfg]
        self.models = [m for m in self.models if m]  # 过滤空

    def _call_one(self, model_cfg: dict, content: str) -> dict[str, Any] | None:
        """用单个模型执行检查，成功返回解析结果，失败返回 None."""
        model_name = model_cfg.get("model_name")
        api_base = model_cfg.get("api_base")
        api_key_env = model_cfg.get("api_key_env")
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        timeout = model_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT

        if not api_key or not model_name:
            logger.warning("checker 模型缺 key/名称: %s", model_cfg)
            return None

        if len(content) > 3000:
            truncated = content[:3000] + "\n\n...(truncated)"
        else:
            truncated = content

        try:
            client = OpenAI(api_key=api_key, base_url=api_base, timeout=timeout)
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": CHECKER_PROMPT},
                    {"role": "user", "content": truncated},
                ],
                temperature=0.1,
                max_tokens=500,
            )
            content_out = resp.choices[0].message.content or ""
            result = json.loads(content_out)
            logger.info("checker(%s) 结果: %s", model_name, result)
            return result
        except json.JSONDecodeError:
            try:
                from json_repair import repair_json
                result = json.loads(repair_json(content_out))
                return result
            except Exception:
                logger.warning("checker(%s) 返回非法 JSON", model_name)
                return None
        except Exception as e:
            logger.warning("checker(%s) 调用失败: %s", model_name, e)
            return None

    def check(self, report_content: str) -> dict[str, Any] | None:
        """按顺序尝试各 checker 模型，第一个成功即返回；全失败返回 None."""
        for model_cfg in self.models:
            result = self._call_one(model_cfg, report_content)
            if result is not None:
                return result
        logger.warning("所有 checker 模型均失败")
        return None
