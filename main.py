import re
import os
import json
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

@register("astrbot_plugin_smart_silence", "maolbsMd", "進階版攔截含有特定標籤的 LLM 回覆", "1.0.0")
class SmartSilencePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config_path = os.path.join(os.path.dirname(__file__), "config.json")
        self.silent_tags = ["<silent>"] 
        self._ensure_config_exists()

    def _ensure_config_exists(self):
        """確保設定檔存在"""
        if not os.path.exists(self.config_path):
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump({"silent_tags": self.silent_tags}, f, ensure_ascii=False, indent=4)
                logger.info("[Smart Silence] 找不到配置文件，已自動生成預設的 config.json")
            except Exception as e:
                logger.error(f"[Smart Silence] 自動創建配置文件失敗: {e}")

    def get_current_pattern(self):
        """獲取最新的正則表達式規則"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    tags = config.get("silent_tags", self.silent_tags)
                    if tags:
                        pattern_str = "|".join(map(re.escape, tags))
                        return re.compile(pattern_str)
            except Exception as e:
                logger.warning(f"[Smart Silence] 實時讀取配置失敗: {e}")
        return None

    # ================= 第一道防線：攔截一般對話 =================
    @filter.on_llm_response(priority=1)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        if not resp or not resp.completion_text:
            return

        current_pattern = self.get_current_pattern()
        if not current_pattern:
            return

        if current_pattern.search(resp.completion_text):
            logger.info(f"[Smart Silence] (LLM 攔截) 檢測到攔截標籤，已阻止一般訊息發送。")
            resp.completion_text = ""


    # ================= 第二道防線：攔截主動發送與最終結果 =================
    @filter.on_decorating_result(priority=1)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        這個鉤子會在訊息最終要打包送出前觸發。
        無論訊息是從哪裡來的（主動或被動），都會經過這裡。
        """
        current_pattern = self.get_current_pattern()
        if not current_pattern:
            return
            
        # 獲取準備發送的最終結果物件
        result = event.get_result()
        
        # 確認這是一個準備發送的事件，且訊息鏈 (chain) 存在
        if result and hasattr(result, 'chain') and len(result.chain) > 0:
            # 將訊息鏈中的所有元件（例如 Plain 文字）轉換為純文字來進行比對
            message_text = result.chain.to_text() if hasattr(result.chain, 'to_text') else str(result.chain)
            
            if current_pattern.search(message_text):
                logger.info(f"[Smart Silence] (最終攔截) 檢測到主動發送的攔截標籤，已清空訊息鏈。")
                # 清空訊息鏈，AstrBot 若發現 chain 為空，將不會發送任何內容
                result.chain.clear()
                # 保險起見，同時呼叫停止事件
                event.stop_event()
