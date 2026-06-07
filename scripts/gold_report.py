from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)
CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class Quote:
    name: str
    symbol: str
    value: float | None
    change_24h: float | None
    high_24h: float | None
    low_24h: float | None
    source: str
    updated_at: datetime | None = None
    error: str | None = None


@dataclass
class CalendarEvent:
    event: str
    country: str
    time_text: str
    period: str
    actual: str
    expected: str
    prior: str
    importance: str


@dataclass
class DriverScore:
    factor: str
    observation: str
    effect: str
    direction_score: int
    risk_score: int
    explanation: str


@dataclass
class TradeDecision:
    headline: str
    action: str
    probabilities: dict[str, int]
    reasons: list[str]
    confidence: str
    trade_grade: str
    trade_grade_text: str
    direction_score: int
    risk_score: int
    driver_scores: list[DriverScore]


@dataclass
class MarketContext:
    quotes: dict[str, Quote]
    notes: list[str]
    prior_evening: dict[str, object] | None = None


def now_cn() -> datetime:
    report_at = (os.getenv("REPORT_AT") or "").strip()
    if report_at:
        try:
            parsed = datetime.fromisoformat(report_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CN_TZ)
            return parsed.astimezone(CN_TZ)
        except ValueError:
            pass
    return datetime.now(CN_TZ)


def is_weekend_cn() -> bool:
    return now_cn().weekday() in {5, 6}


def is_market_closed() -> bool:
    return is_weekend_cn()


def http_get(url: str, *, timeout: int = 15) -> requests.Response:
    return requests.get(url, timeout=timeout, headers={"User-Agent": UA})


def fetch_yahoo_chart(symbol: str, name: str, *, range_: str = "1d", interval: str = "5m") -> Quote:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_}&interval={interval}"
    try:
        resp = http_get(url)
        resp.raise_for_status()
        payload = resp.json()
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        timestamps = result.get("timestamp", [])
        points = [(ts, close) for ts, close in zip(timestamps, closes) if close is not None]
        if not points:
            raise ValueError("no valid close points")

        latest_ts, latest = points[-1]
        cutoff = latest_ts - 24 * 3600
        prior_candidates = [p for p in points if p[0] <= cutoff]
        prior = prior_candidates[-1][1] if prior_candidates else points[0][1]
        last_24h = [p[1] for p in points if p[0] >= cutoff]

        return Quote(
            name=name,
            symbol=symbol,
            value=float(latest),
            change_24h=float(latest - prior),
            high_24h=float(max(last_24h)) if last_24h else None,
            low_24h=float(min(last_24h)) if last_24h else None,
            source=f"Yahoo Finance chart API ({symbol}, {interval})",
            updated_at=datetime.fromtimestamp(latest_ts, tz=timezone.utc).astimezone(CN_TZ),
        )
    except Exception as exc:
        return Quote(name=name, symbol=symbol, value=None, change_24h=None, high_24h=None, low_24h=None, source=url, error=str(exc))


def fetch_twelvedata_chart(symbol: str, name: str, *, interval: str = "5min") -> Quote:
    api_key = os.getenv("TWELVE_DATA_API_KEY")
    if not api_key:
        return Quote(name=name, symbol=symbol, value=None, change_24h=None, high_24h=None, low_24h=None, source="Twelve Data", error="missing TWELVE_DATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 320,
        "timezone": "Asia/Shanghai",
        "apikey": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=20, headers={"User-Agent": UA})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") == "error":
            raise ValueError(payload.get("message", "Twelve Data error"))

        values = payload.get("values") or []
        points = []
        for item in values:
            close = item.get("close")
            dt_text = item.get("datetime")
            if close is None or not dt_text:
                continue
            dt = datetime.fromisoformat(dt_text).replace(tzinfo=CN_TZ)
            points.append((dt, float(close)))
        points.sort(key=lambda item: item[0])
        if not points:
            raise ValueError("no valid close points")

        latest_dt, latest = points[-1]
        cutoff = latest_dt.timestamp() - 24 * 3600
        prior_candidates = [p for p in points if p[0].timestamp() <= cutoff]
        prior = prior_candidates[-1][1] if prior_candidates else points[0][1]
        last_24h = [p[1] for p in points if p[0].timestamp() >= cutoff]

        return Quote(
            name=name,
            symbol=symbol,
            value=float(latest),
            change_24h=float(latest - prior),
            high_24h=float(max(last_24h)) if last_24h else None,
            low_24h=float(min(last_24h)) if last_24h else None,
            source=f"Twelve Data time_series ({symbol}, {interval})",
            updated_at=latest_dt,
        )
    except Exception as exc:
        return Quote(name=name, symbol=symbol, value=None, change_24h=None, high_24h=None, low_24h=None, source=url, error=str(exc))


def fetch_treasury_10y() -> Quote:
    year = now_cn().strftime("%Y")
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )
    try:
        resp = http_get(url, timeout=25)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        points = []
        for row in rows:
            date_text = row.get("Date") or ""
            value_text = row.get("10 Yr") or ""
            try:
                dt = datetime.strptime(date_text, "%m/%d/%Y").replace(tzinfo=CN_TZ)
                value = float(value_text)
            except ValueError:
                continue
            if dt.date() <= now_cn().date():
                points.append((dt, value))
        points.sort(key=lambda item: item[0])
        if not points:
            raise ValueError("no valid 10Y yield rows")

        latest_dt, latest = points[-1]
        prior = points[-2][1] if len(points) >= 2 else latest
        recent = [value for _, value in points[-5:]]
        return Quote(
            name="US 10Y Treasury Yield",
            symbol="10Y Treasury",
            value=latest,
            change_24h=latest - prior,
            high_24h=max(recent) if recent else None,
            low_24h=min(recent) if recent else None,
            source="U.S. Treasury daily treasury rates CSV",
            updated_at=latest_dt,
        )
    except Exception as exc:
        return Quote(name="US 10Y Treasury Yield", symbol="10Y Treasury", value=None, change_24h=None, high_24h=None, low_24h=None, source=url, error=str(exc))


def parse_fred_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CN_TZ)


def fetch_fred_series(series_id: str, name: str) -> Quote:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
    try:
        resp = http_get(url, timeout=20)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        points = []
        for row in rows:
            value_text = (row.get(series_id) or "").strip()
            date_text = (row.get("observation_date") or "").strip()
            if not value_text or value_text == "." or not date_text:
                continue
            try:
                dt = parse_fred_date(date_text)
                value = float(value_text)
            except ValueError:
                continue
            if dt.date() <= now_cn().date():
                points.append((dt, value))
        points.sort(key=lambda item: item[0])
        if not points:
            raise ValueError("no valid FRED rows")

        latest_dt, latest = points[-1]
        prior = points[-2][1] if len(points) >= 2 else latest
        recent = [value for _, value in points[-5:]]
        return Quote(
            name=name,
            symbol=series_id,
            value=latest,
            change_24h=latest - prior,
            high_24h=max(recent) if recent else None,
            low_24h=min(recent) if recent else None,
            source=f"FRED CSV ({series_id})",
            updated_at=latest_dt,
        )
    except Exception as exc:
        return Quote(name=name, symbol=series_id, value=None, change_24h=None, high_24h=None, low_24h=None, source=url, error=str(exc))


def first_usable_quote(quotes: list[Quote], *, require_recent: bool = False) -> Quote:
    fallback = quotes[0] if quotes else Quote("Unknown", "Unknown", None, None, None, None, "none")
    for quote in quotes:
        if quote.value is None:
            continue
        if require_recent:
            age = quote_age_minutes(quote)
            if age is None or age > 90:
                fallback = quote
                continue
        return quote
    return fallback


def quote_status_note(quote: Quote, *, label: str = "行情") -> str:
    if quote.value is not None:
        age = quote_age_minutes(quote)
        if is_market_closed() and (age is None or age > 90):
            return "周末休市，显示最近收盘价"
        if age is not None and age > 90:
            return "数据偏旧"
        return "正常"

    error = (quote.error or "").lower()
    if is_market_closed():
        return "周末休市，无新报价"
    if "missing twelve_data_api_key" in error:
        return "未配置 Twelve Data Key"
    if "403" in error or "forbidden" in error:
        return "数据源拒绝访问"
    if "timed out" in error or "timeout" in error:
        return "数据源超时"
    return f"{label}暂不可用"


def fmt_quote_value(quote: Quote, *, unit: str = "") -> str:
    if quote.value is None:
        return quote_status_note(quote)
    suffix = f" {unit}" if unit else ""
    return f"{fmt(quote.value)}{suffix}"


def fetch_news() -> list[str]:
    query = urllib.parse.quote("gold price dollar yields Fed nonfarm payrolls when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = http_get(url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = []
        for item in root.findall(".//item")[:6]:
            title = item.findtext("title") or ""
            if title:
                items.append(title.strip())
        return items
    except Exception:
        return []


IMPORTANT_EVENT_KEYWORDS = [
    "nonfarm",
    "payroll",
    "unemployment",
    "cpi",
    "consumer price",
    "pce",
    "ppi",
    "fomc",
    "fed",
    "powell",
    "jobless claims",
    "gdp",
    "retail sales",
    "ism",
    "pmi",
    "jolts",
    "adp",
    "durable goods",
    "consumer confidence",
    "treasury",
]


def clean_html_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def event_importance(event: str, country: str) -> str:
    text = event.lower()
    if country.upper() == "US" and any(keyword in text for keyword in IMPORTANT_EVENT_KEYWORDS):
        return "高"
    if country.upper() in {"US", "EU", "GB", "JP", "CN"}:
        return "中"
    return "低"


def fetch_economic_calendar() -> list[CalendarEvent]:
    if is_weekend_cn():
        return [
            CalendarEvent(
                event="周末，通常没有主要美国财经数据",
                country="US",
                time_text="-",
                period="-",
                actual="-",
                expected="-",
                prior="-",
                importance="低",
            )
        ]

    today = now_cn().strftime("%Y-%m-%d")
    url = f"https://finance.yahoo.com/calendar/economic?day={today}"
    try:
        resp = http_get(url, timeout=20)
        resp.raise_for_status()
        html = resp.text
        table_idx = html.find('data-testid="data-table-v2"')
        if table_idx >= 0:
            html = html[table_idx:]

        events: list[CalendarEvent] = []
        for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.S | re.I)
            if len(cells) < 7:
                continue
            values = [clean_html_text(cell) for cell in cells[:7]]
            if not values[0] or values[0].lower() == "event":
                continue
            country = values[1].upper()
            event = values[0]
            importance = event_importance(event, country)
            events.append(
                CalendarEvent(
                    event=event,
                    country=country,
                    time_text=values[2],
                    period=values[3],
                    actual=values[4],
                    expected=values[5],
                    prior=values[6],
                    importance=importance,
                )
            )

        important = [event for event in events if event.importance == "高"]
        medium_us = [event for event in events if event.importance == "中" and event.country == "US"]
        return (important + medium_us)[:10]
    except Exception as exc:
        return [
            CalendarEvent(
                event="财经日历抓取失败",
                country="-",
                time_text="-",
                period="-",
                actual="-",
                expected="-",
                prior="-",
                importance="未知",
            )
        ]


def calendar_to_dict(event: CalendarEvent) -> dict[str, str]:
    return {
        "event": event.event,
        "country": event.country,
        "time_text": event.time_text,
        "period": event.period,
        "actual": event.actual,
        "expected": event.expected,
        "prior": event.prior,
        "importance": event.importance,
    }


def news_risk_flags(news: list[str]) -> list[str]:
    text = " ".join(news).lower()
    flags = []
    checks = [
        ("美联储/鲍威尔相关消息", ["fed", "fomc", "powell", "rate cut", "rate hike"]),
        ("通胀数据相关消息", ["inflation", "cpi", "pce", "ppi"]),
        ("就业数据相关消息", ["payroll", "jobs", "unemployment", "jobless"]),
        ("地缘政治或避险消息", ["war", "attack", "ceasefire", "tariff", "sanction", "geopolitical"]),
        ("美元或美债剧烈波动线索", ["dollar", "treasury yields", "yields"]),
    ]
    for label, keywords in checks:
        if any(keyword in text for keyword in keywords):
            flags.append(label)
    return flags


def build_market_context(gold: Quote, dxy: Quote, tnx: Quote, gld: Quote | None) -> MarketContext:
    quotes: dict[str, Quote] = {
        "gold": gold,
        "dxy": dxy,
        "us10y": tnx,
    }
    if gld is not None:
        quotes["gld"] = gld

    extra_fetchers = {
        "real_10y": lambda: fetch_fred_series("DFII10", "10Y Real Yield"),
        "fred_usd": lambda: fetch_fred_series("DTWEXBGS", "Broad US Dollar Index"),
        "fred_10y": lambda: fetch_fred_series("DGS10", "10Y Treasury Yield FRED"),
        "vix": lambda: fetch_best_market_quote("^VIX", "VIX Volatility Index", twelvedata_symbols=["VIX"]),
        "sp500": lambda: fetch_best_market_quote("^GSPC", "S&P 500", twelvedata_symbols=["SPX", "SPY"]),
        "silver": lambda: fetch_best_market_quote("SI=F", "Silver Futures", twelvedata_symbols=["XAG/USD", "XAGUSD"]),
    }
    for key, fetcher in extra_fetchers.items():
        try:
            quotes[key] = fetcher()
        except Exception as exc:
            quotes[key] = Quote(key, key, None, None, None, None, "supplemental fetch", error=str(exc))

    prior_evening = load_prior_evening_snapshot()
    notes = []
    usable_count = sum(1 for quote in quotes.values() if quote.value is not None)
    notes.append(f"后台参考数据共 {len(quotes)} 项，可用 {usable_count} 项。")
    if is_market_closed():
        notes.append("当前是周末休市时段，若没有新报价，属于正常市场状态。")
    if not os.getenv("TWELVE_DATA_API_KEY"):
        notes.append("Twelve Data Key未配置，美元指数、GLD、VIX等备用行情会受影响。")
    if quotes.get("real_10y") and quotes["real_10y"].value is not None:
        notes.append("已纳入实际利率，用来判断黄金中期压力。")
    if quotes.get("vix") and quotes["vix"].value is not None:
        notes.append("已纳入VIX，用来判断市场恐慌程度。")
    if quotes.get("silver") and quotes["silver"].value is not None:
        notes.append("已纳入白银，用来观察贵金属联动。")
    if prior_evening:
        notes.append("已读取昨晚市场快照，用来判断隔夜变化。")
    return MarketContext(quotes=quotes, notes=notes, prior_evening=prior_evening)


def load_prior_evening_snapshot() -> dict[str, object] | None:
    current_date = now_cn().date()
    for days_back in (1, 2, 3):
        date_key = (current_date - timedelta(days=days_back)).strftime("%Y-%m-%d")
        year = date_key[:4]
        data_path = Path("data") / year / f"{date_key}.json"
        if not data_path.exists():
            continue
        try:
            snapshot = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        evening = snapshot.get("evening_snapshot")
        if isinstance(evening, dict):
            evening["source_date"] = date_key
            return evening
    return None


def quote_value_from_snapshot(snapshot: dict[str, object], key: str) -> float | None:
    quotes = snapshot.get("quotes")
    if not isinstance(quotes, dict):
        return None
    item = quotes.get(key)
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def pct(change: float | None, value: float | None) -> float | None:
    if change is None or value is None:
        return None
    base = value - change
    if not base:
        return None
    return change / base * 100


def fmt(value: float | None, digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "缺失"
    return f"{value:.{digits}f}"


def fmt_time(value: datetime | None) -> str:
    if value is None:
        return "缺失"
    return value.strftime("%Y-%m-%d %H:%M CST")


def quote_age_minutes(quote: Quote) -> float | None:
    if quote.updated_at is None:
        return None
    return (now_cn() - quote.updated_at).total_seconds() / 60


def round_to_step(value: float, step: int = 5) -> int:
    return int(round(value / step) * step)


def build_levels(gold: Quote) -> dict[str, str]:
    if gold.value is None:
        return {
            "support_near": "待确认",
            "support_key": "待确认",
            "resistance_near": "待确认",
            "resistance_key": "待确认",
        }

    price = gold.value
    low = gold.low_24h or price - 30
    high = gold.high_24h or price + 30
    support_near = min(round_to_step(price - 15), round_to_step(low))
    support_key = round_to_step(price - 45)
    resistance_near = max(round_to_step(price + 15), round_to_step(high))
    resistance_key = max(round_to_step(price + 45), resistance_near + 20)
    return {
        "support_near": f"{support_near}-{support_near + 10}",
        "support_key": f"{support_key}-{support_key + 10}",
        "resistance_near": f"{resistance_near}-{resistance_near + 10}",
        "resistance_key": f"{resistance_key}-{resistance_key + 15}",
    }


def build_driver_scores(
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    news: list[str] | None = None,
    calendar_events: list[CalendarEvent] | None = None,
    market_context: MarketContext | None = None,
) -> list[DriverScore]:
    news = news or []
    calendar_events = calendar_events or []
    rows: list[DriverScore] = []

    gold_age = quote_age_minutes(gold)
    gold_pct = pct(gold.change_24h, gold.value)
    if gold.value is None:
        if is_market_closed():
            rows.append(DriverScore("金价", "周末休市，无新报价", "不能交易", 0, 2, "周末没有连续报价，等周一开盘后再判断。"))
        else:
            rows.append(DriverScore("金价", quote_status_note(gold, label="金价"), "不能判断", 0, 3, "没有最新金价时，不给交易建议。"))
    elif gold_age is None or gold_age > 90:
        if is_market_closed():
            rows.append(DriverScore("金价", f"{fmt(gold.value)}，周末休市，最近收盘价", "不能交易", 0, 2, "周末价格不是实时跳动，不能按它制定新交易。"))
        else:
            rows.append(DriverScore("金价", f"{fmt(gold.value)}，但数据偏旧", "风险升高", 0, 3, "价格不够新，点位可能已经失效。"))
    elif gold_pct is None:
        rows.append(DriverScore("金价", f"{fmt(gold.value)}", "方向不明", 0, 1, "只能看到现价，看不到过去24小时变化。"))
    elif gold_pct >= 0.8:
        rows.append(DriverScore("金价", f"近24小时 +{fmt(gold_pct)}%", "支持上涨", 2, 0, "价格自己在明显往上，短线买盘较强。"))
    elif gold_pct >= 0.4:
        rows.append(DriverScore("金价", f"近24小时 +{fmt(gold_pct)}%", "略支持上涨", 1, 0, "价格在往上，但还没有强到可以追高。"))
    elif gold_pct <= -0.8:
        rows.append(DriverScore("金价", f"近24小时 {fmt(gold_pct)}%", "支持下跌", -2, 1, "价格明显往下，新手不要急着抄底。"))
    elif gold_pct <= -0.4:
        rows.append(DriverScore("金价", f"近24小时 {fmt(gold_pct)}%", "略支持下跌", -1, 0, "价格偏弱，买入要等更清楚的止跌信号。"))
    else:
        rows.append(DriverScore("金价", f"近24小时 {fmt(gold_pct)}%", "方向不明", 0, 0, "金价变化不大，不能单靠价格判断。"))

    dxy_pct = pct(dxy.change_24h, dxy.value)
    if dxy.value is None:
        rows.append(DriverScore("美元", quote_status_note(dxy, label="美元指数"), "不能判断", 0, 1, "美元是黄金的重要对手盘，缺失时信心下降。"))
    elif dxy_pct is None:
        rows.append(DriverScore("美元", f"{fmt(dxy.value)}", "方向不明", 0, 0, "只有美元现价，看不出短线变化。"))
    elif dxy_pct <= -0.3:
        rows.append(DriverScore("美元", f"近24小时 {fmt(dxy_pct)}%", "支持上涨", 2, 0, "美元明显走弱，通常有利于黄金。"))
    elif dxy_pct <= -0.15:
        rows.append(DriverScore("美元", f"近24小时 {fmt(dxy_pct)}%", "略支持上涨", 1, 0, "美元偏弱，对黄金有帮助。"))
    elif dxy_pct >= 0.3:
        rows.append(DriverScore("美元", f"近24小时 +{fmt(dxy_pct)}%", "支持下跌", -2, 0, "美元明显走强，黄金容易被压住。"))
    elif dxy_pct >= 0.15:
        rows.append(DriverScore("美元", f"近24小时 +{fmt(dxy_pct)}%", "略支持下跌", -1, 0, "美元偏强，黄金上行会更吃力。"))
    else:
        rows.append(DriverScore("美元", f"近24小时 {fmt(dxy_pct)}%", "方向不明", 0, 0, "美元没有给出强方向。"))

    if tnx.value is None:
        rows.append(DriverScore("美债收益率", quote_status_note(tnx, label="美债收益率"), "不能判断", 0, 1, "利率数据缺失时，不能完整判断黄金压力。"))
    elif tnx.change_24h is None:
        rows.append(DriverScore("美债收益率", f"{fmt(tnx.value)}", "方向不明", 0, 0, "只有收益率现值，看不出短线变化。"))
    elif tnx.change_24h <= -0.06:
        rows.append(DriverScore("美债收益率", f"近24小时 {fmt(tnx.change_24h)}", "支持上涨", 2, 0, "收益率明显下行，黄金压力减轻。"))
    elif tnx.change_24h <= -0.03:
        rows.append(DriverScore("美债收益率", f"近24小时 {fmt(tnx.change_24h)}", "略支持上涨", 1, 0, "收益率回落，对黄金有帮助。"))
    elif tnx.change_24h >= 0.06:
        rows.append(DriverScore("美债收益率", f"近24小时 +{fmt(tnx.change_24h)}", "支持下跌", -2, 0, "收益率明显上行，黄金压力加大。"))
    elif tnx.change_24h >= 0.03:
        rows.append(DriverScore("美债收益率", f"近24小时 +{fmt(tnx.change_24h)}", "略支持下跌", -1, 0, "收益率上行，黄金容易承压。"))
    else:
        rows.append(DriverScore("美债收益率", f"近24小时 {fmt(tnx.change_24h)}", "方向不明", 0, 0, "利率端暂时没有明显方向。"))

    context_quotes = market_context.quotes if market_context else {}
    prior_evening = market_context.prior_evening if market_context else None
    if prior_evening:
        evening_gold = quote_value_from_snapshot(prior_evening, "gold")
        if evening_gold and gold.value is not None:
            overnight_change = gold.value - evening_gold
            overnight_pct = overnight_change / evening_gold * 100
            risk = 1 if abs(overnight_pct) >= 0.8 else 0
            if overnight_pct >= 0.3:
                rows.append(DriverScore("隔夜金价", f"昨晚到今早 +{fmt(overnight_pct)}%", "支持上涨", 1, risk, "昨晚之后金价继续走强，说明买盘没有立刻消失。"))
            elif overnight_pct <= -0.3:
                rows.append(DriverScore("隔夜金价", f"昨晚到今早 {fmt(overnight_pct)}%", "支持下跌", -1, risk, "昨晚之后金价继续走弱，说明卖压还在。"))
            else:
                rows.append(DriverScore("隔夜金价", f"昨晚到今早 {fmt(overnight_pct)}%", "方向不明", 0, 0, "昨晚到今早变化不大，隔夜走势没有给出强信号。"))

    real_10y = context_quotes.get("real_10y")
    if real_10y and real_10y.value is not None:
        if real_10y.change_24h is not None and real_10y.change_24h <= -0.04:
            rows.append(DriverScore("实际利率", f"{fmt(real_10y.value)}，近一日 {fmt(real_10y.change_24h)}", "支持上涨", 2, 0, "实际利率下降时，黄金吸引力通常会提高。"))
        elif real_10y.change_24h is not None and real_10y.change_24h >= 0.04:
            rows.append(DriverScore("实际利率", f"{fmt(real_10y.value)}，近一日 +{fmt(real_10y.change_24h)}", "支持下跌", -2, 0, "实际利率上升时，黄金容易承压。"))
        else:
            rows.append(DriverScore("实际利率", f"{fmt(real_10y.value)}", "方向不明", 0, 0, "实际利率没有明显变化。"))
    elif real_10y:
        rows.append(DriverScore("实际利率", quote_status_note(real_10y, label="实际利率"), "不能判断", 0, 1, "实际利率缺失时，中期判断信心下降。"))

    gld = context_quotes.get("gld")
    if gld and gld.value is not None:
        gld_pct = pct(gld.change_24h, gld.value)
        if gld_pct is not None and gld_pct >= 0.7:
            rows.append(DriverScore("黄金ETF价格", f"GLD近24小时 +{fmt(gld_pct)}%", "支持上涨", 1, 0, "黄金ETF同步走强，说明资金情绪不差。"))
        elif gld_pct is not None and gld_pct <= -0.7:
            rows.append(DriverScore("黄金ETF价格", f"GLD近24小时 {fmt(gld_pct)}%", "支持下跌", -1, 0, "黄金ETF同步走弱，说明资金情绪偏弱。"))
        else:
            rows.append(DriverScore("黄金ETF价格", f"GLD近24小时 {fmt(gld_pct)}%", "方向不明", 0, 0, "黄金ETF没有给出强信号。"))

    silver = context_quotes.get("silver")
    if silver and silver.value is not None:
        silver_pct = pct(silver.change_24h, silver.value)
        if silver_pct is not None and silver_pct >= 1.0:
            rows.append(DriverScore("白银联动", f"白银近24小时 +{fmt(silver_pct)}%", "支持上涨", 1, 0, "白银同步走强，贵金属板块情绪较好。"))
        elif silver_pct is not None and silver_pct <= -1.0:
            rows.append(DriverScore("白银联动", f"白银近24小时 {fmt(silver_pct)}%", "支持下跌", -1, 0, "白银同步走弱，贵金属板块情绪偏弱。"))
        else:
            rows.append(DriverScore("白银联动", f"白银近24小时 {fmt(silver_pct)}%", "方向不明", 0, 0, "白银没有给出明显联动信号。"))

    vix = context_quotes.get("vix")
    sp500 = context_quotes.get("sp500")
    vix_pct = pct(vix.change_24h, vix.value) if vix else None
    sp500_pct = pct(sp500.change_24h, sp500.value) if sp500 else None
    if vix and vix.value is not None:
        if (vix_pct is not None and vix_pct >= 5) or vix.value >= 25:
            rows.append(DriverScore("市场恐慌", f"VIX {fmt(vix.value)}，近24小时 {fmt(vix_pct)}%", "避险支持上涨", 1, 1, "市场恐慌上升时，黄金可能获得避险买盘，但波动也会变大。"))
        elif vix_pct is not None and vix_pct <= -5:
            rows.append(DriverScore("市场恐慌", f"VIX {fmt(vix.value)}，近24小时 {fmt(vix_pct)}%", "风险降低", 0, -1, "恐慌回落，黄金的避险买盘可能减弱。"))
        else:
            rows.append(DriverScore("市场恐慌", f"VIX {fmt(vix.value)}", "影响不明显", 0, 0, "恐慌指数没有明显变化。"))
    if sp500 and sp500.value is not None and sp500_pct is not None:
        if sp500_pct <= -1.0:
            rows.append(DriverScore("风险资产", f"标普500近24小时 {fmt(sp500_pct)}%", "避险支持上涨", 1, 1, "股市明显下跌时，黄金可能受避险需求支撑。"))
        elif sp500_pct >= 1.0:
            rows.append(DriverScore("风险资产", f"标普500近24小时 +{fmt(sp500_pct)}%", "避险需求减弱", 0, 0, "股市走强时，避险买盘可能下降。"))

    high_events = [event for event in calendar_events if event.importance == "高"]
    if high_events:
        names = "、".join(event.event for event in high_events[:3])
        rows.append(DriverScore("财经日历", names, "风险升高", 0, 2, "重要数据前后容易快速拉升或跳水，新手少做。"))
    elif any(event.event == "财经日历抓取失败" for event in calendar_events):
        rows.append(DriverScore("财经日历", "抓取失败", "风险升高", 0, 1, "今天是否有重要数据需要手动确认。"))
    elif calendar_events:
        rows.append(DriverScore("财经日历", f"{len(calendar_events)}个需留意事件", "小风险", 0, 1, "有事件但暂未识别到最高风险数据。"))
    else:
        rows.append(DriverScore("财经日历", "未发现高影响事件", "风险较低", 0, 0, "事件面暂时没有明显拦路项。"))

    flags = news_risk_flags(news)
    if not news:
        rows.append(DriverScore("新闻", "缺失", "不能判断", 0, 1, "新闻抓取不到时，不要过度相信方向判断。"))
    elif flags:
        direction = 1 if "地缘政治或避险消息" in flags else 0
        effect = "避险支持上涨" if direction else "风险升高"
        rows.append(DriverScore("新闻", "、".join(flags[:3]), effect, direction, min(len(flags), 2), "新闻会让价格突然变化，适合降低仓位而不是追单。"))
    else:
        rows.append(DriverScore("新闻", "未发现强风险词", "影响不明显", 0, 0, "新闻面暂时没有明显推力。"))

    return rows


def judge(
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    news: list[str] | None = None,
    calendar_events: list[CalendarEvent] | None = None,
    market_context: MarketContext | None = None,
) -> TradeDecision:
    gold_age = quote_age_minutes(gold)
    news = news or []
    calendar_events = calendar_events or []
    driver_scores = build_driver_scores(gold, dxy, tnx, news, calendar_events, market_context)
    direction_score = sum(item.direction_score for item in driver_scores)
    risk_score = sum(item.risk_score for item in driver_scores)
    reasons = [item.explanation for item in driver_scores if item.direction_score or item.risk_score]

    high_events = [event for event in calendar_events if event.importance == "高"]
    if high_events:
        names = "、".join(event.event for event in high_events[:3])
        reasons.append(f"今天有高影响财经事件：{names}。事件前后容易突然拉升或跳水，新手不要追单。")

    risk_flags = news_risk_flags(news)
    if risk_flags:
        reasons.append("新闻面出现风险线索：" + "、".join(risk_flags[:3]) + "。需要降低仓位或等待价格稳定。")

    if is_market_closed() and (gold.value is None or gold_age is None or gold_age > 90):
        return TradeDecision(
            headline="C 只观察：周末休市，等开盘后再判断",
            action="周末没有连续报价，不开新仓；等周一开盘后再看最新金价。",
            probabilities={"周末休市，先观察": 80, "周一开盘后重新判断": 20},
            reasons=reasons or ["当前是周末休市时段，缺少实时金价不等于行情异常。"],
            confidence="低",
            trade_grade="C",
            trade_grade_text="只观察",
            direction_score=direction_score,
            risk_score=risk_score,
            driver_scores=driver_scores,
        )

    if direction_score >= 2:
        stance = "上涨机会更大"
        probs = {"上涨机会": 50, "方向不清楚": 30, "下跌风险": 20}
    elif direction_score <= -2:
        stance = "下跌风险更大"
        probs = {"下跌风险": 50, "方向不清楚": 30, "上涨机会": 20}
    else:
        stance = "方向不清楚"
        probs = {"方向不清楚": 50, "上涨机会": 25, "下跌风险": 25}

    if risk_score >= 3:
        if direction_score >= 2:
            probs = {"上涨机会": 40, "方向不清楚": 40, "下跌风险": 20}
        elif direction_score <= -2:
            probs = {"下跌风险": 40, "方向不清楚": 40, "上涨机会": 20}
        else:
            probs = {"方向不清楚": 60, "上涨机会": 20, "下跌风险": 20}

    if gold_age is None or gold_age > 90:
        return TradeDecision(
            headline="C 只观察：行情不够新，先不要交易",
            action="先观望；等拿到最新金价后再判断。",
            probabilities={"先观望": 70, "拿到最新行情后再判断": 30},
            reasons=reasons,
            confidence="低",
            trade_grade="C",
            trade_grade_text="只观察",
            direction_score=direction_score,
            risk_score=risk_score,
            driver_scores=driver_scores,
        )

    if gold.value is None:
        return TradeDecision(
            headline="C 只观察：没有可用金价",
            action="没有最新价格时，不开新仓。",
            probabilities={"先观望": 80, "拿到最新行情后再判断": 20},
            reasons=reasons,
            confidence="低",
            trade_grade="C",
            trade_grade_text="只观察",
            direction_score=direction_score,
            risk_score=risk_score,
            driver_scores=driver_scores,
        )

    if high_events and abs(direction_score) < 3:
        trade_grade = "C"
        trade_grade_text = "只观察"
    elif abs(direction_score) >= 4 and risk_score <= 1:
        trade_grade = "A"
        trade_grade_text = "可交易"
    elif abs(direction_score) >= 2 and risk_score <= 3:
        trade_grade = "B"
        trade_grade_text = "轻仓"
    else:
        trade_grade = "C"
        trade_grade_text = "只观察"

    if trade_grade == "A" and stance == "上涨机会更大":
        action = "可以按计划等低位买，但只在价格稳住后动手，已经涨远就不追。"
    elif trade_grade == "A" and stance == "下跌风险更大":
        action = "不建议新买；已有多单优先保护利润，等待价格重新转强。"
    elif trade_grade == "B" and stance == "上涨机会更大":
        action = "只允许小仓位试单，必须等价格到买入观察区并止住下跌。"
    elif trade_grade == "B" and stance == "下跌风险更大":
        action = "轻仓或不做；已有多单先设好离场线，不要补仓摊低成本。"
    else:
        action = "先观望；没有到计划位置，或者你说不清买入理由，就不交易。"

    headline = f"{trade_grade} {trade_grade_text}：{stance}"

    if trade_grade == "A" and not high_events:
        confidence = "中高"
    elif trade_grade == "B":
        confidence = "中"
    elif high_events:
        confidence = "低"
    else:
        confidence = "中低"

    return TradeDecision(
        headline=headline,
        action=action,
        probabilities=probs,
        reasons=reasons,
        confidence=confidence,
        trade_grade=trade_grade,
        trade_grade_text=trade_grade_text,
        direction_score=direction_score,
        risk_score=risk_score,
        driver_scores=driver_scores,
    )


def indicator_status(label: str, value: str, impact: str, note: str) -> str:
    return f"- {label}：{value}｜影响：{impact}｜小白解释：{note}"


def build_market_checklist(gold: Quote, dxy: Quote, tnx: Quote) -> list[str]:
    gold_pct = pct(gold.change_24h, gold.value)
    dxy_pct = pct(dxy.change_24h, dxy.value)
    rows = []

    if gold_pct is None:
        rows.append(indicator_status("金价动能", "缺失", "未知", "没有价格就不要交易，只观察。"))
    elif gold_pct > 0.4:
        rows.append(indicator_status("金价动能", f"近24小时 +{fmt(gold_pct)}%", "帮助上涨", "价格自己在往上走，说明买的人更多。"))
    elif gold_pct < -0.4:
        rows.append(indicator_status("金价动能", f"近24小时 {fmt(gold_pct)}%", "不利上涨", "价格自己在往下走，先别急着抄底。"))
    else:
        rows.append(indicator_status("金价动能", f"近24小时 {fmt(gold_pct)}%", "方向不明显", "没有明显方向，等关键价位。"))

    if dxy_pct is None:
        rows.append(indicator_status("美元指数", "缺失", "未知", "美元数据缺失时，结论信心下降。"))
    elif dxy_pct < -0.15:
        rows.append(indicator_status("美元指数", f"{fmt(dxy.value)}，近24小时 {fmt(dxy_pct)}%", "帮助上涨", "黄金用美元计价，美元弱通常更利于黄金。"))
    elif dxy_pct > 0.15:
        rows.append(indicator_status("美元指数", f"{fmt(dxy.value)}，近24小时 +{fmt(dxy_pct)}%", "不利上涨", "美元强时，黄金上涨容易受压。"))
    else:
        rows.append(indicator_status("美元指数", f"{fmt(dxy.value)}，近24小时 {fmt(dxy_pct)}%", "影响不明显", "美元没有给出强方向。"))

    if tnx.change_24h is None:
        rows.append(indicator_status("美债收益率", "缺失", "未知", "收益率是黄金的重要压力源，缺失时要轻仓。"))
    elif tnx.change_24h < -0.03:
        rows.append(indicator_status("美债收益率", f"{fmt(tnx.value)}，近24小时 {fmt(tnx.change_24h)}", "帮助上涨", "收益率下行，持有黄金的机会成本下降。"))
    elif tnx.change_24h > 0.03:
        rows.append(indicator_status("美债收益率", f"{fmt(tnx.value)}，近24小时 +{fmt(tnx.change_24h)}", "不利上涨", "收益率上行时，黄金容易承压。"))
    else:
        rows.append(indicator_status("美债收益率", f"{fmt(tnx.value)}，近24小时 {fmt(tnx.change_24h)}", "影响不明显", "利率端暂时没有明显方向。"))

    return rows


def build_calendar_text(events: list[CalendarEvent]) -> str:
    if not events:
        return "- 今天暂未抓到重要财经日历。"
    rows = [
        "| 时间 | 事件 | 处理 |",
        "| --- | --- | --- |",
    ]
    for event in events[:8]:
        if event.event == "财经日历抓取失败":
            rows.append("| - | 财经日历抓取失败 | 手动留意事件风险 |")
            continue
        impact = "新手先观望" if event.importance == "高" else "留意即可"
        rows.append(f"| {event.time_text} | {event.country}｜{event.event}｜{event.importance} | {impact} |")
    return "\n".join(rows)


def build_driver_scores_text(decision: TradeDecision, *, max_rows: int = 5) -> str:
    rows = [
        "| 因素 | 结论 | 分数/解释 |",
        "| --- | --- | --- |",
    ]
    ranked = sorted(
        decision.driver_scores,
        key=lambda item: (abs(item.direction_score) + item.risk_score, abs(item.direction_score)),
        reverse=True,
    )
    for item in ranked[:max_rows]:
        sign = "+" if item.direction_score > 0 else ""
        rows.append(f"| {item.factor} | {item.effect} | 方向{sign}{item.direction_score} / 风险{item.risk_score}；{item.explanation} |")
    shown_rows = len(rows) - 2
    hidden_count = max(0, len(decision.driver_scores) - shown_rows)
    if hidden_count:
        rows.append(f"| 其他后台数据 | 已参与评分 | 另有 {hidden_count} 项，完整记录保存到每日归档 |")
    return "\n".join(rows)


def build_core_logic(decision: TradeDecision) -> str:
    useful = [item for item in decision.driver_scores if item.direction_score or item.risk_score]
    if not useful:
        return "今天没有看到足够强的上涨或下跌理由，所以先按观察处理。"
    parts = []
    for item in useful[:4]:
        parts.append(f"{item.factor}：{item.effect}")
    return "；".join(parts)


def build_daily_view(decision: TradeDecision) -> str:
    if "周末休市" in decision.headline:
        return "周末休市，先观察"
    if decision.risk_score >= 3 and decision.trade_grade == "C":
        return "事件风险主导，先观察"
    if "上涨机会" in decision.headline:
        return "上涨机会更大"
    if "下跌风险" in decision.headline:
        return "下跌风险更大"
    return "方向不清楚"


def build_conclusion_table(decision: TradeDecision, core_logic: str) -> str:
    if "周末休市" in decision.headline:
        max_risk = "周末没有连续报价，旧价格不能直接拿来交易。"
    else:
        max_risk = "今天有重要数据或新闻风险，价格可能突然大幅波动。" if decision.risk_score >= 2 else "暂未发现特别强的事件风险，但不能满仓。"
    return "\n".join(
        [
            "| 项目 | 今天的答案 | 小白解释 |",
            "| --- | --- | --- |",
            f"| 今日黄金观点 | {build_daily_view(decision)} | 这不是保证涨跌，只是未来24小时更值得防哪边。 |",
            f"| 今日交易等级 | {decision.trade_grade}，{decision.trade_grade_text} | A可以按计划做；B只能小仓位；C最好不做。 |",
            f"| 核心逻辑 | {core_logic} | 只看真正会影响金价的几个变量。 |",
            f"| 最大风险 | {max_risk} | 风险高时，宁愿错过，也不要追进去。 |",
            f"| 操作原则 | {decision.action} | 没到计划价格，不交易；看不懂原因，也不交易。 |",
        ]
    )


def build_score_summary(decision: TradeDecision) -> str:
    if decision.direction_score >= 4:
        direction_text = "上涨理由比较集中"
    elif decision.direction_score >= 2:
        direction_text = "上涨理由略多"
    elif decision.direction_score <= -4:
        direction_text = "下跌理由比较集中"
    elif decision.direction_score <= -2:
        direction_text = "下跌理由略多"
    else:
        direction_text = "方向理由不够集中"

    if decision.risk_score >= 4:
        risk_text = "风险偏高，新手不适合主动交易"
    elif decision.risk_score >= 2:
        risk_text = "有明显风险，只能轻仓或观察"
    else:
        risk_text = "风险暂时可控，但仍要止损"

    return "\n".join(
        [
            "| 项目 | 数值 | 含义 |",
            "| --- | --- | --- |",
            f"| 总方向分 | {decision.direction_score} | {direction_text} |",
            f"| 总风险分 | {decision.risk_score} | {risk_text} |",
        ]
    )


def build_context_notes_table(notes: list[str]) -> str:
    rows = [
        "| 后台数据 | 状态 |",
        "| --- | --- |",
    ]
    if not notes:
        rows.append("| 参考数据 | 暂未扩展 |")
        return "\n".join(rows)
    for idx, note in enumerate(notes[:4], start=1):
        rows.append(f"| {idx} | {note} |")
    if len(notes) > 4:
        rows.append(f"| 其他 | 另有 {len(notes) - 4} 条说明已保存到归档 |")
    return "\n".join(rows)


def build_market_snapshot(gold: Quote, dxy: Quote, tnx: Quote, gld: Quote | None = None) -> str:
    gold_pct = pct(gold.change_24h, gold.value)
    dxy_pct = pct(dxy.change_24h, dxy.value)
    rows = [
        "| 指标 | 当前/变化 | 用途 |",
        "| --- | --- | --- |",
        f"| 国际黄金 | {fmt_quote_value(gold, unit='美元/盎司')}；24小时 {fmt(gold.change_24h)} 美元，约 {fmt(gold_pct)}% | 核心交易价格 |",
        f"| 金价更新时间 | {fmt_time(gold.updated_at)}；{quote_status_note(gold, label='金价')} | 判断数据是否新 |",
        f"| 美元指数 | {fmt_quote_value(dxy)}；24小时 {fmt(dxy_pct)}% | 美元强通常压黄金 |",
        f"| 美国10年期收益率 | {fmt_quote_value(tnx)}；24小时 {fmt(tnx.change_24h)} | 利率上行通常压黄金 |",
    ]
    if gld is not None:
        gld_pct = pct(gld.change_24h, gld.value)
        rows.append(f"| GLD黄金ETF | {fmt_quote_value(gld)}；24小时 {fmt(gld_pct)}% | 海外黄金资金情绪 |")
    if is_market_closed() and gold.value is None:
        data_quality = "周末休市，金价无新报价"
    else:
        data_quality = "完整" if not any(q.value is None for q in [gold, dxy, tnx]) else "部分缺失"
    rows.append(f"| 数据完整性 | {data_quality} | 缺失时降低仓位和信心 |")
    return "\n".join(rows)


def build_trade_rules(decision: TradeDecision, levels: dict[str, str], gold: Quote) -> tuple[str, str, str]:
    if is_market_closed() and (gold.value is None or quote_age_minutes(gold) is None or quote_age_minutes(gold) > 90):
        return (
            "不能交易：周末休市，没有连续报价。",
            "等周一开盘后重新生成报告，再看是否有计划价格。",
            "周末不设置新的止损和目标；已有仓位只复盘，不追加。",
        )
    if gold.value is None:
        return (
            "不能交易：没有可用金价。",
            "等下一次报告或手动确认最新金价。",
            "没有最新价格时，不设置止损和目标。",
        )
    if decision.trade_grade == "C":
        return (
            "今天不主动交易，只记录关键价位，等待更清楚的机会。",
            f"如果价格涨到 {levels['resistance_near']}，没有提前持仓就不追；已有持仓可以减一部分。",
            f"如果价格跌破 {levels['support_key']}，今天不再找买点。",
        )

    if "上涨机会" in decision.headline:
        return (
            f"可以只等低位买：价格回到 {levels['support_near']} 附近，并且不再继续跌，才考虑小仓位。",
            f"第一目标看 {levels['resistance_near']}；到了先减仓，不贪。",
            f"如果跌破 {levels['support_key']}，说明判断错了，先离场。",
        )
    if "下跌风险" in decision.headline:
        return (
            "今天不建议新买黄金。",
            f"已有多单可以把 {levels['support_near']} 当作保护线，跌破就先减仓或离场。",
            f"只有重新站回 {levels['resistance_near']} 上方，才说明下跌风险缓和。",
        )
    return (
        "今天默认观望，不主动找交易。",
        f"除非价格跌到 {levels['support_near']} 并明显稳住，或涨过 {levels['resistance_near']} 后没有马上跌回。",
        f"如果进场，跌破 {levels['support_key']} 就退出；没到计划位置就不做。",
    )


def build_levels_table(levels: dict[str, str]) -> str:
    return "\n".join(
        [
            "| 价位 | 区间 | 怎么用 |",
            "| --- | --- | --- |",
            f"| 买入观察价 | {levels['support_near']} | 只观察是否止跌，不是到价就买 |",
            f"| 认错位置 | {levels['support_key']} | 跌破说明判断错了，先退出 |",
            f"| 第一目标 | {levels['resistance_near']} | 到了先减仓，不贪 |",
            f"| 第二目标 | {levels['resistance_key']} | 强势才看这里 |",
        ]
    )


def build_trade_plan_table(entry_rule: str, target_rule: str, stop_rule: str) -> str:
    return "\n".join(
        [
            "| 项目 | 规则 |",
            "| --- | --- |",
            f"| 入场 | {entry_rule} |",
            f"| 目标/减仓 | {target_rule} |",
            f"| 止损/认错 | {stop_rule} |",
        ]
    )


def build_probability_table(probabilities: dict[str, int]) -> str:
    rows = [
        "| 情况 | 概率 |",
        "| --- | --- |",
    ]
    for label, value in probabilities.items():
        rows.append(f"| {label} | {value}% |")
    return "\n".join(rows)


def build_trade_scenarios(decision: TradeDecision, levels: dict[str, str], gold: Quote) -> str:
    if gold.value is None or decision.trade_grade == "C":
        return "\n".join(
            [
                "| 情景 | 处理 |",
                "| --- | --- |",
                "| 价格下跌到买入观察区 | 先看，不急着买，等下一份报告或手动确认 |",
                "| 价格突然上涨 | 不追高，因为没有计划内买点 |",
                "| 价格快速下跌 | 不抄底，先保护本金 |",
            ]
        )

    if "上涨机会" in decision.headline:
        return "\n".join(
            [
                "| 情景 | 处理 |",
                "| --- | --- |",
                f"| 回到 {levels['support_near']} 且不再继续跌 | 小仓位买入 |",
                f"| 直接涨到 {levels['resistance_near']} 附近 | 没持仓不追；有持仓先减一部分 |",
                f"| 跌破 {levels['support_key']} | 取消买入想法，已买先离场 |",
            ]
        )

    if "下跌风险" in decision.headline:
        return "\n".join(
            [
                "| 情景 | 处理 |",
                "| --- | --- |",
                f"| 跌破 {levels['support_near']} | 不买，已有多单先减仓或离场 |",
                f"| 重新涨回 {levels['resistance_near']} 上方 | 下跌风险缓和，但仍先观察 |",
                "| 在中间位置来回动 | 不交易，没有便宜价也没有强势信号 |",
            ]
        )

    return "\n".join(
        [
            "| 情景 | 处理 |",
            "| --- | --- |",
            f"| 跌到 {levels['support_near']} 并稳住 | 最多小仓位试一次 |",
            f"| 涨到 {levels['resistance_near']} | 没有提前买就不追 |",
            f"| 跌破 {levels['support_key']} | 今天不再找买点 |",
        ]
    )


def build_loss_traps(decision: TradeDecision, gold: Quote, events: list[CalendarEvent]) -> list[str]:
    rows = [
        "看到价格快速上涨就追进去。",
        "价格还在继续跌，却因为觉得便宜而提前买。",
        "买错后不止损，继续加仓摊低成本。",
        "价格没到计划位置，却因为手痒交易。",
    ]
    if decision.trade_grade == "C":
        rows.insert(0, "报告已经给出只观察，但仍然强行开仓。")
    if is_market_closed():
        rows.insert(0, "周末休市还用旧价格做新决定。")
    if any(event.importance == "高" for event in events):
        rows.insert(0, "重要数据公布前后30分钟追涨追跌。")
    gold_age = quote_age_minutes(gold)
    if gold_age is None or gold_age > 90:
        rows.insert(0, "金价数据不新，还照着旧点位交易。")
    return rows


def build_no_trade_conditions(gold: Quote, events: list[CalendarEvent]) -> list[str]:
    rows = []
    gold_age = quote_age_minutes(gold)
    if is_market_closed():
        rows.append("周末休市，没有连续报价时不做新交易。")
    if gold_age is None or gold_age > 90:
        rows.append("金价不是90分钟内更新的数据。")
    if any(event.importance == "高" for event in events):
        rows.append("今天有美国高影响数据或美联储相关事件，公布前后不要追涨追跌。")
    rows.extend(
        [
            "价格在买入区和卖出区中间，没有便宜价也没有强势信号。",
            "你看不懂为什么要买，只是因为价格在动。",
            "已经连续亏损两次，当天停止交易。",
        ]
    )
    return rows


def build_simple_table(title: str, rows: list[str]) -> str:
    table = [
        f"| {title} |",
        "| --- |",
    ]
    for item in rows:
        table.append(f"| {item} |")
    return "\n".join(table)


def rules_report(
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    gld: Quote | None,
    news: list[str],
    calendar_events: list[CalendarEvent],
    market_context: MarketContext | None = None,
) -> str:
    levels = build_levels(gold)
    decision = judge(gold, dxy, tnx, news, calendar_events, market_context)
    today = now_cn().strftime("%Y-%m-%d %H:%M CST")

    news_text = "\n".join(f"- {item}" for item in news[:5]) if news else "- 暂未抓到可靠的近24小时新闻标题，需降低新闻面判断权重。"
    probs_text = build_probability_table(decision.probabilities)
    reasons_text = "\n".join(f"- {reason}" for reason in decision.reasons) if decision.reasons else "- 当前没有单一变量给出强信号，按方向不清楚处理。"
    calendar_text = build_calendar_text(calendar_events)
    entry_rule, target_rule, stop_rule = build_trade_rules(decision, levels, gold)
    levels_table = build_levels_table(levels)
    trade_plan_table = build_trade_plan_table(entry_rule, target_rule, stop_rule)
    scenarios_text = build_trade_scenarios(decision, levels, gold)
    driver_text = build_driver_scores_text(decision)
    market_snapshot = build_market_snapshot(gold, dxy, tnx, gld)
    no_trade_text = build_simple_table("直接不做的情况", build_no_trade_conditions(gold, calendar_events))
    loss_traps_text = build_simple_table("最容易亏钱的动作", build_loss_traps(decision, gold, calendar_events))
    risk_flags = news_risk_flags(news)
    news_risk_text = "、".join(risk_flags) if risk_flags else "暂未发现特别强的新闻风险词"
    core_logic = build_core_logic(decision)
    conclusion_table = build_conclusion_table(decision, core_logic)
    score_summary = build_score_summary(decision)
    context_notes = build_context_notes_table(market_context.notes if market_context else [])
    risk_control_table = build_simple_table(
        "风险控制",
        [
            "新手仓位：单笔最多只让账户亏损 0.5% 到 1%。",
            "不加仓摊平亏损单；方向错了先退出。",
            "数据前后30分钟波动会放大，新手不做追单。",
            "报告目标是减少乱交易，不能保证每天盈利。",
        ],
    )

    return f"""# 黄金日报：交易决策版

时间：{today}
标的：国际黄金，现货黄金/XAUUSD为主。

## 1. 今日结论，先给答案
{conclusion_table}

一句话执行：{decision.action}
判断信心：{decision.confidence}

## 2. 市场快照
{market_snapshot}

## 3. 今日宏观事件日历
{calendar_text}

## 4. 驱动因素评分
{score_summary}
{context_notes}
{driver_text}

## 5. 技术面，只保留关键价位
{levels_table}

## 6. 今日交易计划，必须分情景
{trade_plan_table}

{scenarios_text}

## 7. 风险控制模块
{risk_control_table}

## 8. 新闻与地缘风险
- 新闻风险归纳：{news_risk_text}
{news_text}

## 9. 哪些情况直接不做
{no_trade_text}

今天最容易亏钱的动作：
{loss_traps_text}

## 10. 复盘模块
| 项目 | 记录 |
| --- | --- |
| 预测方向 | {decision.headline} |
| 交易等级 | {decision.trade_grade} |
| 方向分 | {decision.direction_score} |
| 风险分 | {decision.risk_score} |
| 报告价 | {fmt(gold.value)} |

24小时概率判断：
{probs_text}

24小时后会记录真实金价，并在周六复盘里比较预测和实际变化。

## 判断依据补充
{reasons_text}

## 来源与备注
- 行情源：{gold.source}；{dxy.source}；{tnx.source}{f"；{gld.source}" if gld is not None else ""}
- 新闻源：Google News RSS
- 财经日历源：Yahoo Finance Economic Calendar
- 若行情源限流或不可用，报告会标注“缺失”，并自动降低结论信心。

免责声明：以上为市场信息整理和交易情景推演，不构成个性化投资建议。
""".strip()


def improve_with_openai(raw_report: str) -> str:
    polish_flag = (os.getenv("USE_OPENAI_POLISH") or "").strip().lower()
    if polish_flag in {"false", "0", "no", "off"}:
        return raw_report

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return raw_report

    try:
        payload = {
            "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是谨慎的黄金投资分析助手，服务对象是黄金投资新手。"
                        "只基于用户提供的报告改写，不编造额外行情数据。"
                        "报告必须保持10部分结构：今日结论、市场快照、宏观事件日历、驱动因素评分、技术面关键价位、分情景交易计划、风险控制、新闻风险、哪些情况不做、复盘模块。"
                        "不要新增大段栏目，不要改变栏目顺序。"
                        "能用表格表达的模块必须保留Markdown表格，尤其是结论、市场快照、评分、关键价位、交易计划和复盘。"
                        "必须保留交易等级A/B/C、方向分、风险分、买入观察价、止损价和目标价。"
                        "驱动因素评分部分只展示最关键的5条，其余后台数据只说明已参与评分，不要全部展开。"
                        "避免使用震荡偏多、宽幅震荡、冲高回落、回踩、突破等交易黑话；必须用大白话解释。"
                        "把每个专业词都翻译成普通投资者能理解的话。句子要短，直接告诉用户今天该不该动手、为什么、错了怎么办。"
                        "必须强调风险控制，不能承诺盈利。输出中文 Markdown，但不要使用星号加粗。"
                    ),
                },
                {
                    "role": "user",
                    "content": "请把下面的规则版报告优化成小白也能看懂、能照着执行的黄金日报。必须保留原来的10部分结构和顺序，不使用交易黑话，完整保留交易等级、评分、关键价格、分情景计划、概率、风险控制和免责声明：\n\n"
                    + raw_report,
                },
            ],
            "temperature": 0.2,
        }
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=40,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        return raw_report + f"\n\n备注：AI 综合改写失败，已发送规则版报告。错误：{exc}"


def sanitize_report(report: str) -> str:
    return report.replace("*", "")


def send_serverchan(title: str, desp: str) -> None:
    sendkey = os.getenv("SERVERCHAN_SENDKEY")
    if not sendkey:
        raise RuntimeError("missing SERVERCHAN_SENDKEY")

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    resp = requests.post(url, data={"title": title, "desp": desp}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"ServerChan failed: {data}")


def quote_to_dict(quote: Quote) -> dict[str, object]:
    return {
        "name": quote.name,
        "symbol": quote.symbol,
        "value": quote.value,
        "change_24h": quote.change_24h,
        "high_24h": quote.high_24h,
        "low_24h": quote.low_24h,
        "source": quote.source,
        "updated_at": quote.updated_at.isoformat() if quote.updated_at else None,
        "error": quote.error,
        "status_note": quote_status_note(quote, label=quote.name),
    }


def archive_report(
    report: str,
    raw_report: str,
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    gld: Quote | None,
    news: list[str],
    calendar_events: list[CalendarEvent],
    market_context: MarketContext | None = None,
) -> None:
    generated_at = now_cn()
    date_key = generated_at.strftime("%Y-%m-%d")
    year = generated_at.strftime("%Y")
    report_path = Path("reports") / year / f"{date_key}.md"
    data_path = Path("data") / year / f"{date_key}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)

    decision = judge(gold, dxy, tnx, news, calendar_events, market_context)
    levels = build_levels(gold)
    snapshot = {
        "date": date_key,
        "generated_at": generated_at.isoformat(),
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
        "github_run_number": os.getenv("GITHUB_RUN_NUMBER"),
        "prediction": {
            "headline": decision.headline,
            "action": decision.action,
            "probabilities": decision.probabilities,
            "reasons": decision.reasons,
            "confidence": decision.confidence,
            "trade_grade": decision.trade_grade,
            "trade_grade_text": decision.trade_grade_text,
            "direction_score": decision.direction_score,
            "risk_score": decision.risk_score,
            "driver_scores": [
                {
                    "factor": item.factor,
                    "observation": item.observation,
                    "effect": item.effect,
                    "direction_score": item.direction_score,
                    "risk_score": item.risk_score,
                    "explanation": item.explanation,
                }
                for item in decision.driver_scores
            ],
            "levels": levels,
        },
        "quotes": {
            "gold": quote_to_dict(gold),
            "dxy": quote_to_dict(dxy),
            "us10y": quote_to_dict(tnx),
            "gld": quote_to_dict(gld) if gld is not None else None,
        },
        "market_context": {
            "notes": market_context.notes if market_context else [],
            "prior_evening": market_context.prior_evening if market_context else None,
            "quotes": {
                key: quote_to_dict(value)
                for key, value in (market_context.quotes if market_context else {}).items()
            },
        },
        "news": news,
        "calendar_events": [calendar_to_dict(event) for event in calendar_events],
        "news_risk_flags": news_risk_flags(news),
        "raw_report": raw_report,
        "final_report": report,
    }

    report_path.write_text(report + "\n", encoding="utf-8")
    data_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Archived report to {report_path} and {data_path}.")


def collect_market_inputs() -> tuple[Quote, Quote, Quote, Quote, list[str], list[CalendarEvent], MarketContext]:
    gold = fetch_best_gold_quote()
    dxy = fetch_best_dxy_quote()
    tnx = fetch_best_tnx_quote()
    gld = fetch_best_gld_quote()
    market_context = build_market_context(gold, dxy, tnx, gld)
    news = fetch_news()
    calendar_events = fetch_economic_calendar()
    return gold, dxy, tnx, gld, news, calendar_events, market_context


def build_evening_snapshot(
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    gld: Quote | None,
    news: list[str],
    calendar_events: list[CalendarEvent],
    market_context: MarketContext | None,
) -> dict[str, object]:
    decision = judge(gold, dxy, tnx, news, calendar_events, market_context)
    return {
        "captured_at": now_cn().isoformat(),
        "quotes": {
            "gold": quote_to_dict(gold),
            "dxy": quote_to_dict(dxy),
            "us10y": quote_to_dict(tnx),
            "gld": quote_to_dict(gld) if gld is not None else None,
        },
        "market_context": {
            "notes": market_context.notes if market_context else [],
            "quotes": {
                key: quote_to_dict(value)
                for key, value in (market_context.quotes if market_context else {}).items()
            },
        },
        "news": news,
        "calendar_events": [calendar_to_dict(event) for event in calendar_events],
        "news_risk_flags": news_risk_flags(news),
        "evening_assessment": {
            "headline": decision.headline,
            "trade_grade": decision.trade_grade,
            "direction_score": decision.direction_score,
            "risk_score": decision.risk_score,
            "confidence": decision.confidence,
            "driver_scores": [
                {
                    "factor": item.factor,
                    "observation": item.observation,
                    "effect": item.effect,
                    "direction_score": item.direction_score,
                    "risk_score": item.risk_score,
                    "explanation": item.explanation,
                }
                for item in decision.driver_scores
            ],
        },
    }


def archive_evening_snapshot(
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    gld: Quote | None,
    news: list[str],
    calendar_events: list[CalendarEvent],
    market_context: MarketContext | None,
) -> None:
    captured_at = now_cn()
    date_key = captured_at.strftime("%Y-%m-%d")
    year = captured_at.strftime("%Y")
    data_path = Path("data") / year / f"{date_key}.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    if data_path.exists():
        try:
            snapshot = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            snapshot = {"date": date_key}
    else:
        snapshot = {
            "date": date_key,
            "generated_at": None,
            "note": "仅晚间市场快照；当天早报数据尚未归档。",
        }

    evening = build_evening_snapshot(gold, dxy, tnx, gld, news, calendar_events, market_context)
    morning_gold = ((snapshot.get("quotes") or {}).get("gold") or {}).get("value")
    evening_gold = gold.value
    if isinstance(morning_gold, (int, float)) and isinstance(evening_gold, (int, float)):
        change = float(evening_gold) - float(morning_gold)
        evening["change_from_morning"] = {
            "morning_gold_value": float(morning_gold),
            "evening_gold_value": float(evening_gold),
            "change": change,
            "pct_change": change / float(morning_gold) * 100 if morning_gold else None,
            "note": "晚间金价相对早报报告价的变化。",
        }

    snapshot["evening_snapshot"] = evening
    data_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Archived evening market snapshot to {data_path}.")


def load_daily_snapshots() -> list[tuple[Path, dict[str, object]]]:
    snapshots: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(Path("data").glob("*/*.json")):
        try:
            snapshots.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except Exception as exc:
            print(f"Skip broken snapshot {path}: {exc}")
    return snapshots


def parse_cn_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ)
    except ValueError:
        return None


def update_pending_outcomes(current_gold: Quote | None = None) -> list[Path]:
    updated_paths: list[Path] = []
    fetched_gold = current_gold

    for path, snapshot in load_daily_snapshots():
        if snapshot.get("outcome_24h"):
            continue

        generated_at = parse_cn_datetime(snapshot.get("generated_at"))
        if generated_at is None or now_cn() - generated_at < timedelta(hours=23):
            continue

        gold_snapshot = (snapshot.get("quotes") or {}).get("gold") or {}
        start_value = gold_snapshot.get("value")
        if not isinstance(start_value, (int, float)) or start_value <= 0:
            continue

        if fetched_gold is None:
            fetched_gold = fetch_best_gold_quote()
        if fetched_gold.value is None:
            continue

        change = fetched_gold.value - float(start_value)
        pct_change = change / float(start_value) * 100
        snapshot["outcome_24h"] = {
            "checked_at": now_cn().isoformat(),
            "target_after": (generated_at + timedelta(hours=24)).isoformat(),
            "actual_gold": quote_to_dict(fetched_gold),
            "start_gold_value": float(start_value),
            "actual_gold_value": fetched_gold.value,
            "change": change,
            "pct_change": pct_change,
            "note": "用复盘运行时抓到的最新金价近似记录报告发布后24小时的真实结果。",
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        updated_paths.append(path)

    if updated_paths:
        print("Updated outcomes: " + ", ".join(str(path) for path in updated_paths))
    return updated_paths


def prediction_direction(snapshot: dict[str, object]) -> str:
    prediction = snapshot.get("prediction") or {}
    trade_grade = str(prediction.get("trade_grade") or "")
    if trade_grade == "C":
        return "观望"
    direction_score = prediction.get("direction_score")
    if isinstance(direction_score, (int, float)):
        if direction_score >= 2:
            return "看涨"
        if direction_score <= -2:
            return "看跌"
    headline = str(prediction.get("headline") or "")
    action = str(prediction.get("action") or "")
    text = headline + " " + action
    if "小涨" in text or "上涨" in text or "买入区" in text:
        return "看涨"
    if "小跌" in text or "下跌" in text or "不建议新买" in text or "不利于金价上涨" in text:
        return "看跌"
    return "观望"


def actual_direction(pct_change: float | None) -> str:
    if pct_change is None:
        return "缺失"
    if pct_change >= 0.3:
        return "实际上涨"
    if pct_change <= -0.3:
        return "实际下跌"
    return "实际变化不大"


def accuracy_result(predicted: str, actual: str) -> str:
    if actual == "缺失":
        return "未统计"
    if predicted == "看涨" and actual == "实际上涨":
        return "命中"
    if predicted == "看跌" and actual == "实际下跌":
        return "命中"
    if predicted == "观望" and actual == "实际变化不大":
        return "命中"
    if predicted == "观望":
        return "观望但行情走出方向"
    return "未命中"


def weekly_review_range(today: datetime) -> tuple[datetime.date, datetime.date]:
    end_date = today.date()
    start_date = end_date - timedelta(days=6)
    return start_date, end_date


def build_weekly_review() -> tuple[str, dict[str, object]]:
    today = now_cn()
    start_date, end_date = weekly_review_range(today)
    records = []

    for _, snapshot in load_daily_snapshots():
        date_text = snapshot.get("date")
        if not isinstance(date_text, str):
            continue
        try:
            record_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date <= record_date <= end_date:
            records.append(snapshot)

    rows = []
    counted = 0
    hits = 0
    abs_moves = []
    misses = []

    for snapshot in sorted(records, key=lambda item: str(item.get("date"))):
        date_text = str(snapshot.get("date"))
        predicted = prediction_direction(snapshot)
        prediction = snapshot.get("prediction") or {}
        trade_grade = str(prediction.get("trade_grade") or "旧版")
        direction_score = prediction.get("direction_score")
        risk_score = prediction.get("risk_score")
        gold_snapshot = ((snapshot.get("quotes") or {}).get("gold") or {})
        start_value = gold_snapshot.get("value")
        outcome = snapshot.get("outcome_24h") or {}
        actual_value = outcome.get("actual_gold_value")
        pct_change = outcome.get("pct_change")
        if isinstance(pct_change, (int, float)):
            pct_value = float(pct_change)
        else:
            pct_value = None
        actual = actual_direction(pct_value)
        result = accuracy_result(predicted, actual)
        evening_snapshot = snapshot.get("evening_snapshot") or {}
        evening_change = None
        evening_assessment = {}
        if isinstance(evening_snapshot, dict):
            change_from_morning = evening_snapshot.get("change_from_morning") or {}
            if isinstance(change_from_morning, dict):
                evening_pct = change_from_morning.get("pct_change")
                if isinstance(evening_pct, (int, float)):
                    evening_change = float(evening_pct)
            evening_assessment_raw = evening_snapshot.get("evening_assessment") or {}
            if isinstance(evening_assessment_raw, dict):
                evening_assessment = evening_assessment_raw
        calendar_events = snapshot.get("calendar_events") or []
        high_event_count = sum(
            1
            for event in calendar_events
            if isinstance(event, dict) and event.get("importance") == "高"
        )
        risk_flags = snapshot.get("news_risk_flags") or []
        risk_note = []
        if high_event_count:
            risk_note.append(f"{high_event_count}个高影响财经事件")
        if isinstance(risk_flags, list) and risk_flags:
            risk_note.append("新闻风险：" + "、".join(str(item) for item in risk_flags[:2]))
        if evening_change is not None:
            risk_note.append(f"晚间相对早报 {fmt(evening_change)}%")
        risk_text = "；".join(risk_note) if risk_note else "未记录明显事件风险"

        if result != "未统计":
            counted += 1
            abs_moves.append(abs(pct_value or 0))
            if result == "命中":
                hits += 1
            else:
                misses.append(f"{date_text}：预测{predicted}，{actual}，差距 {fmt(pct_value)}%")

        rows.append(
            {
                "date": date_text,
                "predicted": predicted,
                "trade_grade": trade_grade,
                "direction_score": direction_score,
                "risk_score": risk_score,
                "start_value": start_value,
                "actual_value": actual_value,
                "pct_change": pct_value,
                "evening_pct_change": evening_change,
                "evening_headline": evening_assessment.get("headline"),
                "evening_direction_score": evening_assessment.get("direction_score"),
                "evening_risk_score": evening_assessment.get("risk_score"),
                "actual": actual,
                "result": result,
                "risk_text": risk_text,
            }
        )

    accuracy = hits / counted * 100 if counted else None
    avg_abs_move = sum(abs_moves) / len(abs_moves) if abs_moves else None
    row_text = "\n".join(
        (
            f"- {row['date']}：预测 {row['predicted']}；报告价 {fmt(row['start_value'])}；"
            f"晚间变化 {fmt(row['evening_pct_change'])}%；24小时后 {fmt(row['actual_value'])}；真实变化 {fmt(row['pct_change'])}%；"
            f"结果 {row['result']}；交易等级 {row['trade_grade']}；方向分 {row['direction_score']}；风险分 {row['risk_score']}；当日风险：{row['risk_text']}"
        )
        for row in rows
    )
    if not row_text:
        row_text = "- 本周还没有可复盘的日报数据。"

    if counted == 0:
        summary = "本周可统计的数据还不够，先继续积累。"
    elif accuracy is not None and accuracy >= 70:
        summary = "本周判断整体不错，可以继续沿用当前的保守交易规则。"
    elif accuracy is not None and accuracy >= 50:
        summary = "本周判断有一定参考价值，但还需要降低仓位，尤其要注意没到计划价位不交易。"
    else:
        summary = "本周判断偏差较大，下周要更保守，宁愿少交易，也不要强行找机会。"

    miss_text = "\n".join(f"- {item}" for item in misses[:5]) if misses else "- 暂无明显偏差，或数据不足。"
    generated_at = today.strftime("%Y-%m-%d %H:%M CST")
    review = f"""# 每周黄金报告复盘

时间：{generated_at}
复盘范围：{start_date} 至 {end_date}

## 先看结果
- 本周可统计报告数：{counted}
- 判断命中数：{hits}
- 判断准确率：{fmt(accuracy)}%
- 平均真实波动：{fmt(avg_abs_move)}%
- 一句话结论：{summary}

## 每天对比
{row_text}

## 偏差在哪里
{miss_text}

## 下周怎么改
- 如果准确率低于50%，下周日报默认更保守，少给交易机会。
- 如果连续两天判断失败，第三天只给观察建议，不主动建议开仓。
- 金价真实波动低于0.3%时，按“变化不大”处理，不强行判断涨跌。
- 任何时候都先控制亏损，再考虑盈利。

## 说明
- 这里的准确率只衡量“未来24小时方向判断”是否接近真实走势。
- 真实数据来自每日报告发布约24小时后抓取到的金价。
- 复盘用于改进报告，不代表未来一定准确。
""".strip()

    payload = {
        "generated_at": today.isoformat(),
        "range": {"start": str(start_date), "end": str(end_date)},
        "counted": counted,
        "hits": hits,
        "accuracy": accuracy,
        "average_absolute_move_pct": avg_abs_move,
        "rows": rows,
        "misses": misses,
        "review": review,
    }
    return sanitize_report(review), payload


def archive_weekly_review(review: str, payload: dict[str, object]) -> None:
    generated_at = now_cn()
    date_key = generated_at.strftime("%Y-%m-%d")
    year = generated_at.strftime("%Y")
    md_path = Path("reviews") / year / f"{date_key}.md"
    json_path = Path("reviews") / year / f"{date_key}.json"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(review + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Archived weekly review to {md_path} and {json_path}.")


def fetch_best_quote(symbol: str, name: str) -> Quote:
    quote = fetch_yahoo_chart(symbol, name, range_="1d", interval="5m")
    age = quote_age_minutes(quote)
    if quote.value is not None and (is_market_closed() or (age is not None and age <= 90)):
        return quote
    fallback = fetch_yahoo_chart(symbol, name, range_="5d", interval="1h")
    if fallback.value is not None:
        return fallback
    return quote


def fetch_best_market_quote(
    yahoo_symbol: str,
    name: str,
    *,
    twelvedata_symbols: list[str] | None = None,
) -> Quote:
    candidates: list[Quote] = []
    for td_symbol in twelvedata_symbols or []:
        candidates.append(fetch_twelvedata_chart(td_symbol, name))
    candidates.append(fetch_yahoo_chart(yahoo_symbol, name, range_="1d", interval="5m"))
    candidates.append(fetch_yahoo_chart(yahoo_symbol, name, range_="5d", interval="1h"))
    candidates.append(fetch_yahoo_chart(yahoo_symbol, name, range_="1mo", interval="1d"))
    return first_usable_quote(candidates)


def fetch_best_gold_quote() -> Quote:
    quote = fetch_twelvedata_chart("XAU/USD", "Spot Gold")
    age = quote_age_minutes(quote)
    if quote.value is not None and (is_market_closed() or (age is not None and age <= 90)):
        return quote

    quote = fetch_best_quote("GC=F", "COMEX Gold Futures")
    age = quote_age_minutes(quote)
    if quote.value is not None and (is_market_closed() or (age is not None and age <= 90)):
        return quote

    spot = fetch_best_quote("XAUUSD=X", "Spot Gold")
    if spot.value is not None:
        return spot
    return quote


def fetch_best_dxy_quote() -> Quote:
    return fetch_best_market_quote("DX-Y.NYB", "US Dollar Index", twelvedata_symbols=["DXY", "USDOLLAR"])


def fetch_best_tnx_quote() -> Quote:
    quote = fetch_best_market_quote("^TNX", "US 10Y Treasury Yield", twelvedata_symbols=["US10Y", "TNX"])
    if quote.value is not None:
        return quote
    return fetch_treasury_10y()


def fetch_best_gld_quote() -> Quote:
    return fetch_best_market_quote("GLD", "SPDR Gold ETF", twelvedata_symbols=["GLD"])


def run_daily() -> None:
    update_pending_outcomes()
    gold, dxy, tnx, gld, news, calendar_events, market_context = collect_market_inputs()

    raw = rules_report(gold, dxy, tnx, gld, news, calendar_events, market_context)
    report = sanitize_report(improve_with_openai(raw))
    archive_report(report, raw, gold, dxy, tnx, gld, news, calendar_events, market_context)
    send_serverchan("每日黄金24小时交易判断", report)
    print("Report sent through ServerChan.")


def run_evening() -> None:
    gold, dxy, tnx, gld, news, calendar_events, market_context = collect_market_inputs()
    archive_evening_snapshot(gold, dxy, tnx, gld, news, calendar_events, market_context)
    print("Evening market snapshot archived.")


def run_test_daily() -> None:
    report_at = now_cn()
    updated_at = report_at - timedelta(minutes=8)
    gold = Quote(
        name="Spot Gold",
        symbol="XAU/USD",
        value=3354.20,
        change_24h=-43.80,
        high_24h=3407.10,
        low_24h=3340.60,
        source="历史测试快照：昨天9点附近黄金行情，用于预览报告结构",
        updated_at=updated_at,
    )
    dxy = Quote(
        name="US Dollar Index",
        symbol="DXY",
        value=104.18,
        change_24h=0.32,
        high_24h=104.35,
        low_24h=103.72,
        source="历史测试快照：昨天9点附近美元指数",
        updated_at=updated_at,
    )
    tnx = Quote(
        name="US 10Y Treasury Yield",
        symbol="10Y Treasury",
        value=4.55,
        change_24h=0.08,
        high_24h=4.55,
        low_24h=4.47,
        source="U.S. Treasury daily treasury rates CSV, 2026-06-05",
        updated_at=updated_at,
    )
    gld = Quote(
        name="SPDR Gold ETF",
        symbol="GLD",
        value=308.70,
        change_24h=-3.60,
        high_24h=312.20,
        low_24h=307.90,
        source="历史测试快照：昨天9点附近GLD黄金ETF",
        updated_at=updated_at,
    )
    real_10y = Quote(
        name="10Y Real Yield",
        symbol="DFII10",
        value=2.12,
        change_24h=0.05,
        high_24h=2.12,
        low_24h=2.07,
        source="模拟后台数据：10年期实际利率",
        updated_at=updated_at,
    )
    vix = Quote(
        name="VIX Volatility Index",
        symbol="VIX",
        value=18.60,
        change_24h=2.20,
        high_24h=19.40,
        low_24h=16.50,
        source="模拟后台数据：VIX",
        updated_at=updated_at,
    )
    sp500 = Quote(
        name="S&P 500",
        symbol="SPX",
        value=5825.40,
        change_24h=-62.30,
        high_24h=5890.20,
        low_24h=5810.00,
        source="模拟后台数据：标普500",
        updated_at=updated_at,
    )
    silver = Quote(
        name="Silver Futures",
        symbol="XAG/USD",
        value=31.25,
        change_24h=-0.72,
        high_24h=32.10,
        low_24h=31.05,
        source="模拟后台数据：白银",
        updated_at=updated_at,
    )
    market_context = MarketContext(
        quotes={
            "gold": gold,
            "dxy": dxy,
            "us10y": tnx,
            "gld": gld,
            "real_10y": real_10y,
            "vix": vix,
            "sp500": sp500,
            "silver": silver,
        },
        notes=[
            "后台参考数据共 8 项，可用 8 项。",
            "已纳入实际利率，用来判断黄金中期压力。",
            "已纳入VIX，用来判断市场恐慌程度。",
            "已纳入白银，用来观察贵金属联动。",
        ],
    )
    news = [
        "Gold slides as stronger U.S. jobs data lifts dollar and Treasury yields",
        "Treasury yields rise after labor market data reduces near-term rate-cut hopes",
        "Dollar index strengthens as traders reassess Fed policy path",
        "Gold traders watch U.S. inflation and Fed comments for next direction",
    ]
    calendar_events = [
        CalendarEvent(
            event="U.S. Employment Situation / Nonfarm Payrolls",
            country="US",
            time_text="20:30",
            period="May",
            actual="-",
            expected="-",
            prior="-",
            importance="高",
        )
    ]
    raw = rules_report(gold, dxy, tnx, gld, news, calendar_events, market_context)
    raw = raw.replace("# 黄金日报：交易决策版", "# 黄金日报：交易决策版（历史测试）", 1)
    raw += "\n\n测试说明：这份报告使用昨天9点附近的历史测试快照，只用于查看正式报告长什么样；正式日报会抓取实时数据。"
    report = sanitize_report(improve_with_openai(raw))
    send_serverchan("测试：每日黄金24小时交易判断", report)
    print("Test report sent through ServerChan.")


def run_weekly() -> None:
    update_pending_outcomes()
    review, payload = build_weekly_review()
    archive_weekly_review(review, payload)
    send_serverchan("每周黄金报告复盘", review)
    print("Weekly review sent through ServerChan.")


def main() -> None:
    mode = (os.getenv("REPORT_MODE") or "daily").strip().lower()
    if mode == "weekly":
        run_weekly()
    elif mode == "evening":
        run_evening()
    elif mode in {"test_daily", "test"}:
        run_test_daily()
    else:
        run_daily()


if __name__ == "__main__":
    main()
