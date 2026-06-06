from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
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


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


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
) -> list[DriverScore]:
    news = news or []
    calendar_events = calendar_events or []
    rows: list[DriverScore] = []

    gold_age = quote_age_minutes(gold)
    gold_pct = pct(gold.change_24h, gold.value)
    if gold.value is None:
        rows.append(DriverScore("金价", "缺失", "不能判断", 0, 3, "没有最新金价时，不给交易建议。"))
    elif gold_age is None or gold_age > 90:
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
        rows.append(DriverScore("美元", "缺失", "不能判断", 0, 1, "美元是黄金的重要对手盘，缺失时信心下降。"))
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
        rows.append(DriverScore("美债收益率", "缺失", "不能判断", 0, 1, "利率数据缺失时，不能完整判断黄金压力。"))
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
) -> TradeDecision:
    gold_age = quote_age_minutes(gold)
    news = news or []
    calendar_events = calendar_events or []
    driver_scores = build_driver_scores(gold, dxy, tnx, news, calendar_events)
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
    rows = []
    for event in events[:8]:
        if event.event == "财经日历抓取失败":
            rows.append("- 财经日历抓取失败：今天的事件风险需要手动留意。")
            continue
        impact = "新手先观望" if event.importance == "高" else "留意即可"
        rows.append(
            f"- {event.time_text}｜{event.country}｜{event.event}｜重要性：{event.importance}｜处理：{impact}"
        )
    return "\n".join(rows)


def build_driver_scores_text(decision: TradeDecision) -> str:
    rows = []
    for item in decision.driver_scores:
        sign = "+" if item.direction_score > 0 else ""
        rows.append(
            f"- {item.factor}：{item.observation}｜结论：{item.effect}｜方向分 {sign}{item.direction_score}｜风险分 {item.risk_score}｜说明：{item.explanation}"
        )
    return "\n".join(rows)


def build_core_logic(decision: TradeDecision) -> str:
    useful = [item for item in decision.driver_scores if item.direction_score or item.risk_score]
    if not useful:
        return "今天没有看到足够强的上涨或下跌理由，所以先按观察处理。"
    parts = []
    for item in useful[:4]:
        parts.append(f"{item.factor}：{item.effect}")
    return "；".join(parts)


def build_market_snapshot(gold: Quote, dxy: Quote, tnx: Quote, gld: Quote | None = None) -> str:
    gold_pct = pct(gold.change_24h, gold.value)
    dxy_pct = pct(dxy.change_24h, dxy.value)
    gld_line = ""
    if gld is not None:
        gld_pct = pct(gld.change_24h, gld.value)
        gld_line = f"\n- GLD黄金ETF：{fmt(gld.value)}，近24小时 {fmt(gld_pct)}%，用于粗看海外黄金资金情绪"
    lines = f"""- 国际黄金：{fmt(gold.value)} 美元/盎司，近24小时 {fmt(gold.change_24h)} 美元，约 {fmt(gold_pct)}%
- 金价更新时间：{fmt_time(gold.updated_at)}
- 美元指数：{fmt(dxy.value)}，近24小时 {fmt(dxy_pct)}%
- 美国10年期收益率：{fmt(tnx.value)}，近24小时 {fmt(tnx.change_24h)}
{gld_line}
- 数据完整性：{"完整" if not any(q.value is None for q in [gold, dxy, tnx]) else "部分缺失，今天只适合降低仓位或观察"}""".strip()
    return re.sub(r"\n{2,}", "\n", lines)


def build_trade_rules(decision: TradeDecision, levels: dict[str, str], gold: Quote) -> tuple[str, str, str]:
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


def build_trade_scenarios(decision: TradeDecision, levels: dict[str, str], gold: Quote) -> str:
    if gold.value is None or decision.trade_grade == "C":
        return "\n".join(
            [
                "- 情景一：价格下跌到买入观察区。处理：先看，不急着买，等下一份报告或手动确认。",
                "- 情景二：价格突然上涨。处理：不追高，因为没有计划内买点。",
                "- 情景三：价格快速下跌。处理：不抄底，先保护本金。",
            ]
        )

    if "上涨机会" in decision.headline:
        return "\n".join(
            [
                f"- 情景一：价格回到 {levels['support_near']}，并且不再继续跌。处理：只用小仓位买入。",
                f"- 情景二：价格直接涨到 {levels['resistance_near']} 附近。处理：没有持仓就不追；已有持仓先减一部分。",
                f"- 情景三：价格跌破 {levels['support_key']}。处理：取消今天买入想法，已经买了就先离场。",
            ]
        )

    if "下跌风险" in decision.headline:
        return "\n".join(
            [
                f"- 情景一：价格跌破 {levels['support_near']}。处理：不买，已有多单先减仓或离场。",
                f"- 情景二：价格重新涨回 {levels['resistance_near']} 上方，并且没有马上跌回。处理：说明下跌风险缓和，但仍先观察。",
                f"- 情景三：价格在中间位置来回动。处理：不交易，因为没有便宜价，也没有强势信号。",
            ]
        )

    return "\n".join(
        [
            f"- 情景一：价格跌到 {levels['support_near']} 并稳住。处理：最多小仓位试一次。",
            f"- 情景二：价格涨到 {levels['resistance_near']}。处理：没有提前买就不追。",
            f"- 情景三：价格跌破 {levels['support_key']}。处理：今天不再找买点。",
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
    if any(event.importance == "高" for event in events):
        rows.insert(0, "重要数据公布前后30分钟追涨追跌。")
    gold_age = quote_age_minutes(gold)
    if gold_age is None or gold_age > 90:
        rows.insert(0, "金价数据不新，还照着旧点位交易。")
    return rows


def build_no_trade_conditions(gold: Quote, events: list[CalendarEvent]) -> list[str]:
    rows = []
    gold_age = quote_age_minutes(gold)
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


def rules_report(
    gold: Quote,
    dxy: Quote,
    tnx: Quote,
    gld: Quote | None,
    news: list[str],
    calendar_events: list[CalendarEvent],
) -> str:
    levels = build_levels(gold)
    decision = judge(gold, dxy, tnx, news, calendar_events)
    today = now_cn().strftime("%Y-%m-%d %H:%M CST")

    news_text = "\n".join(f"- {item}" for item in news[:5]) if news else "- 暂未抓到可靠的近24小时新闻标题，需降低新闻面判断权重。"
    probs_text = "\n".join(f"- {k}：{v}%" for k, v in decision.probabilities.items())
    reasons_text = "\n".join(f"- {reason}" for reason in decision.reasons) if decision.reasons else "- 当前没有单一变量给出强信号，按方向不清楚处理。"
    calendar_text = build_calendar_text(calendar_events)
    entry_rule, target_rule, stop_rule = build_trade_rules(decision, levels, gold)
    scenarios_text = build_trade_scenarios(decision, levels, gold)
    driver_text = build_driver_scores_text(decision)
    market_snapshot = build_market_snapshot(gold, dxy, tnx, gld)
    no_trade_text = "\n".join(f"- {item}" for item in build_no_trade_conditions(gold, calendar_events))
    loss_traps_text = "\n".join(f"- {item}" for item in build_loss_traps(decision, gold, calendar_events))
    risk_flags = news_risk_flags(news)
    news_risk_text = "、".join(risk_flags) if risk_flags else "暂未发现特别强的新闻风险词"
    core_logic = build_core_logic(decision)

    return f"""# 黄金日报：24小时交易决策仪表盘

时间：{today}
标的：国际黄金，现货黄金/XAUUSD为主。

## 1. 今日结论，先给答案
- 今日黄金观点：{decision.headline}
- 交易等级：{decision.trade_grade}，{decision.trade_grade_text}
- 判断信心：{decision.confidence}
- 核心逻辑：{core_logic}
- 最大风险：{"今天有重要数据或新闻风险，容易突然大幅波动。" if decision.risk_score >= 2 else "暂未发现特别强的事件风险，但仍不能满仓。"}
- 操作原则：{decision.action}

## 2. 市场快照
{market_snapshot}

## 3. 今日宏观事件日历
{calendar_text}

## 4. 驱动因素评分
- 方向分：{decision.direction_score}。正数越高，越支持上涨；负数越低，越支持下跌。
- 风险分：{decision.risk_score}。分数越高，越不适合新手交易。
{driver_text}

## 5. 技术面，只保留关键价位
- 买入观察价：{levels["support_near"]}
- 跌破就认错的位置：{levels["support_key"]}
- 第一目标/减仓价：{levels["resistance_near"]}
- 第二目标/减仓价：{levels["resistance_key"]}

## 6. 今日交易计划，分情景执行
- 入场规则：{entry_rule}
- 目标/减仓：{target_rule}
- 止损/认错：{stop_rule}
{scenarios_text}

## 7. 哪些情况直接不做
{no_trade_text}

## 8. 今天最容易亏钱的情况
{loss_traps_text}

## 9. 近24小时新闻线索
- 新闻风险归纳：{news_risk_text}
{news_text}

## 10. 为什么这么判断
{reasons_text}

## 11. 24小时概率判断
{probs_text}

## 12. 风险控制模块
- 新手仓位：单笔最多只让账户亏损 0.5% 到 1%。
- 不加仓摊平亏损单；方向错了先退出。
- 数据前后30分钟波动会放大，除非经验足够，否则不做追单。
- 这份报告追求的是提高胜率和减少乱交易，不能保证每天盈利。

## 13. 今日复盘记录
- 今天预测方向：{decision.headline}
- 交易等级：{decision.trade_grade}
- 报告价：{fmt(gold.value)}
- 24小时后会记录真实金价，并在周六复盘里比较预测和实际变化。

## 14. 来源与备注
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
                        "报告要保持交易决策仪表盘结构：今日结论、市场快照、宏观日历、驱动因素评分、关键价位、分情景交易计划、风险控制、复盘记录。"
                        "必须保留交易等级A/B/C、方向分、风险分、买入观察价、止损价和目标价。"
                        "避免使用震荡偏多、宽幅震荡、冲高回落、回踩、突破等交易黑话；必须用大白话解释。"
                        "把每个专业词都翻译成普通投资者能理解的话。句子要短，直接告诉用户今天该不该动手、为什么、错了怎么办。"
                        "必须强调风险控制，不能承诺盈利。输出中文 Markdown，但不要使用星号加粗。"
                    ),
                },
                {
                    "role": "user",
                    "content": "请把下面的规则版报告优化成小白也能看懂、能照着执行的黄金日报，不使用交易黑话，完整保留交易等级、评分、关键价格、分情景计划、概率、风险控制和免责声明：\n\n"
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
) -> None:
    generated_at = now_cn()
    date_key = generated_at.strftime("%Y-%m-%d")
    year = generated_at.strftime("%Y")
    report_path = Path("reports") / year / f"{date_key}.md"
    data_path = Path("data") / year / f"{date_key}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)

    decision = judge(gold, dxy, tnx, news, calendar_events)
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
        "news": news,
        "calendar_events": [calendar_to_dict(event) for event in calendar_events],
        "news_risk_flags": news_risk_flags(news),
        "raw_report": raw_report,
        "final_report": report,
    }

    report_path.write_text(report + "\n", encoding="utf-8")
    data_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Archived report to {report_path} and {data_path}.")


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
            f"24小时后 {fmt(row['actual_value'])}；真实变化 {fmt(row['pct_change'])}%；"
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
    if quote.value is not None and age is not None and age <= 90:
        return quote
    fallback = fetch_yahoo_chart(symbol, name, range_="5d", interval="1h")
    if fallback.value is not None:
        return fallback
    return quote


def fetch_best_gold_quote() -> Quote:
    quote = fetch_twelvedata_chart("XAU/USD", "Spot Gold")
    age = quote_age_minutes(quote)
    if quote.value is not None and age is not None and age <= 90:
        return quote

    quote = fetch_best_quote("GC=F", "COMEX Gold Futures")
    age = quote_age_minutes(quote)
    if quote.value is not None and age is not None and age <= 90:
        return quote

    spot = fetch_best_quote("XAUUSD=X", "Spot Gold")
    if spot.value is not None:
        return spot
    return quote


def run_daily() -> None:
    update_pending_outcomes()
    gold = fetch_best_gold_quote()

    dxy = fetch_best_quote("DX-Y.NYB", "US Dollar Index")
    tnx = fetch_best_quote("^TNX", "US 10Y Treasury Yield")
    gld = fetch_best_quote("GLD", "SPDR Gold ETF")
    news = fetch_news()
    calendar_events = fetch_economic_calendar()

    raw = rules_report(gold, dxy, tnx, gld, news, calendar_events)
    report = sanitize_report(improve_with_openai(raw))
    archive_report(report, raw, gold, dxy, tnx, gld, news, calendar_events)
    send_serverchan("每日黄金24小时交易判断", report)
    print("Report sent through ServerChan.")


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
    else:
        run_daily()


if __name__ == "__main__":
    main()
