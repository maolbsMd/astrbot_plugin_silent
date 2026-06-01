import re
import os
import json
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

@register("astrbot_plugin_silent", "YourName", "拦截含有特定标签的 LLM 回复", "1.1.0")
class SilentResponsePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config_path = os.path.join(os.path.dirname(__file__), "config.json")
        self.silent_tags = ["<silent>"] 
        # 初始化时检查并生成默认配置文件
        self._ensure_config_exists()

    def _ensure_config_exists(self):
        """如果配置文件不存在，则初始化一个默认的，方便 WebUI 抓取"""
        if not os.path.exists(self.config_path):
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump({"silent_tags": self.silent_tags}, f, ensure_ascii=False, indent=4)
                logger.info("[Silent Plugin] 找不到配置文件，已自动生成预设的 config.json")
            except Exception as e:
                logger.error(f"[Silent Plugin] 自动创建配置文件失败: {e}")

    def get_current_pattern(self):
        """每次拦截前，实时读取最新的 config.json (完美适配 WebUI 实时修改)"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    tags = config.get("silent_tags", self.silent_tags)
                    
                    if tags:
                        # 将 JSON 中的标签列表转换为正则表达式
                        pattern_str = "|".join(map(re.escape, tags))
                        return re.compile(pattern_str)
            except Exception as e:
                logger.warning(f"[Silent Plugin] 实时读取配置失败，请检查 WebUI 中的 JSON 格式是否正确: {e}")
        return None

    @filter.on_llm_response(priority=1)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        if not resp or not resp.completion_text:
            return

        # 动态获取最新的正则规则
        current_pattern = self.get_current_pattern()
        if not current_pattern:
            return

        reply_text = resp.completion_text

        # 检查是否命中拦截标签
        if current_pattern.search(reply_text):
            logger.info(f"[Silent Plugin] 检测到拦截标签，已阻止该消息发送。原始内容: {reply_text}")
            resp.completion_text = ""