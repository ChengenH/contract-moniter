import asyncio
import aiohttp
import time
import csv
import math
import re
from urllib.parse import urlencode
from typing import List, Dict, Any, Optional, Tuple, Set

# 并发信号量（限制同时处理的币种并发请求数）
CONCURRENCY_LIMIT = 15

# Top10 是链上持币地址集中度，不是交易所合约账户持仓排名。
TOP10_CONCURRENCY = 3
TOP10_PRICE_TOLERANCE = 0.10
TOP10_SEARCH_URL = (
    'https://web3.binance.com/bapi/defi/v5/public/wallet-direct/'
    'buw/wallet/market/token/search/ai'
)
# 同名或多链代币可按合约交易对指定数据来源：(chainId, contractAddress)。
# 例如自行核实后填写：'XXXUSDT': ('1', '0x...')
TOP10_TOKEN_OVERRIDES: Dict[str, Tuple[str, str]] = {}


async def fetch_json(
        session: aiohttp.ClientSession,
        url: str,
        timeout: int = 8,
        method: str = 'GET',
        json_data: dict = None
) -> Optional[Any]:
    """通用异步请求工具函数"""
    try:
        if method == 'GET':
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json()

        elif method == 'POST':
            async with session.post(
                    url,
                    json=json_data,
                    timeout=timeout
            ) as response:
                if response.status == 200:
                    return await response.json()

    except Exception:
        pass

    return None


# ============================================================
# 币安基础数据
# ============================================================

def finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def top10_lookup_symbol(contract: str) -> Tuple[str, int]:
    """拆分常见倍数合约，仅用于链上数据匹配，不改变原合约持仓计算。"""
    base = contract[:-4] if contract.endswith('USDT') else contract
    match = re.fullmatch(r'(1000000|100000|10000|1000)([A-Z][A-Z0-9]*)', base)
    return (match[2], int(match[1])) if match else (base, 1)


def top10_result(status: str, token: Optional[dict] = None) -> Dict[str, Any]:
    token = token or {}
    percent = finite_number(token.get('holdersTop10Percent'))
    valid = percent is not None and 0 <= percent <= 100
    return {
        'top10_holders_ratio': percent / 100 if valid else None,
        'top10_chain': str(token.get('chainId', '')),
        'top10_address': token.get('contractAddress', ''),
        'top10_status': status if valid or not token else '接口未提供有效Top10占比',
    }


def select_top10_token(
        data: Any, contract: str, price: float,
        override: Optional[Tuple[str, str]] = None
) -> Dict[str, Any]:
    """不按最大市值或第一条搜索结果猜测同名代币。"""
    if not isinstance(data, dict) or data.get('code') != '000000' or data.get('success') is False:
        return top10_result('接口请求失败')
    tokens = data.get('data')
    if not isinstance(tokens, list):
        return top10_result('接口数据格式异常')
    symbol, multiplier = top10_lookup_symbol(contract)
    expected_price = finite_number(price)
    if expected_price is not None:
        expected_price /= multiplier
    candidates = {}
    for token in tokens:
        if not isinstance(token, dict):
            continue
        chain = str(token.get('chainId', ''))
        address = token.get('contractAddress')
        if not chain or not isinstance(address, str) or not address:
            continue
        # EVM 十六进制地址不区分大小写；Solana 等地址保留大小写。
        normalized_address = address.lower() if address.startswith('0x') else address
        if override:
            wanted = override[1].lower() if override[1].startswith('0x') else override[1]
            if chain != str(override[0]) or normalized_address != wanted:
                continue
        else:
            if str(token.get('symbol', '')).upper() != symbol.upper():
                continue
            token_price = finite_number(token.get('price'))
            if expected_price is None or expected_price <= 0 or token_price is None or token_price <= 0:
                continue
            if abs(token_price / expected_price - 1) > TOP10_PRICE_TOLERANCE:
                continue
        candidates[(chain, normalized_address)] = token
    if not candidates:
        return top10_result('未找到指定链及合约' if override else '无同名且价格匹配的代币')
    status = '指定链及合约' if override else '名称及价格匹配（待核对）'
    if not override:
        bsc_candidates = {key: value for key, value in candidates.items() if key[0] == '56'}
        if bsc_candidates:
            candidates = bsc_candidates
            status = 'BSC优先，名称及价格匹配（待核对）'
    if len(candidates) > 1:
        return top10_result('同名多链/多合约，需指定地址')
    token = next(iter(candidates.values()))
    return top10_result(status, token)


async def get_top10_holders(
        session: aiohttp.ClientSession, contract: str, price: float
) -> Dict[str, Any]:
    override = TOP10_TOKEN_OVERRIDES.get(contract)
    symbol, _ = top10_lookup_symbol(contract)
    params = {'keyword': override[1] if override else symbol}
    if override:
        params['chainIds'] = str(override[0])
    data = await fetch_json(session, TOP10_SEARCH_URL + '?' + urlencode(params), timeout=12)
    return select_top10_token(data, contract, price, override)


def format_top10(ratio: Optional[float]) -> str:
    return '暂无数据' if ratio is None else f'{ratio:.2%}'


async def add_top10_holders(session: aiohttp.ClientSession, results: List[Dict[str, Any]]):
    if not results:
        return
    print('\n正在查询链上 Top10 持币占比（名称及价格匹配，详见 CSV 来源列）...')
    semaphore = asyncio.Semaphore(TOP10_CONCURRENCY)

    async def update(item):
        async with semaphore:
            await asyncio.sleep(0.3)
            item.update(await get_top10_holders(session, item['symbol'], item['price']))

    await asyncio.gather(*(update(item) for item in results))
    count = sum(item['top10_holders_ratio'] is not None for item in results)
    print(f'Top10 数据覆盖：{count}/{len(results)}；缺失或匹配不唯一的币种显示暂无数据。')

async def get_usdt_contracts(
        session: aiohttp.ClientSession
) -> List[str]:
    """
    获取币安所有 U 本位永续合约交易对
    """
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"

    data = await fetch_json(session, url)

    if not data or 'symbols' not in data:
        return []

    usdt_contracts = []

    for symbol_info in data['symbols']:

        if (
                symbol_info.get('contractType') == 'PERPETUAL'
                and symbol_info.get('quoteAsset') == 'USDT'
                and symbol_info.get('status') == 'TRADING'
        ):
            usdt_contracts.append(
                symbol_info['symbol']
            )

    # 保留你原来的逻辑
    return usdt_contracts[20:]


async def get_binance_spot_symbols(
        session: aiohttp.ClientSession
) -> Set[str]:
    """
    获取币安当前正在交易的 USDT 现货交易对

    返回例如：
    {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        ...
    }
    """

    url = "https://api.binance.com/api/v3/exchangeInfo"

    data = await fetch_json(session, url)

    spot_symbols = set()

    if not data or 'symbols' not in data:
        return spot_symbols

    for item in data['symbols']:

        # 必须是正在交易
        if item.get('status') != 'TRADING':
            continue

        # 只关心 USDT 现货
        if item.get('quoteAsset') != 'USDT':
            continue

        symbol = item.get('symbol')

        if symbol:
            spot_symbols.add(symbol)

    return spot_symbols


async def get_binance_funding_rates(
        session: aiohttp.ClientSession
) -> Dict[str, float]:
    """
    获取币安所有合约的最新资金费率
    """

    url = "https://fapi.binance.com/fapi/v1/premiumIndex"

    data = await fetch_json(session, url)

    rates = {}

    if data and isinstance(data, list):

        for item in data:
            rate_val = (
                item.get('fundingRate')
                if 'fundingRate' in item
                else item.get('lastFundingRate', 0)
            )

            rates[item['symbol']] = float(
                rate_val or 0
            )

    return rates


async def get_all_prices(
        session: aiohttp.ClientSession
) -> Dict[str, float]:
    """
    获取币安合约当前价格表
    """

    url = "https://fapi.binance.com/fapi/v1/ticker/price"

    data = await fetch_json(session, url)

    if data and isinstance(data, list):
        return {
            item['symbol']: float(item['price'])
            for item in data
        }

    return {}


# ============================================================
# 全量 API 接口拉取
# ============================================================

async def get_okx_all_oi(
        session: aiohttp.ClientSession
) -> Dict[str, float]:
    """
    【全量】
    获取 OKX 所有 SWAP 永续合约持仓
    """

    url = (
        "https://www.okx.com/api/v5/public/"
        "open-interest?instType=SWAP"
    )

    data = await fetch_json(session, url)

    okx_oi_map = {}

    if (
            data
            and data.get('code') == '0'
            and data.get('data')
    ):

        for item in data['data']:

            inst_id = item.get('instId', '')

            if inst_id.endswith('-USDT-SWAP'):

                base_coin = inst_id.split('-')[0]

                if (
                        'oiCcy' in item
                        and item['oiCcy']
                ):

                    oi_amount = float(
                        item['oiCcy']
                    )

                else:

                    ct_val = float(
                        item.get('ctVal', 1)
                    )

                    oi_amount = (
                            float(
                                item.get('oi', 0)
                            )
                            * ct_val
                    )

                okx_oi_map[
                    base_coin
                ] = oi_amount

    return okx_oi_map


async def get_hyperliquid_all_oi(
        session: aiohttp.ClientSession
) -> Dict[str, float]:
    """
    【全量】
    获取 Hyperliquid 所有永续合约持仓量
    """

    url = "https://api.hyperliquid.xyz/info"

    payload = {
        "type": "metaAndAssetCtxs"
    }

    data = await fetch_json(
        session,
        url,
        method='POST',
        json_data=payload
    )

    hl_oi_map = {}

    if (
            data
            and isinstance(data, list)
            and len(data) == 2
    ):

        universe = data[0].get(
            'universe',
            []
        )

        asset_ctxs = data[1]

        for idx, asset in enumerate(
                universe
        ):

            name = asset.get('name')

            if idx < len(asset_ctxs):
                open_interest = float(
                    asset_ctxs[idx].get(
                        'openInterest',
                        0
                    )
                )

                hl_oi_map[
                    name
                ] = open_interest

    return hl_oi_map


# ============================================================
# 单币种 API
# ============================================================

async def get_binance_oi(
        session: aiohttp.ClientSession,
        symbol: str
) -> Tuple[float, float, float]:
    """
    获取币安单币种持仓量及 CMC 流通量
    """

    url = (
        "https://fapi.binance.com/futures/data/"
        f"openInterestHist?symbol={symbol}"
        "&period=1h&limit=1"
    )

    data = await fetch_json(
        session,
        url
    )

    if (
            data
            and isinstance(data, list)
            and len(data) > 0
    ):
        return (
            float(
                data[0].get(
                    'sumOpenInterest',
                    0
                )
            ),
            float(
                data[0].get(
                    'sumOpenInterestValue',
                    0
                )
            ),
            float(
                data[0].get(
                    'CMCCirculatingSupply',
                    0
                )
            )
        )

    return 0.0, 0.0, 0.0


async def get_bybit_oi(
        session: aiohttp.ClientSession,
        symbol: str,
        price: float
) -> Tuple[float, float]:
    """
    获取 Bybit 单币种持仓量
    """

    url = (
        "https://api.bybit.com/v5/market/"
        f"open-interest?category=linear"
        f"&symbol={symbol}"
        "&intervalTime=5min"
        "&limit=1"
    )

    data = await fetch_json(
        session,
        url
    )

    if (
            data
            and data.get('retCode') == 0
            and data.get(
        'result',
        {}
    ).get('list')
    ):
        item = data[
            'result'
        ]['list'][0]

        oi_amount = float(
            item.get(
                'openInterest',
                0
            )
        )

        return (
            oi_amount,
            oi_amount * price
        )

    return 0.0, 0.0


async def get_bitget_oi(
        session: aiohttp.ClientSession,
        symbol: str,
        price: float
) -> Tuple[float, float]:
    """
    获取 Bitget 单币种持仓量
    """

    url = (
        "https://api.bitget.com/api/v2/"
        "mix/market/open-interest"
        f"?symbol={symbol}"
        "&productType=usdt-futures"
    )

    data = await fetch_json(
        session,
        url
    )

    if (
            data
            and data.get('code') == '00000'
            and data.get(
        'data',
        {}
    ).get('openInterestList')
    ):
        item = data[
            'data'
        ]['openInterestList'][0]

        oi_amount = float(
            item.get(
                'size',
                0
            )
        )

        return (
            oi_amount,
            oi_amount * price
        )

    return 0.0, 0.0


async def get_gate_oi(
        session: aiohttp.ClientSession,
        contract: str,
        price: float
) -> Tuple[float, float]:
    """
    获取 Gate.io 单币种持仓量
    """

    gate_symbol = (
        f"{contract.replace('USDT', '')}"
        "_USDT"
    )

    url = (
        "https://api.gateio.ws/api/v4/"
        "futures/usdt/contract_stats"
        f"?contract={gate_symbol}"
        "&limit=1"
    )

    data = await fetch_json(
        session,
        url
    )

    if (
            data
            and isinstance(data, list)
            and len(data) > 0
    ):

        latest = data[-1]

        if (
                'open_interest_usd' in latest
                and float(
            latest['open_interest_usd']
        ) > 0
        ):

            oi_value = float(
                latest[
                    'open_interest_usd'
                ]
            )

            oi_amount = (
                oi_value / price
                if price > 0
                else 0.0
            )

            return (
                oi_amount,
                oi_value
            )

        elif 'open_interest' in latest:

            oi_amount = float(
                latest[
                    'open_interest'
                ]
            )

            return (
                oi_amount,
                oi_amount * price
            )

    return 0.0, 0.0


# ============================================================
# K线形态分析
# ============================================================

async def analyze_test_pump_and_triple_bottom(
        session: aiohttp.ClientSession,
        symbol: str,
        current_price: float
) -> Tuple[bool, bool, float, bool]:
    """
    K 线图深度分析（90天 / 3个月）

    1. 判断是否形成三重底
    2. 判断是否出现测试泵/试盘
    3. 计算90天最大波段涨幅
    4. 判断是否符合暴涨前蓄势形态

    返回：
    (
        是否三重底,
        是否测试泵,
        90天最大涨幅,
        是否匹配暴涨前形态
    )
    """

    url = (
        "https://fapi.binance.com/fapi/v1/"
        f"klines?symbol={symbol}"
        "&interval=1d"
        "&limit=90"
    )

    data = await fetch_json(
        session,
        url
    )

    if (
            not data
            or not isinstance(data, list)
            or len(data) < 60
    ):
        return (
            False,
            False,
            0.0,
            False
        )

    klines = [
        {
            'open': float(k[1]),
            'high': float(k[2]),
            'low': float(k[3]),
            'close': float(k[4])
        }
        for k in data
    ]

    n = len(klines)

    # ========================================================
    # 算法1：三重底
    # ========================================================

    seg1 = klines[
        :n // 3
    ]

    seg2 = klines[
        n // 3:
        2 * n // 3
    ]

    seg3 = klines[
        2 * n // 3:
    ]

    l1 = min(
        k['low']
        for k in seg1
    )

    l2 = min(
        k['low']
        for k in seg2
    )

    l3 = min(
        k['low']
        for k in seg3
    )

    min_bottom = min(
        l1,
        l2,
        l3
    )

    max_bottom = max(
        l1,
        l2,
        l3
    )

    if min_bottom > 0:

        is_triple_bottom = (
                (
                        max_bottom
                        - min_bottom
                )
                / min_bottom
                <= 0.06
        )

    else:

        is_triple_bottom = False

    # ========================================================
    # 算法2：测试泵 / 试盘
    # ========================================================

    has_test_pump = False

    max_surge = 0.0

    for i in range(n):

        k = klines[i]

        if k['low'] > 0:

            daily_pump = (
                    (
                            k['high']
                            - k['low']
                    )
                    / k['low']
            )

        else:

            daily_pump = 0

        if daily_pump >= 0.18:
            has_test_pump = True

        # 计算整个90天最大波段拉升
        for j in range(
                i,
                n
        ):

            if k['low'] > 0:

                surge = (
                        (
                                klines[j]['high']
                                - k['low']
                        )
                        / k['low']
                )

            else:

                surge = 0

            if surge > max_surge:
                max_surge = surge

    # ========================================================
    # 算法3：蓄势暴涨前形态
    # ========================================================

    max_high_90d = max(
        k['high']
        for k in klines
    )

    min_low_90d = min(
        k['low']
        for k in klines
    )

    price_range = (
            max_high_90d
            - min_low_90d
    )

    if price_range > 0:

        rel_pos = (
                (
                        current_price
                        - min_low_90d
                )
                / price_range
        )

    else:

        rel_pos = 0

    is_pre_pump_pattern = (
            rel_pos <= 0.90
            and (
                    is_triple_bottom
                    or has_test_pump
            )
            and max_surge < 0.60
    )

    return (
        is_triple_bottom,
        has_test_pump,
        max_surge,
        is_pre_pump_pattern
    )


# ============================================================
# 格式化
# ============================================================

def format_m(
        amount: float
) -> str:
    """
    金额转换为百万(M)
    """

    return (
        f"{amount / 1_000_000:.2f}M"
    )


# ============================================================
# 单币种处理
# ============================================================

async def process_single_contract(
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        contract: str,
        price: float,
        funding_rate: float,
        okx_oi_map: Dict[str, float],
        hl_oi_map: Dict[str, float],
        binance_spot_symbols: Set[str]
) -> Optional[Dict[str, Any]]:
    """
    单个币种处理
    """

    async with semaphore:
        base_coin = contract.replace(
            'USDT',
            ''
        )

        # ----------------------------------------------------
        # 是否上币安现货
        # ----------------------------------------------------

        is_binance_spot = (
                contract
                in binance_spot_symbols
        )

        # ----------------------------------------------------
        # 并发查询
        # ----------------------------------------------------

        bn_task = get_binance_oi(
            session,
            contract
        )

        bybit_task = get_bybit_oi(
            session,
            contract,
            price
        )

        bitget_task = get_bitget_oi(
            session,
            contract,
            price
        )

        gate_task = get_gate_oi(
            session,
            contract,
            price
        )

        pattern_task = (
            analyze_test_pump_and_triple_bottom(
                session,
                contract,
                price
            )
        )

        (
            (
                bn_oi,
                bn_value,
                cmc_supply
            ),
            (
                bybit_oi,
                bybit_value
            ),
            (
                bitget_oi,
                bitget_value
            ),
            (
                gate_oi,
                gate_value
            ),
            (
                is_triple_bottom,
                has_test_pump,
                max_90d_surge,
                is_pre_pump
            )
        ) = await asyncio.gather(
            bn_task,
            bybit_task,
            bitget_task,
            gate_task,
            pattern_task
        )

        # ----------------------------------------------------
        # 流通市值
        # ----------------------------------------------------

        circulating_market_cap = (
                cmc_supply
                * price
        )

        if circulating_market_cap == 0:
            return None

        # ----------------------------------------------------
        # OKX
        # ----------------------------------------------------

        okx_value = (
                okx_oi_map.get(
                    base_coin,
                    0.0
                )
                * price
        )

        # ----------------------------------------------------
        # Hyperliquid
        # ----------------------------------------------------

        hl_value = (
                hl_oi_map.get(
                    base_coin,
                    0.0
                )
                * price
        )

        # ----------------------------------------------------
        # 总持仓
        # ----------------------------------------------------

        total_oi_value = (
                bn_value
                + bybit_value
                + bitget_value
                + okx_value
                + gate_value
                + hl_value
        )

        # ----------------------------------------------------
        # 返回
        # ----------------------------------------------------

        return {

            'symbol':
                contract,

            'price':
                price,

            'funding_rate':
                funding_rate,

            # 新增
            'is_binance_spot':
                is_binance_spot,

            'circulating_market_cap':
                circulating_market_cap,

            'bn_value':
                bn_value,

            'bybit_value':
                bybit_value,

            'bitget_value':
                bitget_value,

            'okx_value':
                okx_value,

            'gate_value':
                gate_value,

            'hl_value':
                hl_value,

            'total_oi_value':
                total_oi_value,

            'total_ratio':
                (
                        total_oi_value
                        / circulating_market_cap
                ),

            'bn_ratio':
                (
                        bn_value
                        / circulating_market_cap
                ),

            'bybit_ratio':
                (
                        bybit_value
                        / circulating_market_cap
                ),

            'bitget_ratio':
                (
                        bitget_value
                        / circulating_market_cap
                ),

            'okx_ratio':
                (
                        okx_value
                        / circulating_market_cap
                ),

            'gate_ratio':
                (
                        gate_value
                        / circulating_market_cap
                ),

            'hl_ratio':
                (
                        hl_value
                        / circulating_market_cap
                ),

            'is_triple_bottom':
                is_triple_bottom,

            'has_test_pump':
                has_test_pump,

            'max_90d_surge':
                max_90d_surge,

            'is_pre_pump':
                is_pre_pump
        }


# ============================================================
# MAIN
# ============================================================

async def main_async():
    start_time = time.time()

    headers = {
        'User-Agent':
            'Mozilla/5.0 '
            '(Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36'
    }

    async with aiohttp.ClientSession(
            headers=headers,
            trust_env=True
    ) as session:

        print(
            "正在获取基础数据及全量持仓汇总..."
        )

        # ----------------------------------------------------
        # 基础数据
        # ----------------------------------------------------

        contracts_task = (
            get_usdt_contracts(
                session
            )
        )

        prices_task = (
            get_all_prices(
                session
            )
        )

        funding_task = (
            get_binance_funding_rates(
                session
            )
        )

        okx_all_task = (
            get_okx_all_oi(
                session
            )
        )

        hl_all_task = (
            get_hyperliquid_all_oi(
                session
            )
        )

        # 新增
        binance_spot_task = (
            get_binance_spot_symbols(
                session
            )
        )

        (
            contracts,
            prices,
            funding_rates,
            okx_oi_map,
            hl_oi_map,
            binance_spot_symbols
        ) = await asyncio.gather(

            contracts_task,
            prices_task,
            funding_task,
            okx_all_task,
            hl_all_task,
            binance_spot_task
        )

        # ----------------------------------------------------
        # 基础信息
        # ----------------------------------------------------

        total = len(
            contracts
        )

        print(
            f"获取到 {total} 个 U 本位合约"
        )

        print(
            f"获取到 {len(binance_spot_symbols)} "
            "个币安 USDT 现货交易对"
        )

        print(
            "开始抓取多交易所持仓量及"
            "【测试泵 + 三重底】形态识别..."
        )

        if total == 0:
            print(
                "\n未获取到任何合约数据，"
                "请检查代理工具设置！"
            )

            return

        semaphore = asyncio.Semaphore(
            CONCURRENCY_LIMIT
        )

        tasks = []

        # ----------------------------------------------------
        # 创建任务
        # ----------------------------------------------------

        for contract in contracts:

            price = prices.get(
                contract,
                0.0
            )

            if price == 0:
                continue

            funding_rate = (
                funding_rates.get(
                    contract,
                    0.0
                )
            )

            task = process_single_contract(

                session,

                semaphore,

                contract,

                price,

                funding_rate,

                okx_oi_map,

                hl_oi_map,

                binance_spot_symbols
            )

            tasks.append(
                task
            )

        # ----------------------------------------------------
        # 执行
        # ----------------------------------------------------

        task_results = (
            await asyncio.gather(
                *tasks
            )
        )

        results = [
            r
            for r in task_results
            if r is not None
        ]

        await add_top10_holders(session, results)

    # ========================================================
    # 排序
    # ========================================================

    results.sort(
        key=lambda x:
        x['total_oi_value'],
        reverse=True
    )

    elapsed_time = (
            time.time()
            - start_time
    )

    print(
        f"\n全量数据抓取完成！"
        f"耗时: {elapsed_time:.2f} 秒"
    )

    # ========================================================
    # 重点目标
    # ========================================================

    targets = [

        r

        for r in results

        if (
                r['is_pre_pump']
                and r['total_ratio'] >= 0.30
        )
    ]

    print(
        "\n"
        + "=" * 120
    )

    print(
        "🎯 重点暴涨前预选目标"
        "（测试泵/三重底 + 高持仓占比）"
        f"：共找到 {len(targets)} 个币种"
    )

    print(
        "=" * 120
    )

    for t in targets[:15]:

        pattern_desc = []

        if t['is_triple_bottom']:
            pattern_desc.append(
                "三重底"
            )

        if t['has_test_pump']:
            pattern_desc.append(
                "有测试泵/试盘"
            )

        desc_str = "+".join(
            pattern_desc
        )

        spot_str = (
            "是"
            if t['is_binance_spot']
            else "否"
        )

        print(

            f"-> {t['symbol']:<10} "

            f"| 币安现货:{spot_str:<2} "

            f"| 形态:{desc_str:<12} "

            f"| 总持仓:"
            f"${format_m(t['total_oi_value'])} "

            f"| 持仓市值比:"
            f"{t['total_ratio']:.2%} "
            f"| 链上Top10:{format_top10(t['top10_holders_ratio'])}"
        )

    # ========================================================
    # 排行榜
    # ========================================================

    print(
        "\n"
        + "=" * 220
    )

    print(
        "多交易所持仓汇总排行榜 "
        "(单位:百万/M)"
    )

    print(
        "=" * 220
    )

    print(

        f"{'排名':<5} "

        f"{'交易对':<10} "

        f"{'币安现货':<8} "

        f"{'资金费率':<10} "

        f"{'总持仓(M)':<12} "

        f"{'币安(M)':<10} "

        f"{'Bybit(M)':<10} "

        f"{'Bitget(M)':<10} "

        f"{'OKX(M)':<10} "

        f"{'Gate(M)':<10} "

        f"{'Hyper(M)':<10} "

        f"{'流通市值(M)':<12} "

        f"{'总持仓比':<10} "

        f"{'链上Top10占比':<14} "

        f"{'状态/形态标记':<35}"
    )

    print(
        "=" * 220
    )

    for rank, item in enumerate(
            results,
            1
    ):

        flags = []

        if item['is_triple_bottom']:
            flags.append(
                "底:三重底"
            )

        if item['has_test_pump']:
            flags.append(
                "🚀测试泵试盘"
            )

        if item['total_ratio'] > 1.0:

            flags.append(
                "🚨总持仓>市值"
            )

        elif item['total_ratio'] > 0.5:

            flags.append(
                "⚠️总持仓>50%市值"
            )

        flag_str = (
            " | ".join(flags)
            if flags
            else "正常"
        )

        fr_str = (
            f"{item['funding_rate']:.2%}"
        )

        spot_str = (
            "是"
            if item['is_binance_spot']
            else "否"
        )

        print(

            f"{rank:<5} "

            f"{item['symbol']:<10} "

            f"{spot_str:<8} "

            f"{fr_str:<10} "

            f"{format_m(item['total_oi_value']):<12} "

            f"{format_m(item['bn_value']):<10} "

            f"{format_m(item['bybit_value']):<10} "

            f"{format_m(item['bitget_value']):<10} "

            f"{format_m(item['okx_value']):<10} "

            f"{format_m(item['gate_value']):<10} "

            f"{format_m(item['hl_value']):<10} "

            f"{format_m(item['circulating_market_cap']):<12} "

            f"{item['total_ratio']:.2%}       "

            f"{format_top10(item['top10_holders_ratio']):<14} "

            f"{flag_str}"
        )

    # ========================================================
    # CSV
    # ========================================================

    with open(
            'multi_exchange_oi_ranking.csv',
            'w',
            newline='',
            encoding='utf-8-sig'
    ) as f:

        fieldnames = [

            '排名',

            '交易对',

            '币安现货',

            '流通市值(M)',

            '总持仓市值比',

            'Top10持仓占比',
            'Top10数据链',
            'Top10合约地址',
            'Top10匹配状态',

            '90天波段最大拉升',

            '是否三重底',

            '是否有测试泵试盘',

            '是否匹配暴涨前形态',

            '价格',

            '资金费率',

            '总持仓(M)',

            '币安(M)',

            'bybit(M)',

            'bitget(M)',

            'okx(M)',

            'gate(M)',

            'hyperliquid(M)'
        ]

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()

        for rank, item in enumerate(
                results,
                1
        ):
            writer.writerow({

                '排名':
                    rank,

                '交易对':
                    item['symbol'],

                '币安现货':
                    (
                        '是'
                        if item['is_binance_spot']
                        else '否'
                    ),

                '流通市值(M)':
                    f"{item['circulating_market_cap'] / 1_000_000:.2f}",

                '总持仓市值比':
                    round(
                        item['total_ratio'],
                        4
                    ),

                '90天波段最大拉升':
                    f"{item['max_90d_surge']:.2%}",

                'Top10持仓占比': format_top10(item['top10_holders_ratio']),
                'Top10数据链': item['top10_chain'],
                'Top10合约地址': item['top10_address'],
                'Top10匹配状态': item['top10_status'],

                '是否三重底':
                    item['is_triple_bottom'],

                '是否有测试泵试盘':
                    item['has_test_pump'],

                '是否匹配暴涨前形态':
                    item['is_pre_pump'],

                '价格':
                    item['price'],

                '资金费率':
                    f"{item['funding_rate']:.2%}",

                '总持仓(M)':
                    f"{item['total_oi_value'] / 1_000_000:.2f}",

                '币安(M)':
                    f"{item['bn_value'] / 1_000_000:.2f}",

                'bybit(M)':
                    f"{item['bybit_value'] / 1_000_000:.2f}",

                'bitget(M)':
                    f"{item['bitget_value'] / 1_000_000:.2f}",

                'okx(M)':
                    f"{item['okx_value'] / 1_000_000:.2f}",

                'gate(M)':
                    f"{item['gate_value'] / 1_000_000:.2f}",

                'hyperliquid(M)':
                    f"{item['hl_value'] / 1_000_000:.2f}"
            })

    print(
        "\n全量分析结果已保存到 "
        "multi_exchange_oi_ranking.csv"
    )

    # ========================================================
    # 额外统计：币安只有合约，没有现货
    # ========================================================

    no_spot_results = [

        r

        for r in results

        if not r['is_binance_spot']
    ]

    print(
        f"\n币安有永续合约但没有 USDT 现货："
        f"{len(no_spot_results)} 个"
    )

    if no_spot_results:

        print(
            "前20个："
        )

        for item in no_spot_results[:20]:
            print(

                f"{item['symbol']:<12} "

                f"总持仓:"
                f"${format_m(item['total_oi_value'])} "

                f"持仓/市值:"
                f"{item['total_ratio']:.2%}"
            )


# ============================================================
# 启动
# ============================================================

def main():
    asyncio.run(
        main_async()
    )


if __name__ == "__main__":
    main()
