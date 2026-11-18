"""模型接入层：主流做法 = provider 适配 + 配置外置，业务代码里 ⛔ 出现裸 HTTP。

⚠️ 三条纪律：
1. **key 只从环境读**，用 ``SecretStr`` 包起来（repr/日志自动打码），
   ⛔ 写进任何文件、⛔ 进 git、⛔ 出现在异常文本里。
2. 走 **OpenAI 兼容口**（``ChatOpenAI(base_url=...)``）——业界连百炼的主流接法，
   ⛔ 自己拿 httpx 拼请求体（那是 D1 受限环境才需要的土办法）。
3. 测试**一律用打桩模型**（``forge.model.scripted``），⛔ 在 CI 里烧真钱、⛔ 让验收随网络抖动。
"""
from __future__ import annotations

import os
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class Settings(BaseSettings):
    """``FORGE_*`` 环境变量 + 可选 ``.env``（.env 已在 gitignore 里）。"""

    model_config = SettingsConfigDict(env_prefix="FORGE_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    api_key: Optional[SecretStr] = Field(default=None, exclude=True)
    base_url: str = DEFAULT_BASE_URL
    model: str = "qwen-plus"
    judge_model: str = "qwen-max"

    def require_key(self) -> str:
        key = self.api_key
        if key is None and os.environ.get("FORGE_API_KEY"):
            key = SecretStr(os.environ["FORGE_API_KEY"])
        if not key:
            # ⛔ 把任何 key 片段拼进异常文本
            raise RuntimeError("缺少 FORGE_API_KEY：请写进 .env 或环境变量（本项目不会打印它）")
        return key.get_secret_value()


def build_live_model(*, model: Optional[str] = None, temperature: float = 0.0,
                     settings: Optional[Settings] = None) -> BaseChatModel:
    """真模型。**只在 ``--live`` 显式要求时**才走这条路。"""
    from langchain_openai import ChatOpenAI

    s = settings or Settings()
    return ChatOpenAI(model=model or s.model, base_url=s.base_url,
                      api_key=s.require_key(), temperature=temperature)


def secret_leak_check(settings: Optional[Settings] = None) -> dict[str, bool]:
    """给测试用：确认 key 不会从 repr / str / model_dump 三条路漏出去。"""
    s = settings or Settings()
    secret = s.api_key.get_secret_value() if s.api_key else None
    sinks = {"repr": repr(s), "str": str(s), "dump": str(s.model_dump())}
    leaked = [k for k, v in sinks.items() if secret and secret in v]
    return {"clean": not leaked, "leaked_via": leaked}
