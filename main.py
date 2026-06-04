import re
import json
import time
import asyncio
from typing import Any, Iterable, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

try:
    # 用于精确判断文本组件，避免误伤图片/At 等
    from astrbot.api.message_components import Plain
except Exception:  # pragma: no cover - 兼容旧版本
    Plain = None


@register(
    "astrbot_plugin_smart_silence",
    "maolbsMd",
    "進階版攔截含有特定標籤的 LLM 回覆（主動沉默）",
    "1.1.0",
)
class SmartSilencePlugin(Star):
    DEFAULT_TAGS = ["<silent>"]

    def __init__(self, context: Context):
        super().__init__(context)

        plugin_dir = StarTools.get_data_dir()
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = plugin_dir / "config.json"

        self.silent_tags = list(self.DEFAULT_TAGS)
        self.ignore_case = False

        self._cached_pattern: Optional[re.Pattern] = None
        self._last_mtime: float = 0.0
        self._last_check_time: float = 0.0
        self._check_interval: float = 5.0

        self._lock = asyncio.Lock()

        self._ensure_config_exists()
        # 显式首次加载，避免依赖隐式 mtime 行为
        self._load_and_compile()

    # ---------------- 配置管理 ----------------

    def _ensure_config_exists(self) -> None:
        if self.config_path.exists():
            return
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"silent_tags": self.silent_tags, "ignore_case": self.ignore_case},
                    f,
                    ensure_ascii=False,
                    indent=4,
                )
            logger.info("[Smart Silence] 找不到配置文件，已自動生成預設的 config.json")
        except OSError:
            logger.exception("[Smart Silence] 自動創建配置文件失敗 (OSError)")
        except Exception:
            logger.exception("[Smart Silence] 自動創建配置文件時發生未知錯誤")

    def _is_file_modified(self) -> bool:
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

    def _load_and_compile(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            raw_tags = config.get("silent_tags", self.silent_tags)
            self.ignore_case = bool(config.get("ignore_case", False))

            if not isinstance(raw_tags, list):
                logger.warning(
                    "[Smart Silence] 配置格式錯誤：silent_tags 必須是列表。"
                    "保留上次的快取或預設值。"
                )
                return

            valid_tags = [t.strip() for t in raw_tags if isinstance(t, str) and t.strip()]

            if not valid_tags:
                self._cached_pattern = None
                logger.debug("[Smart Silence] silent_tags 為空，攔截功能已停用。")
                return

            flags = re.IGNORECASE if self.ignore_case else 0
            pattern_str = "|".join(map(re.escape, valid_tags))
            self._cached_pattern = re.compile(pattern_str, flags)
            logger.debug("[Smart Silence] 配置已更新，重新編譯正則：%s", pattern_str)

        except json.JSONDecodeError:
            logger.exception("[Smart Silence] 配置文件 JSON 格式損壞，請檢查語法！")
        except OSError:
            logger.exception("[Smart Silence] 讀取配置文件失敗（權限不足或檔案被佔用）")
        except Exception:
            logger.exception("[Smart Silence] 實時讀取配置時發生未知錯誤")

    async def get_current_pattern(self) -> Optional[re.Pattern]:
        # 快路徑：未到检查间隔，直接返回缓存（无需加锁）
        if time.monotonic() - self._last_check_time < self._check_interval:
            return self._cached_pattern

        async with self._lock:
            # 双重检查，避免并发重复加载
            if time.monotonic() - self._last_check_time < self._check_interval:
                return self._cached_pattern

            self._last_check_time = time.monotonic()
            if self._is_file_modified():
                self._load_and_compile()

        return self._cached_pattern

    # ---------------- 文本提取 ----------------

    def _extract_text_from_chain(self, chain: Iterable[Any]) -> str:
        text_parts = []
        for component in chain:
            # 优先精确匹配 Plain 文本组件
            if Plain is not None and isinstance(component, Plain):
                text_parts.append(component.text or "")
                continue
            # 退化处理：仅提取带 text 属性的组件，避免把图片/At 等转成噪声
            text = getattr(component, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        return "".join(text_parts)

    # ---------------- 拦截逻辑 ----------------

    @filter.on_llm_response(priority=1)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        if not resp or not resp.completion_text:
            return

        pattern = await self.get_current_pattern()
        if not pattern:
            return

        if pattern.search(resp.completion_text):
            logger.debug("[Smart Silence] (LLM 階段) 檢測到攔截標籤，清空回覆文本。")
            resp.completion_text = ""

    @filter.on_decorating_result(priority=1)
    async def on_decorating_result(self, event: AstrMessageEvent):
        pattern = await self.get_current_pattern()
        if not pattern:
            return

        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        message_text = self._extract_text_from_chain(result.chain)
        if message_text and pattern.search(message_text):
            logger.debug("[Smart Silence] (最終階段) 檢測到攔截標籤，清空訊息鏈並終止事件。")
            result.chain.clear()
            event.stop_event()
