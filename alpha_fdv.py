"""查询 Binance Alpha 中 FDV 低于指定美元金额的在交易交易对。"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://www.binance.com"
TOKEN_PATH = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
EXCHANGE_PATH = "/bapi/defi/v1/public/alpha-trade/get-exchange-info"
FIELDS = [
    "交易对", "API交易对", "名称", "FDV_USD", "价格_USD", "流通市值_USD",
    "链", "合约地址", "查询时间_UTC",
]


def positive_decimal(value):
    """无效、非有限及非正数返回 None，不将缺失数据当作零。"""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def parse_threshold(value):
    number = positive_decimal(value)
    if number is None:
        raise argparse.ArgumentTypeError("必须是大于零的有限数字，例如 1000000")
    return number


def fetch_data(path, timeout=20, attempts=3):
    """读取公开接口；对连接错误、限流和服务端错误进行有限重试。"""
    request = Request(BASE_URL + path, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
    })
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if (exc.code == 429 or exc.code >= 500) and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{path}: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{path}: 网络请求失败，请检查网络或代理 ({exc})") from exc
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(f"{path}: 返回的内容不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{path}: 响应结构异常")
        if payload.get("code") != "000000" or payload.get("success") is False:
            raise RuntimeError(
                f"{path}: API 错误 {payload.get('code')}: {payload.get('message')}"
            )
        return payload.get("data")


def select_pairs(tokens, exchange, threshold, quote=None):
    if not isinstance(tokens, list) or not isinstance(exchange, dict):
        raise RuntimeError("接口 data 格式异常，无法筛选")
    symbols = exchange.get("symbols")
    if not isinstance(symbols, list):
        raise RuntimeError("交易信息缺少 symbols 列表")
    if any(not isinstance(item, dict) for item in tokens + symbols):
        raise RuntimeError("接口列表包含异常记录")

    eligible = {}
    invalid_fdv = 0
    for token in tokens:
        fdv = positive_decimal(token.get("fdv"))
        if fdv is None:
            invalid_fdv += 1
            continue
        if fdv < threshold and token.get("alphaId"):
            # 以 Alpha ID 关联，避免同名代币错配；不猜测交易对名称。
            eligible[token["alphaId"]] = (token, fdv)

    rows = []
    seen = set()
    queried_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for pair in symbols:
        if pair.get("status") != "TRADING":
            continue
        if quote and pair.get("quoteAsset") != quote:
            continue
        match = eligible.get(pair.get("baseAsset"))
        api_symbol = pair.get("symbol")
        if not match or not api_symbol or api_symbol in seen:
            continue
        seen.add(api_symbol)
        token, fdv = match
        rows.append({
            "交易对": f"{token.get('symbol', pair['baseAsset'])}/{pair.get('quoteAsset', '')}",
            "API交易对": api_symbol,
            "名称": token.get("name", ""),
            "FDV_USD": fdv,
            "价格_USD": token.get("price", ""),
            "流通市值_USD": token.get("marketCap", ""),
            "链": token.get("chainName", token.get("chainId", "")),
            "合约地址": token.get("contractAddress", ""),
            "查询时间_UTC": queried_at,
        })
    rows.sort(key=lambda row: (row["FDV_USD"], row["API交易对"]))
    return rows, invalid_fdv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-fdv", type=parse_threshold, default=Decimal("1000000"),
                        help="FDV 严格上限，单位美元，默认 1000000")
    parser.add_argument("--quote", type=str.upper, help="只查指定计价币种，例如 USDT")
    parser.add_argument("--output", type=Path, default=Path("alpha_low_fdv.csv"),
                        help="CSV 路径，默认 alpha_low_fdv.csv（覆盖同名文件）")
    args = parser.parse_args(argv)
    try:
        print("正在查询 Binance Alpha 代币估值与交易对...")
        tokens = fetch_data(TOKEN_PATH)
        exchange = fetch_data(EXCHANGE_PATH)
        rows, invalid = select_pairs(tokens, exchange, args.max_fdv, args.quote)
        print(f"FDV < ${args.max_fdv:,.2f}：共 {len(rows)} 个交易对；"
              f"跳过 {invalid} 条无效或非正 FDV 的代币记录。")
        for rank, row in enumerate(rows, 1):
            print(f"{rank:>3}. {row['交易对']:<22} {row['API交易对']:<22} "
                  f"FDV=${row['FDV_USD']:>14,.2f}  链={row['链']}")
        with args.output.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"结果已保存：{args.output.resolve()}")
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
