import re
import json
import time
from typing import Any, Iterable
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

@register("astrbot_plugin_smart_silence", "maolbsMd", "進階版攔截含有特定標籤的 LLM 回覆", "1.0.0")
class SmartSilencePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
        plugin_dir = StarTools.get_data_dir()
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = plugin_dir / "config.json"
        
        self.silent_tags = ["<silent>"] 
        
        self._cached_pattern = None
        self._last_mtime = 0
        self._last_check_time = 0
        self._check_interval = 5.0 
        
        self._ensure_config_exists()

    def _ensure_config_exists(self):
        if not self.config_path.exists():
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump({"silent_tags": self.silent_tags}, f, ensure_ascii=False, indent=4)
                logger.info("[Smart Silence] 找不到配置文件，已自動生成預設的 config.json")
            except OSError:
                logger.exception("[Smart Silence] 自動創建配置文件失敗 (OSError)")
            except Exception:
                logger.exception("[Smart Silence] 自動創建配置文件時發生未知錯誤")

    def get_current_pattern(self):
        current_time = time.time()
        if current_time - self._last_check_time < self._check_interval:
            return self._cached_pattern
        
        self._last_check_time = current_time

        if not self.config_path.exists():
            return None

        try:
            current_mtime = self.config_path.stat().st_mtime
            
            if current_mtime != self._last_mtime:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    raw_tags = config.get("silent_tags", self.silent_tags)
                    
                    if not isinstance(raw_tags, list):
                        logger.warning("[Smart Silence] 配置格式錯誤：silent_tags 必須是列表格式。將使用上次的快取或預設值。")
                        return self._cached_pattern
                        
                    # 過濾空字串與純空白字元，防止生成會攔截所有訊息的正則表達式
                    valid_tags = [t.strip() for t in raw_tags if t and t.strip()]
                    
                    if valid_tags:
                        pattern_str = "|".join(map(re.escape, valid_tags))
                        self._cached_pattern = re.compile(pattern_str)
                    else:
                        self._cached_pattern = None
                        
                self._last_mtime = current_mtime
                logger.debug("[Smart Silence] 配置已更新，重新編譯正則表達式。")
                
        except json.JSONDecodeError:
            logger.exception("[Smart Silence] 配置文件 JSON 格式損壞，請檢查語法！")
        except OSError:
            logger.exception("[Smart Silence] 讀取配置文件失敗 (可能權限不足或檔案被佔用)")
        except Exception:
            logger.exception("[Smart Silence] 實時讀取配置時發生未知錯誤")
            
        return self._cached_pattern

    def _extract_text_from_chain(self, chain: Iterable[Any]) -> str:
        """封裝相容性防禦邏輯，遍歷訊息鏈提取純文字"""
        # 如果 chain 自身帶有轉換文字的方法，優先呼叫
        if hasattr(chain, 'to_text') and callable(getattr(chain, 'to_text')):
            return chain.to_text()
        
        # 安全地遍歷列表，提取有效文字
        text_parts = []
        for component in chain:
            # 大部分 AstrBot 的文字組件會將文字儲存在 text 屬性中
            if hasattr(component, 'text'):
                text_parts.append(str(component.text))
            else:
                # 若無 text 屬性，則將組件自身轉為字串
                text_parts.append(str(component))
                
        return "".join(text_parts)

    @filter.on_llm_response(priority=1)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        if not resp or not resp.completion_text:
            return

        current_pattern = self.get_current_pattern()
        if not current_pattern:
            return

        if current_pattern.search(resp.completion_text):
            logger.debug("[Smart Silence] (LLM 攔截) 檢測到攔截標籤，已阻止一般訊息發送。")
            resp.completion_text = ""

    @filter.on_decorating_result(priority=1)
    async def on_decorating_result(self, event: AstrMessageEvent):
        current_pattern = self.get_current_pattern()
        if not current_pattern:
            return
            
        result = event.get_result()
        
        if result and hasattr(result, 'chain') and len(result.chain) > 0:
            message_text = self._extract_text_from_chain(result.chain)
            
            if current_pattern.search(message_text):
                logger.debug("[Smart Silence] (最終攔截) 檢測到主動發送的攔截標籤，已清空訊息鏈。")
                result.chain.clear()
                event.stop_event()
