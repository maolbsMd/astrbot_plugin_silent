import re
import json
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

@register("astrbot_plugin_smart_silence", "maolbsMd", "進階版攔截含有特定標籤的 LLM 回覆", "1.0.0")
class SmartSilencePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
        # 1. 規範數據路徑：使用 StarTools.get_data_dir() 取得 Path 物件
        plugin_dir = StarTools.get_data_dir()
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = plugin_dir / "config.json"
        
        self.silent_tags = ["<silent>"] 
        
        # 2. 效能最佳化：加入快取變數與檔案修改時間記錄
        self._cached_pattern = None
        self._last_mtime = 0
        
        self._ensure_config_exists()

    def _ensure_config_exists(self):
        """確保設定檔存在"""
        if not self.config_path.exists():
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump({"silent_tags": self.silent_tags}, f, ensure_ascii=False, indent=4)
                logger.info("[Smart Silence] 找不到配置文件，已自動生成預設的 config.json")
            except OSError:
                # 3. 例外處理最佳化：使用 exception 記錄完整堆疊
                logger.exception("[Smart Silence] 自動創建配置文件失敗 (OSError)")
            except Exception:
                logger.exception("[Smart Silence] 自動創建配置文件時發生未知錯誤")

    def get_current_pattern(self):
        """獲取最新的正則表達式規則 (搭載效能快取機制)"""
        if not self.config_path.exists():
            return None

        try:
            # 取得檔案的最後修改時間 (時間戳)
            current_mtime = self.config_path.stat().st_mtime
            
            # 只有當修改時間發生變化時，才重新讀取硬碟並編譯正則
            if current_mtime != self._last_mtime:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    tags = config.get("silent_tags", self.silent_tags)
                    if tags:
                        pattern_str = "|".join(map(re.escape, tags))
                        self._cached_pattern = re.compile(pattern_str)
                    else:
                        self._cached_pattern = None
                # 更新快取的時間戳
                self._last_mtime = current_mtime
                logger.debug("[Smart Silence] 配置已更新，重新編譯正則表達式。")
                
        except json.JSONDecodeError:
            logger.exception("[Smart Silence] 配置文件 JSON 格式損壞，請檢查語法！")
        except OSError:
            logger.exception("[Smart Silence] 讀取配置文件失敗 (可能權限不足或檔案被佔用)")
        except Exception:
            logger.exception("[Smart Silence] 實時讀取配置時發生未知錯誤")
            
        return self._cached_pattern

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

    @filter.on_decorating_result(priority=1)
    async def on_decorating_result(self, event: AstrMessageEvent):
        current_pattern = self.get_current_pattern()
        if not current_pattern:
            return
            
        result = event.get_result()
        
        if result and hasattr(result, 'chain') and len(result.chain) > 0:
            message_text = result.chain.to_text() if hasattr(result.chain, 'to_text') else str(result.chain)
            
            if current_pattern.search(message_text):
                logger.info(f"[Smart Silence] (最終攔截) 檢測到主動發送的攔截標籤，已清空訊息鏈。")
                result.chain.clear()
                event.stop_event()
