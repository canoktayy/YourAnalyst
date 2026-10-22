#!/usr/bin/env python3
"""Unit tests for the AI guard code (needs node): the wording check for stock indicators
(workflows/src/lib.py) and the currency-aware number check of "YA – Answer question".
Cases are real wrong answers the 7B model gave during testing.
    python3 tests/test_checks.py
"""
import json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workflows" / "src"))
from lib import WORDING_CHECK_JS  # noqa: E402

NVDA = {"symbol": "NVDA", "contributions": {"rsi": 0.5, "macd": -1, "volume": 0, "ma_trend": 0.5}}
WORDING = [
    ("accurate single-stock text", [NVDA], True, [],
     "RSI-14 is at 37.53, below neutral but not oversold. The MACD histogram is negative and decreasing. "
     "It closed above the 200-day moving average, but not in a clear uptrend. Volume isn’t unusually high."),
    ("'RSI overbought' for RSI 37.5", [NVDA], True, ["NVDA"],
     "NVDA shows RSI overbought, a negative and falling MACD histogram, and closing above SMA200."),
    ("negative MACD left out", [NVDA], True, ["NVDA"], "NVDA has RSI below 50 and sits above SMA200 only."),
    ("group described as one", [
        {"symbol": "AMZN", "contributions": {"rsi": 0.5, "macd": 0.5, "volume": 0, "ma_trend": 0.5}},
        {"symbol": "JPM", "contributions": {"rsi": 0, "macd": 1, "volume": 0, "ma_trend": 0.5}},
        {"symbol": "MSFT", "contributions": {"rsi": 0, "macd": 0.5, "volume": 0, "ma_trend": 1}}], False, ["AMZN", "JPM"],
     "AMZN, JPM, and MSFT each score 68.8, indicating neutral to strong RSI, positive MACD, and an uptrend."),
    ("accurate ranked list", [
        {"symbol": "KO", "contributions": {"rsi": -1, "macd": 1, "volume": 0, "ma_trend": 1}},
        {"symbol": "JNJ", "contributions": {"rsi": 0.5, "macd": 0, "volume": 0, "ma_trend": 0.5}}], False, [],
     "1. KO: RSI overbought (above 70), MACD positive and rising, uptrend.\n"
     "2. JNJ: RSI below 50, MACD negative but improving, above SMA200 only."),
]

answer_wf = json.loads((ROOT / "workflows" / "answer_question.json").read_text())
CHECK_NUMBERS = next(n for n in answer_wf["nodes"] if n["name"] == "Check numbers")["parameters"]["jsCode"]
CTX = {"amounts": [[107065, "USD"], [95143.31, "EUR"], [29455, "USD"], [26175.19, "EUR"]],
       "llm_facts": {"quantity": 500}, "question": "How much is my NVDA position worth?",
       "mode": "question", "facts": {}, "block": "B"}
CURRENCY = [
    ("USD amount called EUR", "Your NVDA position is worth 107,065 EUR and the profit is 29,455 EUR.", False),
    ("amounts with their own currency", "Your NVDA position is worth 107,065.00 USD (95,143.31 EUR), profit $29,455.", True),
]

js = [WORDING_CHECK_JS, "let fails = 0; const t = (ok, name, got) => { console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : ' → ' + JSON.stringify(got))); if (!ok) fails++; };"]
for name, ranked, single, expected, text in WORDING:
    js.append(f"{{ const p = wordingProblems({json.dumps(text)}, {json.dumps(ranked)}, {str(single).lower()}).map(x => x.split(':')[0]);"
              f" t(JSON.stringify(p) === {json.dumps(json.dumps(expected, separators=(',', ':')))}, 'wording: ' + {json.dumps(name)}, p); }}")
for name, text, expected in CURRENCY:
    js.append(f"{{ const out = (function () {{ const $ = () => ({{ first: () => ({{ json: {json.dumps(CTX)} }}) }}); const $json = {{ text: {json.dumps(text)} }};\n"
              f"{CHECK_NUMBERS}\n}})(); const d = out[0].json.data; t(d.explanation_checked === {str(expected).lower()}, 'currency: ' + {json.dumps(name)}, d.rejected_numbers); }}")
js.append("process.exit(fails);")

sys.exit(subprocess.run(["node", "-e", "\n".join(js)]).returncode)
