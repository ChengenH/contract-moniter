# contract-moniter

基于 Python 的多交易所永续合约持仓分析脚本。以币安正在交易的 USDT 永续合约为扫描范围，汇总 Binance、Bybit、Bitget、OKX、Gate.io 和 Hyperliquid 的持仓数据，计算持仓与流通市值的比例，结合日 K 线规则筛选候选交易对，并导出 CSV。

## 主要功能

- 汇总六家交易所的持仓金额，按总持仓金额从高到低排序。
- 展示币安合约价格、资金费率及是否存在同名且正在交易的币安 USDT 现货交易对。
- 使用币安返回的 `CMCCirculatingSupply` 与合约价格估算流通市值，计算总持仓市值比。
- 获取最近最多 90 根日 K 线，识别脚本定义的“三重底”“测试泵/试盘”和组合形态。
- 在终端展示重点候选、完整排行榜，以及有币安永续合约但没有匹配 USDT 现货的交易对。
- 将完整分析结果保存为带 UTF-8 BOM 的 CSV，便于使用 Excel 打开。

以上行情分析脚本每次运行执行一轮扫描后退出，没有消息推送或自动下单功能，也不需要配置 API Key。主网兑换脚本 `base_swap.py` 的使用方式见下文。

## 项目结构

```text
contract-moniter/
├── binance.py                     # 数据获取、形态分析、排行和导出
├── alpha_fdv.py                   # Alpha 低 FDV 交易对筛选（仅标准库）
├── README.md                      # 使用说明
└── multi_exchange_oi_ranking.csv   # 运行后生成
```

## Alpha：查询低 FDV 交易对和 Top10 持币占比

`alpha_fdv.py` 使用 Python 3 标准库，无需安装第三方依赖或配置 API Key：

```powershell
python alpha_fdv.py
# 只看 USDT 交易对
python alpha_fdv.py --quote USDT
# 自定义美元上限和输出文件
python alpha_fdv.py --max-fdv 500000 --output alpha_under_500k.csv
```

脚本读取 Alpha 代币列表中的 `fdv`，默认按 **`0 < FDV < 1500000`** 筛选，再将 `alphaId` 与交易信息的 `baseAsset` 匹配，仅输出状态为 `TRADING` 的真实交易对。默认包含所有计价币种，按 FDV 升序排列；同一代币有多个交易对时分别显示。FDV 缺失、非数字、非有限或非正数的记录会跳过，不使用流通市值代替 FDV。

终端和 CSV 新增 `Top10占比`，例如 `50.23%`。按链 ID 和合约地址查询并精确匹配币安公开 token search 接口的 `holdersTop10Percent`，直接使用接口提供的前 10 大持币地址合计百分比，不额外排除合约、交易所或流动性池地址。同一币种的多个交易对共用一次查询；缺失、无效或查询失败时 CSV 留空，终端显示 `N/A` 并输出警告。接口说明见 [Binance query-token-info](https://www.binance.com/en/skills/detail/binance-web3/query-token-info)。

终端显示代币交易对、API 交易对代码（例如 `ALPHA_175USDT`）、美元 FDV 和链。完整结果写入当前工作目录的 `alpha_low_fdv.csv`，包含价格、流通市值、合约地址和 UTC 查询时间，采用 UTF-8 BOM 编码，覆盖同名文件。筛选成功但无匹配时仍导出表头；接口失败会报错并以非零状态退出，不会按“零个结果”处理。

使用 Binance [Alpha Market Data 官方接口](https://developers.binance.com/en/docs/catalog/advanced-trading-alpha-trading/api/rest-api/market-data)：

- `/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list`
- `/bapi/defi/v1/public/alpha-trade/get-exchange-info`

请求超时为 20 秒，网络错误、HTTP 429 或服务端错误最多尝试 3 次。支持 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，配置方式见下文。FDV 是接口查询时的代币估值，两个接口并非同时采样；本脚本范围是 Alpha 交易信息接口列出的交易对，不包含仅出现在链上代币列表而没有匹配交易对的项目。

## Base 主网：ETH → USDC → ETH 往返兑换

`base_swap.py` 使用 Uniswap V3 的 ETH/WETH–原生 USDC 单池兑换，默认参数为：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--amount-eth` | `0.001` | 每轮投入的 ETH；后续各轮仍投入固定金额 |
| `--rounds` | `100` | 往返轮数，即 200 次 swap，另有 USDC 授权交易 |
| `--interval` | `3` | 上一笔交易确认成功后再等待的秒数，授权后也等待；实际发送间隔大于 3 秒 |
| `--pool-fee` | `500` | V3 单池费率档位，表示每次兑换 0.05%，不自动寻找其他路线 |
| `--slippage-bps` | `50` | 每次报价允许 0.5% 滑点 |
| `--max-round-loss-bps` | `100` | 回程最低 ETH 为投入的 99%，不含 Gas；报价不满足时停止，保留 USDC |
| `--max-gas-gwei` | `1` | L2 `maxFeePerGas` 上限，不是总手续费上限 |
| `--gas-reserve-eth` | `0.0002` | 每次发送前额外保留的 ETH，供后续 Gas / L1 数据费使用 |

该脚本默认只做一次当前行情的往返报价预览，不使用私钥、不签名、不发送交易。预览不是 100 轮历史回测，也不是有资金钱包的完整交易模拟。

### 安装和预览

Python 3.10+，使用单独的虚拟环境：

```powershell
python -m venv .venv-swap
.\.venv-swap\Scripts\python.exe -m pip install -r requirements-swap.txt
# 可选：使用你自己的 Base 主网 RPC；未设置时使用 https://mainnet.base.org
$env:BASE_RPC_URL = "https://mainnet.base.org"
.\.venv-swap\Scripts\python.exe base_swap.py
```

### 实际执行

只支持可用私钥签名的普通 EOA 钱包，不支持交易所账户、多签或智能账户。钱包需要有 Base 主网 ETH，本金会循环使用，但要另留足够 ETH 支付累计兑换损耗和手续费。

可以直接在 `base_swap.py` 顶部的 `PRIVATE_KEY = ""` 引号内填入私钥，保存后执行：

```powershell
.\.venv-swap\Scripts\python.exe base_swap.py --execute
```

代码中的 `PRIVATE_KEY` 优先；留空时使用环境变量。填入真实私钥后，不要提交或分享该文件。

也可以在 **PowerShell 7** 中，通过隐藏输入设置当前进程的私钥环境变量，然后执行：

```powershell
$env:BASE_PRIVATE_KEY = Read-Host "输入钱包私钥（不显示）" -MaskInput
.\.venv-swap\Scripts\python.exe base_swap.py --execute
Remove-Item Env:BASE_PRIVATE_KEY
```

也可以在本地 IDE 的运行环境变量中设置 `BASE_PRIVATE_KEY`，运行参数填写 `--execute`。不要把私钥发送到聊天；脚本不自动加载 `.env`。

显式指定本次需求的参数：

```powershell
.\.venv-swap\Scripts\python.exe base_swap.py --amount-eth 0.001 --rounds 100 --interval 3 --execute
```

每轮先执行 ETH → USDC，从该笔交易的 USDC 转账日志读取实际收到的数量，再仅将这部分 USDC 换回 ETH，不使用钱包已有 USDC 余额。授权不足时只授权本轮数量，因此从零授权开始，100 轮通常是 **200 笔 swap + 100 笔 approve**。回程的 WETH 解包和返还 ETH 在同一笔交易内完成。

每笔交易发送前校验待确认 nonce、估算 Gas 并模拟执行；交易使用最小接收数量和 120 秒链上截止时间。报价失败、余额不足、Gas 超限、回程价格损耗超限、交易失败或 180 秒确认超时都会停止。不会自动重发不确定状态的交易，也不会绕过滑点限制强行换回。

RPC 连接超时，以及 HTTP 408、429、500、502、503、504，会对列入白名单的只读请求额外重试最多 3 次，等待 2、4、8 秒。错误提示显示 RPC 方法和 HTTP 状态码，不显示可能包含密钥的 RPC URL。交易广播不会自动重试。持续出现 429 时，可在运行配置的环境变量中设置自己的 `BASE_RPC_URL`；401/403 需要检查 RPC 认证或访问权限。

### 日志和中断处理

实际执行时在当前目录创建 `base_swap_<钱包地址>.jsonl`。日志在广播前记录交易哈希、nonce 和发送步骤，并记录回执及每轮实际收到的 USDC 数量；不保存私钥或已签名交易原文。已有同名日志时拒绝执行，避免误重跑。

遇到中断或错误，先用日志中的哈希在 BaseScan 查询结果。已广播交易仍可能上链；买入已成功但卖出未完成时，USDC 会留在钱包，可在核对交易后自行兑换。如果授权成功但卖出失败，授权可能仍然存在。脚本目前不支持自动断点续跑。

确认上一轮运行状态并处理剩余资产后，如需开始新任务，可归档旧日志或显式指定新路径，例如 `--log base_swap_second.jsonl`。运行期间不要用同一钱包并行发送其他交易或启动多个脚本实例。

100 轮有累计池手续费、滑点、价格变化及网络手续费。`--max-round-loss-bps` 仅限制单轮兑换返回量，不包括 Gas，也不是整个任务的总损耗预算；Base 的 L1 数据费等附加费用不受 `--max-gas-gwei` 限制。保留余额不足时任务可能在 100 轮前停止。

合约来源：[Uniswap Base 部署表](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-base-deployments)、[Circle 原生 USDC 地址表](https://developers.circle.com/stablecoins/usdc-contract-addresses)。脚本固定校验 Base 主网 `chainId=8453`、合约存在、Router 的 WETH 地址以及 USDC 精度。

## 合约持仓脚本：安装与运行

需要 Python 3 和 `aiohttp`，并能够访问脚本使用的交易所公开接口。以下以 Windows PowerShell 为例，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install aiohttp
.\.venv\Scripts\python.exe binance.py
```

如果已有 Python 环境，也可以直接执行：

```powershell
python -m pip install aiohttp
python binance.py
```

项目当前没有依赖锁定文件，`aiohttp` 是脚本唯一的第三方依赖。

### 可选代理配置

脚本创建 `aiohttp.ClientSession` 时设置了 `trust_env=True`，可以读取代理环境变量。如果需要通过本地 HTTP 代理访问接口，可在当前 PowerShell 会话设置后运行：

```powershell
# 示例端口，请替换成实际 HTTP 代理地址
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
.\.venv\Scripts\python.exe binance.py
```

## 数据来源与计算口径

| 来源 | 脚本读取的数据 | 持仓金额处理 |
| --- | --- | --- |
| Binance | 合约列表、现货列表、价格、资金费率、持仓历史、流通量、日 K 线 | 使用 `sumOpenInterestValue`；持仓历史参数为 `period=1h&limit=1` |
| Bybit | 单交易对持仓量，参数为 `intervalTime=5min&limit=1` | 持仓量 × 币安合约价格 |
| Bitget | 单交易对 USDT 合约持仓量 | 持仓量 × 币安合约价格 |
| OKX | 全量 SWAP 持仓，保留 `-USDT-SWAP` | 优先读取 `oiCcy`，否则使用 `oi × ctVal`（缺失时 `ctVal` 默认为 1），再乘币安合约价格 |
| Gate.io | 单交易对合约统计 | 优先使用 `open_interest_usd`，否则使用 `open_interest × 币安合约价格` |
| Hyperliquid | `metaAndAssetCtxs` 返回的持仓量 | 按币种名称匹配后，持仓量 × 币安合约价格 |

```text
流通市值 = CMCCirculatingSupply × 币安合约价格
总持仓金额 = 六家交易所持仓金额之和
总持仓市值比 = 总持仓金额 / 流通市值
```

流通量来自币安接口返回的字段，脚本没有直接请求 CoinMarketCap。各交易所的数据时间和统计口径并不完全一致，汇总金额是脚本口径下的估算值。

## 形态与筛选规则

分析使用最多 90 根日 K 线；不足 60 根时，三个形态标记均返回 `False`，最大波段拉升返回 `0.0`。

| 指标 | 当前实现 |
| --- | --- |
| 三重底 | 将 K 线按时间分成三段，分别取最低价；三个低点的 `(最高值 - 最低值) / 最低值 ≤ 6%` |
| 测试泵/试盘 | 任意一根日 K 线的 `(最高价 - 最低价) / 最低价 ≥ 18%`，不要求收阳 |
| 最大波段拉升 | 遍历某日最低价与当日及后续各日最高价，取 `(后续最高价 - 起点最低价) / 起点最低价` 的最大值 |
| 匹配组合形态 | 当前价格在区间内的相对位置 `≤ 90%`，且满足“三重底或测试泵”，同时最大波段拉升 `< 60%` |
| 重点候选 | 匹配组合形态，且总持仓市值比 `≥ 30%` |

价格相对位置计算为 `(当前价格 - 区间最低价) / (区间最高价 - 区间最低价)`；区间价差为零时取 `0`。

代码及输出将组合形态称为“暴涨前形态”，这是规则标记名称，并不表示预测已经得到验证。三重底为分段低点比较；最大波段拉升包含同一根 K 线的高低点，无法确定日内高低点出现的先后顺序。

## 输出说明

### 终端

1. 基础数据数量与抓取耗时。
2. 重点候选数量和前 15 个候选，按总持仓金额降序展示。
3. 所有有效结果的持仓排行榜；金额单位为百万（`M`）。总持仓市值比 `> 100%` 或 `> 50%` 时显示相应标记。
4. 没有匹配币安 USDT 现货的交易对数量和前 20 个结果。

### CSV

文件名为 `multi_exchange_oi_ranking.csv`，保存在**运行命令时的当前工作目录**，每次成功执行到导出步骤时覆盖同名文件。导出包含所有有效分析结果，而非仅重点候选。

| 字段 | 含义与格式 |
| --- | --- |
| 排名、交易对 | 按总持仓金额降序排列的序号与币安合约代码 |
| 币安现货 | 是否匹配正在交易的同名 USDT 现货，值为“是”或“否” |
| 流通市值(M) | 估算流通市值，单位百万，保留两位小数 |
| 总持仓市值比 | 小数比例，保留四位小数；例如 `0.3` 表示 `30%` |
| 90天波段最大拉升 | 实际获取的 K 线区间内最大拉升，以百分比文本输出 |
| 是否三重底、是否有测试泵试盘 | `True` / `False` |
| 是否匹配暴涨前形态 | 组合形态标记；不包含重点候选要求的 `≥ 30%` 持仓市值比条件 |
| 价格、资金费率 | 币安合约价格与资金费率；资金费率为百分比文本 |
| 总持仓(M) | 六家交易所持仓金额之和，单位百万 |
| 币安(M)、bybit(M)、bitget(M)、okx(M)、gate(M)、hyperliquid(M) | 各交易所持仓金额，单位百万 |

## 调整参数

当前没有命令行参数或独立配置文件，需要修改 `binance.py` 中对应位置。

| 位置 | 默认值 | 作用 |
| --- | --- | --- |
| `CONCURRENCY_LIMIT` | `15` | 同时处理的币种数量；每个币种内部还会并发请求多个接口，不是全局 HTTP 请求上限 |
| `fetch_json` 的 `timeout` | `8` 秒 | 单次请求超时 |
| `get_usdt_contracts` 的返回切片 | `usdt_contracts[20:]` | **跳过接口返回的符合条件合约中的前 20 个**，并非按市值排除；扫描全部可改为 `return usdt_contracts` |
| K 线请求参数 | `interval=1d&limit=90` | K 线周期与数量 |
| `analyze_test_pump_and_triple_bottom` | `0.06`、`0.18`、`0.90`、`0.60` | 三重底容差、单日振幅、价格相对位置、最大拉升阈值 |
| `main_async` 的 `targets` 条件 | `total_ratio >= 0.30` | 重点候选的最低持仓市值比 |

## 已知限制与排查

- **缺失不等于零**：请求异常、超时或非 HTTP 200 响应会被 `fetch_json` 静默处理，没有重试和错误明细。部分缺失数据会以零、空集合或 `False` 继续计算，因此持仓为零或现货标记为“否”不一定代表真实状态。
- **部分交易对会被跳过**：除默认跳过前 20 个合约外，价格为零或估算流通市值为零的交易对也不会进入最终结果。
- **跨交易所名称和单位未统一校验**：脚本主要按交易对或基础币种名称直接匹配，没有专门处理币种别名、`1000` 前缀或合约乘数差异，特殊合约的匹配和换算需要另行核对。
- **采样并非同时发生**：持仓历史、当前价格和 K 线来自不同请求，最近日 K 线也可能尚未收盘。
- **提示未获取到合约数据**：检查网络、代理和币安接口可访问性；脚本提示检查代理，但实际也可能是接口异常或合约列表经切片后为空。
- **结果为空或某交易所全部为零**：检查该交易所接口是否可访问，以及价格、持仓历史和流通量是否返回。可在 `fetch_json` 中加入状态码及异常日志进一步定位。
- **CSV 无法写入**：确认当前目录可写，并关闭可能占用同名文件的 Excel 窗口后重试。
