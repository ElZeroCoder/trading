# reporter.py
from __future__ import annotations
import os, json, asyncio, logging, logging.handlers, sys
from typing import Optional

# محاولة استيراد python-telegram-bot
try:
    import telegram  # pip install python-telegram-bot
except Exception:
    telegram = None


_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR":    logging.ERROR,
    "WARNING":  logging.WARNING,
    "INFO":     logging.INFO,
    "DEBUG":    logging.DEBUG,
}


def _get_logger(name: str, level_name: str, log_dir: str) -> logging.Logger:
    """
    ينشئ لوجر بكونسول + فايل روتيتنج، ويتجنب إضافة هاندلرز مكررة لو اتنادى أكتر من مرة.
    """
    level = _LOG_LEVELS.get(level_name.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # لو عنده هاندلرز قبل كده يبقى متضبط—بلاش نعيد
    if getattr(logger, "_configured", False):
        return logger

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    # Console handler
    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler (rotating)
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    logger._configured = True  # type: ignore[attr-defined]
    return logger


class Reporter:
    """
    - log(msg, level="INFO"): لوج موحّد (Console + File)
    - notify(text, markdown=False): Async Telegram (إن وُجد) وإلا fallback للّوج
    - notify_sync(text, markdown=False): للنداء من سياق sync
    - save_trade(trade: dict): يحفظ الصفقات في JSONL تحت data_dir
    - configurable via config:
        report.log_level: INFO/DEBUG/...
        report.log_dir:   (افتراضي: <data_dir>/logs)
        telegram.bot_token / telegram.chat_id
    """

    def __init__(self, cfg: dict, data_dir: Optional[str] = None):
        self.cfg = cfg
        self.data_dir = data_dir or os.getcwd()
        os.makedirs(self.data_dir, exist_ok=True)

        # إعدادات اللوج
        rcfg = (cfg.get("report", {}) or {})
        self.log_level_name = str(rcfg.get("log_level", "INFO"))
        log_dir = rcfg.get("log_dir") or os.path.join(self.data_dir, "logs")

        # إعداد اللوجر (بدون basicConfig لتفادي التعارض)
        self.logger = _get_logger("bot", self.log_level_name, log_dir)

        # ملف الصفقات
        self.trades_file = os.path.join(self.data_dir, "trades.jsonl")

        # إعداد تيليجرام
        tcfg = (cfg.get("telegram", {}) or {})
        self._tg_token = tcfg.get("bot_token")
        self._tg_chat  = tcfg.get("chat_id")
        self.tg_bot = None
        if telegram is None:
            self.log("[Reporter] python-telegram-bot غير متاح. إشعارات التليجرام هتتبعت كـ log فقط.", level="WARNING")
        elif not self._tg_token or not self._tg_chat:
            self.log("[Reporter] bot_token/chat_id مش متضبوطين في config.telegram. إشعارات التليجرام هتتبعت كـ log.", level="WARNING")
        else:
            try:
                self.tg_bot = telegram.Bot(token=self._tg_token)
                # Ping خفيف اختياري للتأكد
                # self.tg_bot.get_me()
                self.log("[Reporter] Telegram bot جاهز.", level="INFO")
            except Exception as e:
                self.tg_bot = None
                self.log(f"[Reporter] فشل تهيئة Telegram bot: {e}", level="ERROR")

    # ---------- Logging ----------
    def log(self, msg: str, level: str = "INFO"):
        lvl = _LOG_LEVELS.get(level.upper(), logging.INFO)
        self.logger.log(lvl, msg)

    # ---------- Notifications ----------
    async def notify(self, text: str, markdown: bool = False):
        """
        - إن توفر Telegram bot + chat_id، يبعث رسالة.
        - خلاف ذلك، يطبع في اللوج كبديل.
        """
        if self.tg_bot and self._tg_chat:
            try:
                parse_mode = "Markdown" if markdown else None
                loop = asyncio.get_running_loop()
                # send_message في python-telegram-bot بلوكينج، فنشغلها في executor
                await loop.run_in_executor(
                    None,
                    lambda: self.tg_bot.send_message(
                        chat_id=self._tg_chat, text=text, parse_mode=parse_mode
                    ),
                )
            except Exception as e:
                self.log(f"[Reporter] Telegram notify فشل: {e}. Falling back to log. Text={text}", level="ERROR")
                self.log(f"[NOTIFY] {text}")
        else:
            self.log(f"[NOTIFY] {text}")

    def notify_sync(self, text: str, markdown: bool = False):
        """
        استدعاء مريح من كود Sync:
        - إن فيه event loop شغال: نعمل create_task
        - لو مفيش: نشغل loop مؤقت بـ asyncio.run
        """
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                asyncio.create_task(self.notify(text, markdown))
            else:
                asyncio.run(self.notify(text, markdown))
        except RuntimeError:
            asyncio.run(self.notify(text, markdown))

    # ---------- Trades persistence ----------
    def save_trade(self, trade: dict):
        """
        يضيف صفقة كسطر JSON في trades.jsonl
        """
        try:
            with open(self.trades_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trade, ensure_ascii=False) + "\n")
        except Exception as e:
            self.log(f"[Reporter] Failed to save trade: {e}", level="ERROR")
        self.log(f"[TRADE] {trade}", level="INFO")
