# services/ai_service.py — Gemini 對話 + 摘要
# 使用 google-generativeai SDK，模型需先確認 ListModels

import os
import google.generativeai as genai

# 初始化（API Key 從環境變數讀取）
genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))

# TODO: 執行前需呼叫 genai.list_models() 確認可用模型名稱，禁止直接猜測
#       確認後將模型名填入下方常數
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # 例："models/gemini-1.5-pro"

_SUMMARY_PROMPT = """
你是一個家庭生命紀錄助手。請將以下訊息整理成一句20字以內的繁體中文摘要，
保留時間、地點、人物等關鍵資訊。

訊息內容：
{text}

摘要：
""".strip()


def generate_summary(text: str) -> str:
    """將文字訊息生成摘要，失敗時回傳原文前50字"""
    if not GEMINI_MODEL:
        raise RuntimeError("GEMINI_MODEL 環境變數未設定，請先確認可用模型名稱")
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(_SUMMARY_PROMPT.format(text=text))
        return response.text.strip()
    except Exception as e:
        # 降級處理：直接截斷
        return text[:50]


def list_available_models() -> list[str]:
    """列出目前可用的 Gemini 模型（部署前必須呼叫確認）"""
    return [m.name for m in genai.list_models()]
