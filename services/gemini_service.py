# services/gemini_service.py — Gemini AI 一般助理（google-genai SDK）
#
# 設計：弱 AI，簡單陪伴 + 問答，不做長記憶/RAG/情緒人格
# 失敗 → 備援訊息，不拋例外

import os
import logging
from google import genai
from google.genai import types

_log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是「小暖」，一位親切溫暖的家庭生活助理。
負責陪伴對話、回答日常問題、記錄生活點滴。
語氣溫暖、簡潔，回覆控制在 200 字以內，適合手機閱讀。
使用繁體中文。"""

_MODEL_NAME = "gemini-2.5-flash"

# 啟動時建立 client 一次
_client: genai.Client | None = None


def _get_client() -> genai.Client | None:
    global _client
    if _client is None:
        key = os.environ.get("GEMINI_API_KEY", "")
        if key:
            _client = genai.Client(api_key=key)
    return _client


def generate_reply(user_message: str) -> str:
    """
    呼叫 Gemini，回傳回覆文字。
    失敗 → 備援訊息，不拋例外（呼叫方不需 try/except）。
    """
    client = _get_client()
    if client is None:
        return "現在有點忙，等等再陪你聊 💙"

    try:
        response = client.models.generate_content(
            model   = _MODEL_NAME,
            contents = user_message,
            config  = types.GenerateContentConfig(
                system_instruction = _SYSTEM_PROMPT,
            ),
        )
        return response.text
    except Exception as e:
        _log.error("gemini error: %s", e)
        return "現在有點忙，等等再陪你聊 💙"


def health_check() -> bool:
    """確認 Gemini SDK 可用（不呼叫 API，只確認 key 存在）"""
    return bool(os.environ.get("GEMINI_API_KEY", ""))
