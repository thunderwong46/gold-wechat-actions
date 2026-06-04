from __future__ import annotations

import json
import math
import os
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def judge(gold: Quote, dxy: Quote, tnx: Quote) -> tuple[str, str, dict[str, int], list[str], str]:
    score = 0
    reasons = []
    gold_age = quote_age_minutes(gold)

    gold_pct = pct(gold.change_24h, gold.value)
    dxy_pct = pct(dxy.change_24h, dxy.value)
    tnx_change = tnx.change_24h

    if gold_pct is not None:
        if gold_pct > 0.4:
            score += 1
            reasons.append("黄金近24小时在涨，说明短时间买的人更多")
        elif gold_pct < -0.4:
            score -= 1
            reasons.append("黄金近24小时在跌，说明短时间卖的人更多")

    if dxy_pct is not None:
        if dxy_pct < -0.15:
            score += 1
            reasons.append("美元指数回落，通常会帮助金价上涨")
        elif dxy_pct > 0.15:
            score -= 1
            reasons.append("美元指数走强，通常不利于金价上涨")

    if tnx_change is not None:
        if tnx_change < -0.03:
            score += 1
            reasons.append("美国10年期收益率回落，买黄金的压力会小一些")
        elif tnx_change > 0.03:
            score -= 1
            reasons.append("美国10年期收益率上行，会让黄金承受压力")

    missing = sum(1 for q in [gold, dxy, tnx] if q.value is None)
    if missing:
        reasons.append(f"有 {missing} 项关键行情数据缺失，判断信心需要下调")
    if gold_age is None or gold_age > 90:
        reasons.append("金价不是90分钟内更新的数据，这种情况下不要按点位交易，只观察")

    if gold_age is None or gold_age > 90:
        return (
            "行情不够新，先不要交易",
            "先观望；等拿到最新金价后再判断",
            {"先观望": 70, "拿到最新行情后再判断": 30},
            reasons,
            "低",
        )

    if score >= 2:
        headline = "更可能小涨，但中间会来回晃"
        action = "只在跌到买入区并止住时小仓位买；不要追高"
        probs = {"更可能小涨": 50, "方向不清楚，来回晃": 30, "可能先涨后跌": 20}
    elif score <= -2:
        headline = "更可能小跌，先别急着买"
        action = "观望为主；除非价格重新站稳关键位置，否则不追多"
        probs = {"更可能小跌": 50, "方向不清楚，来回晃": 30, "跌多了再反弹": 20}
    else:
        headline = "方向不清楚，大概率来回晃"
        action = "先观望；只在关键买入区或卖出区出现明确信号时再动手"
        probs = {"方向不清楚，来回晃": 45, "更可能小涨": 30, "更可能小跌": 25}

    if missing >= 2:
        confidence = "低"
    elif abs(score) >= 2 and missing == 0:
        confidence = "中高"
    else:
        confidence = "中"

    return headline, action, probs, reasons, confidence


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


def rules_report(gold: Quote, dxy: Quote, tnx: Quote, news: list[str]) -> str:
    levels = build_levels(gold)
    headline, action, probs, reasons, confidence = judge(gold, dxy, tnx)
    today = now_cn().strftime("%Y-%m-%d %H:%M CST")

    gold_pct = pct(gold.change_24h, gold.value)
    dxy_pct = pct(dxy.change_24h, dxy.value)

    news_text = "\n".join(f"- {item}" for item in news[:5]) if news else "- 暂未抓到可靠的近24小时新闻标题，需降低新闻面判断权重。"
    probs_text = "\n".join(f"- {k}：{v}%" for k, v in probs.items())
    checklist_text = "\n".join(build_market_checklist(gold, dxy, tnx))
    reasons_text = "\n".join(f"- {reason}" for reason in reasons) if reasons else "- 当前没有单一变量给出强信号，按震荡处理。"
    data_quality = "完整" if not any(q.value is None for q in [gold, dxy, tnx]) else "部分缺失，需降低仓位和信心"

    return f"""# 每日黄金24小时交易判断

时间：{today}
标的：国际黄金，现货黄金/XAUUSD为主。

## 先看结论
- 未来24小时方向：{headline}
- 今天操作建议：{action}
- 判断信心：{confidence}
- 新手原则：价格没到计划位置，不交易；到了位置但还没止住，也不交易。

## 小白操作卡
1. 只在两个位置考虑动手：跌到 {levels["support_near"]} 附近并止住，或涨过 {levels["resistance_near"]} 后站稳。
2. 如果价格在中间晃，既不到买入区也不到卖出区，默认观望。
3. 如果进场后跌破 {levels["support_key"]}，说明判断可能错了，先止损或离场，不硬扛。

## 现在市场在说什么
- 黄金：{fmt(gold.value)} 美元/盎司附近，近24小时变化 {fmt(gold.change_24h)}，约 {fmt(gold_pct)}%。
- 金价更新时间：{fmt_time(gold.updated_at)}
- 数据完整性：{data_quality}。
{checklist_text}

## 为什么这么判断
{reasons_text}

## 24小时概率判断
{probs_text}

## 关键价位怎么用
- 买入观察区：{levels["support_near"]}
- 跌破就认错的位置：{levels["support_key"]}
- 第一卖出/减仓区：{levels["resistance_near"]}
- 第二卖出/减仓区：{levels["resistance_key"]}

## 三种执行情景
- 跌下来再买：价格回到 {levels["support_near"]} 后不再继续跌，再考虑小仓位买；先看 {levels["resistance_near"]} 能不能到。
- 涨上去再跟：价格站上 {levels["resistance_near"]}，同时美元和美债没有明显反弹，再小仓位跟；目标看 {levels["resistance_key"]}。
- 直接观望：价格在买入区和卖出区中间、重大数据公布前、或报告数据缺失时，不开新仓。

## 风险控制
- 新手仓位：单笔最多只让账户亏损 0.5%-1%。
- 不加仓摊平亏损单；方向错了先退出。
- 数据前后30分钟波动会放大，除非经验足够，否则不做追单。
- 这份报告追求的是提高胜率和减少乱交易，不能保证每天盈利。

## 近24小时新闻线索
{news_text}

## 小白词典
- 买入观察区：价格跌到这里，可能有人愿意买，但要看到“不再继续跌”才考虑。
- 卖出/减仓区：价格涨到这里，可能有人开始卖，先把利润保护住。
- 站稳：价格涨过某个位置后，没有马上跌回来。
- 止损：承认这次判断错了，先保住本金。

## 来源与备注
- 行情源：{gold.source}；{dxy.source}；{tnx.source}
- 新闻源：Google News RSS
- 若行情源限流或不可用，报告会标注“缺失”，并自动降低结论信心。

免责声明：以上为市场信息整理和交易情景推演，不构成个性化投资建议。
""".strip()


def improve_with_openai(raw_report: str) -> str:
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
                        "报告要先给能不能交易，再解释原因，最后给清晰的买入、卖出、观望规则。"
                        "避免使用震荡偏多、宽幅震荡、冲高回落、回踩、突破等交易黑话；必须用大白话解释。"
                        "必须强调风险控制，不能承诺盈利。输出中文 Markdown，但不要使用星号加粗。"
                    ),
                },
                {
                    "role": "user",
                    "content": "请把下面的规则版报告优化成小白也能看懂、能照着执行的每日黄金交易判断报告，不使用交易黑话，保留关键价格、概率、风险、小白词典和免责声明：\n\n"
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


def archive_report(report: str, raw_report: str, gold: Quote, dxy: Quote, tnx: Quote, news: list[str]) -> None:
    generated_at = now_cn()
    date_key = generated_at.strftime("%Y-%m-%d")
    year = generated_at.strftime("%Y")
    report_path = Path("reports") / year / f"{date_key}.md"
    data_path = Path("data") / year / f"{date_key}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)

    headline, action, probs, reasons, confidence = judge(gold, dxy, tnx)
    levels = build_levels(gold)
    snapshot = {
        "date": date_key,
        "generated_at": generated_at.isoformat(),
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
        "github_run_number": os.getenv("GITHUB_RUN_NUMBER"),
        "prediction": {
            "headline": headline,
            "action": action,
            "probabilities": probs,
            "reasons": reasons,
            "confidence": confidence,
            "levels": levels,
        },
        "quotes": {
            "gold": quote_to_dict(gold),
            "dxy": quote_to_dict(dxy),
            "us10y": quote_to_dict(tnx),
        },
        "news": news,
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
    headline = str(prediction.get("headline") or "")
    action = str(prediction.get("action") or "")
    text = headline + " " + action
    if "小涨" in text or "上涨" in text:
        return "看涨"
    if "小跌" in text or "不利于金价上涨" in text:
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
                "start_value": start_value,
                "actual_value": actual_value,
                "pct_change": pct_value,
                "actual": actual,
                "result": result,
            }
        )

    accuracy = hits / counted * 100 if counted else None
    avg_abs_move = sum(abs_moves) / len(abs_moves) if abs_moves else None
    row_text = "\n".join(
        (
            f"- {row['date']}：预测 {row['predicted']}；报告价 {fmt(row['start_value'])}；"
            f"24小时后 {fmt(row['actual_value'])}；真实变化 {fmt(row['pct_change'])}%；结果 {row['result']}"
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
    news = fetch_news()

    raw = rules_report(gold, dxy, tnx, news)
    report = sanitize_report(improve_with_openai(raw))
    archive_report(report, raw, gold, dxy, tnx, news)
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
