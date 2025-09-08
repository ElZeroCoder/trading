# filters.py
from __future__ import annotations
import time
from typing import List
import requests
import pandas as pd
from ta.volatility import AverageTrueRange


class FilterManager:
    """
    - fetch_universe: يجيب قائمة الرموز بناءً على الـ config
        1) لو في قائمة جاهزة في config.universe.symbols => نستخدمها مباشرة
        2) غير كده: نفلتر من /api/v3/ticker/24hr حسب عملة الاقتباس وحجم تداول 24h
    - volatility_filter: فلترة ATR كنسبة من السعر
    - multi_timeframe_confirmation: تأكيد اتجاه على فريمين
    - news_sentiment_ok: فلترة أخبار بسيطة (CryptoPanic + Reddit) — اختيارية
    """

    BASE_MAIN = "https://api.binance.com"
    BASE_TESTNET = "https://testnet.binance.vision"

    def __init__(self, cfg: dict, reporter, exchange):
        self.cfg = cfg
        self.reporter = reporter
        self.exchange = exchange  # ExchangeClient

        # تحديد الـ base URL حسب testnet من config
        self.base = (
            self.BASE_TESTNET
            if (cfg.get("binance", {}) or {}).get("testnet", False)
            else self.BASE_MAIN
        )

        # مفاتيح متوقعة من config.trade
        tcfg = cfg.get("trade", {}) or {}
        self.quote_asset = str(tcfg.get("quote_asset", "USDT")).upper()
        self.min_qv_24h = float(tcfg.get("min_24h_quote_volume", 0.0))
        self.max_symbols = int(tcfg.get("max_symbols_per_scan", 50))

    # ---------------- Universe ----------------
    def fetch_universe(self) -> List[str]:
        """
        احضر قائمة العملات بناءً على:
        - config.universe.symbols إن وُجدت (أولوية أولى)
        - وإلا فلترة /api/v3/ticker/24hr حسب:
            * ينتهي بـ quote_asset (مثلاً USDT)
            * quoteVolume >= min_24h_quote_volume
        - ثم قصّ القائمة إلى max_symbols_per_scan
        """
        # (1) من config مباشرة
        ucfg = (self.cfg.get("universe", {}) or {})
        explicit = ucfg.get("symbols")
        if explicit and isinstance(explicit, list) and len(explicit) > 0:
            symbols = [s.replace("/", "").upper() for s in explicit]
            if self.max_symbols > 0:
                symbols = symbols[: self.max_symbols]
            self.reporter.log(f"[Filter] Universe from config: {len(symbols)} symbols")
            return symbols

        # (2) فلترة حسب سيولة 24 ساعة عبر REST
        try:
            url = f"{self.base}/api/v3/ticker/24hr"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            tickers = resp.json()

            symbols: List[str] = []
            for t in tickers:
                s = t.get("symbol", "")
                # لازم ينتهي بعملة الاقتباس (USDT افتراضيًا)
                if not s.endswith(self.quote_asset):
                    continue
                # حجم تداول 24 ساعة بالـ quote asset
                qvol = float(t.get("quoteVolume") or 0.0)
                if qvol < self.min_qv_24h:
                    continue
                symbols.append(s)

            # قصّ بحسب الحد الأقصى
            if self.max_symbols > 0:
                symbols = symbols[: self.max_symbols]

            if not symbols:
                symbols = ["BTCUSDT", "ETHUSDT"]

            self.reporter.log(
                f"[Filter] Universe selected: {len(symbols)} symbols "
                f"(quote={self.quote_asset}, min_qv_24h={self.min_qv_24h})"
            )
            return symbols

        except Exception as e:
            self.reporter.log(f"[Filter] Universe fallback due to error: {e}")
            return ["BTCUSDT", "ETHUSDT"]

    # ---------------- Volatility ----------------
    def volatility_filter(
        self, symbol: str, lookback: int = 14, min_atr_pct: float = 0.001
    ) -> bool:
        """فلترة بالـ ATR كنسبة من السعر."""
        try:
            kl = self.exchange.get_klines(symbol, "1m", limit=lookback + 1)
        except Exception:
            return False

        if not kl or len(kl) < lookback + 1:
            return False

        df = pd.DataFrame(
            kl,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "close_time",
                "quote_av",
                "trades",
                "tb_base_av",
                "tb_quote_av",
                "ignore",
            ],
        )
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)

        try:
            atr = AverageTrueRange(
                high=df["high"], low=df["low"], close=df["close"], window=lookback
            )
            atrv = float(atr.average_true_range().iloc[-1])
            last_close = float(df["close"].iloc[-1])
            if last_close <= 0:
                return False
            atr_pct = atrv / last_close
            return atr_pct >= float(
                self.cfg.get("trade", {}).get("min_atr_pct", min_atr_pct)
            )
        except Exception:
            return False

    # ---------------- Multi-Timeframe ----------------
    def multi_timeframe_confirmation(
        self, symbol: str, short_tf: str = "1m", long_tf: str = "5m"
    ) -> bool:
        """تأكيد الاتجاه عبر أكثر من فريم (MA قصير لفريم قصير > MA طويل لفريم أطول)."""
        try:
            kl_short = self.exchange.get_klines(symbol=symbol, interval=short_tf, limit=30)
            kl_long = self.exchange.get_klines(symbol=symbol, interval=long_tf, limit=30)
            if not kl_short or not kl_long:
                return False

            closes_short = [float(k[4]) for k in kl_short]
            closes_long = [float(k[4]) for k in kl_long]

            if not closes_short or not closes_long:
                return False

            ma_s_len = min(9, len(closes_short))
            ma_l_len = min(21, len(closes_long))

            if ma_s_len == 0 or ma_l_len == 0:
                return False

            ma_short = sum(closes_short[-ma_s_len:]) / ma_s_len
            ma_long = sum(closes_long[-ma_l_len:]) / ma_l_len

            return ma_short > ma_long
        except Exception:
            return False

    # ---------------- News & Sentiment ----------------
    def news_sentiment_ok(self, symbol: str) -> bool:
        """فلترة الأخبار والمشاعر (CryptoPanic + Reddit) — اختيارية ولو فشلت بنعدّي."""
        negative_words = [
            "scam",
            "hack",
            "exploit",
            "rugpull",
            "lawsuit",
            "investigation",
            "fraud",
            "pump",
        ]

        # --- CryptoPanic (لو API key موجود) ---
        key = (self.cfg.get("news", {}) or {}).get("crypto_panic_api_key")
        if key:
            try:
                url = f"https://cryptopanic.com/api/v1/posts/?auth_token={key}&public=true"
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    token = symbol[:-len(self.quote_asset)].lower() if symbol.endswith(self.quote_asset) else symbol.lower()
                    for post in data.get("results", []):
                        title = (post.get("title") or "").lower()
                        if token and token in title:
                            if any(w in title for w in negative_words):
                                self.reporter.log(
                                    f"[NewsFilter] Negative news for {symbol}: {title}"
                                )
                                return False
            except Exception:
                pass

        # --- Reddit ---
        try:
            token = symbol[:-len(self.quote_asset)].lower() if symbol.endswith(self.quote_asset) else symbol.lower()
            r = requests.get(
                f"https://www.reddit.com/r/CryptoCurrency/search.json?q={token}&restrict_sr=1&sort=new",
                headers={"User-Agent": "bot"},
                timeout=5,
            )
            if r.status_code == 200:
                posts = r.json().get("data", {}).get("children", [])
                for p in posts[:5]:
                    title = (p.get("data", {}) or {}).get("title", "").lower()
                    if any(w in title for w in negative_words):
                        self.reporter.log(
                            f"[NewsFilter] Reddit negative for {symbol}: {title}"
                        )
                        return False
        except Exception:
            pass

        return True
