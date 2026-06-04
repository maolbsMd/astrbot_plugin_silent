import re
import json
import time
import asyncio
from typing import Any, Iterable, Optional
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
        
        # 型別一致性：使用 0.0 來對應 float 類型的時間
        self._cached_pattern: Optional[re.Pattern] = None
        self._last_mtime: float = 0.0
        self._last_check_time: float = 0.0
        self._check_interval: float = 5.0 
        
        # 引入非同步鎖，防止高併發下的競態條件
        self._lock = asyncio.Lock()
        
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

    def _is_file_modified(self) -> bool:
        """單一職責：僅負責檢查配置檔案的修改時間是否變動"""
        if not self.config_path.exists():
            return False
            
        try:
            current_mtime = self.config_path.stat().st_mtime
            if current_mtime != self._last_mtime:
                self._last_mtime = current_mtime
                return True
        except OSError:
            logger.exception("[Smart Silence] 無法獲取配置文件狀態")
            
        return False

    def _load_and_compile(self):
        """單一職責：僅負責讀取 JSON 並編譯正則表達式"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                raw_tags = config.get("silent_tags", self.silent_tags)
                
                if not isinstance(raw_tags, list):
                    logger.warning("[Smart Silence] 配置格式錯誤：silent_tags 必須是列表格式。將使用上次的快取或預設值。")
                    return
                    
                valid_tags = [t.strip() for t in raw_tags if t and t.strip()]
                
                if valid_tags:
                    pattern_str = "|".join(map(re.escape, valid_tags))
                    self._cached_pattern = re.compile(pattern_str)
                else:
                    self._cached_pattern = None
                    
            logger.debug("[Smart Silence] 配置已更新，重新編譯正則表達式。")
            
        except json.JSONDecodeError:
            logger.exception("[Smart Silence] 配置文件 JSON 格式損壞，請檢查語法！")
        except OSError:
            logger.exception("[Smart Silence] 讀取配置文件失敗 (可能權限不足或檔案被佔用)")
        except Exception:
            logger.exception("[Smart Silence] 實時讀取配置時發生未知錯誤")

    async def get_current_pattern(self) -> Optional[re.Pattern]:
        """主入口：負責併發控制與緩存時間邏輯"""
        # 使用 time.monotonic() 確保時間不受系統時鐘回撥影響
        current_time = time.monotonic()
        
        # 第一層檢查 (無鎖)：如果還在冷卻時間內，直接返回快取，提升效能
        if current_time - self._last_check_time < self._check_interval:
            return self._cached_pattern
            
        # 獲取鎖，保護共享狀態
        async with self._lock:
            # 雙重檢查鎖定模式 (Double-checked locking)：防止等待鎖的期間被其他任務更新
            current_time = time.monotonic()
            if current_time - self._last_check_time < self._check_interval:
                return self._cached_pattern
                
            # 更新檢查時間
            self._last_check_time = current_time
            
            # 若檔案被修改過，才執行讀取與編譯
            if self._is_file_modified():
                self._load_and_compile()
                
        return self._cached_pattern

    def _extract_text_from_chain(self, chain: Iterable[Any]) -> str:
        """封裝相容性防禦邏輯，遍歷訊息鏈提取純文字"""
        if hasattr(chain, 'to_text') and callable(getattr(chain, 'to_text')):
            return chain.to_text()
        
        text_parts = []
        for component in chain:
            if hasattr(component, 'text'):
                text_parts.append(str(component.text))
            else:
                text_parts.append(str(component))
                
        return "".join(text_parts)

    @filter.on_llm_response(priority=1)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        if not resp or not resp.completion_text:
            return

        # 這裡必須加上 await，因為 get_current_pattern 已升級為非同步方法
        current_pattern = await self.get_current_pattern()
        if not current_pattern