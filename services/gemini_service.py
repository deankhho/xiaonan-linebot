# services/gemini_service.py — Gemini AI 一般助理
#
# 設計：弱 AI，簡單陪伴 + 問答，不做長記憶/RAG/情緒人格
# 失敗 → 備援訊息，不拋例外

import os
import logging
import google.generativeai as genai

_log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是「小暖」，一位親切溫暖的家庭生活助理。
負責陪伴對話、回答日常問題、記錄生活點滴。
語氣溫暖、簡潔，回覆控制在 200 字以內，適合手機閱讀。
使用繁體中文。"""

_MODEL_NAME = "gemini-2.5-flash"


def generate_reply(user_message: str) -> str:
    """
    呼叫 Gemini，回傳回覆文字。
    失敗 → 備援訊息，不拋例外（呼叫方不需 try/except）。
    """
    try:
        genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
        model    = genai.GenerativeModel(
            model_name        = _MODEL_NAME,
            system_instruction = _SYSTEM_PROMPT,
        )
        response = model.generate_content(user_message)
        return response.text
    except Exception as e:
        _log.error("gemini error: %s", e)
        return "抱歉，我剛剛思考打結了，請稍後再試一次。💙"


def health_check() -> bool:
    """確認 Gemini SDK 可用（不呼叫 API，只確認 import + key 存在）"""
    try:
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            return False
        import google.generativeai  # noqa
        return True
    except Exception:
        return False
