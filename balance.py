"""DeepSeek 账户余额查询：GET {base_url}/user/balance"""

import asyncio
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
CURRENCY_SYMBOLS = {"CNY": "¥", "USD": "$"}
ERROR_CACHE_SECONDS = 30.0


class BalanceError(Exception):
    pass


@dataclass
class BalanceInfo:
    currency: str
    total: str
    granted: str
    topped_up: str


@dataclass
class Balance:
    is_available: bool
    infos: list[BalanceInfo] = field(default_factory=list)
    fetched_at: float = 0.0


def normalize_base_url(url: str | None) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_BASE_URL
    if "://" not in url:
        url = "https://" + url
    # AstrBot 里的 DeepSeek 提供商常写成 https://api.deepseek.com/v1，余额接口不在 /v1 下
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def normalize_proxy(proxy: str | None) -> str | None:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    return proxy if "://" in proxy else "http://" + proxy


def parse_balance(data) -> Balance:
    if not isinstance(data, dict) or "balance_infos" not in data:
        raise BalanceError(f"返回格式无法识别：{str(data)[:200]}")
    infos = []
    for item in data.get("balance_infos") or []:
        if not isinstance(item, dict):
            continue
        infos.append(BalanceInfo(
            currency=str(item.get("currency") or ""),
            total=str(item.get("total_balance") or "0"),
            granted=str(item.get("granted_balance") or "0"),
            topped_up=str(item.get("topped_up_balance") or "0"),
        ))
    return Balance(is_available=bool(data.get("is_available")), infos=infos, fetched_at=time.time())


def format_balance(balance: Balance) -> str:
    if not balance.infos:
        text = "暂无余额信息"
    else:
        parts = []
        for info in balance.infos:
            symbol = CURRENCY_SYMBOLS.get(info.currency.upper(), "")
            unit = "" if symbol else f" {info.currency}"
            parts.append(
                f"{symbol}{info.total}{unit}（充值 {info.topped_up}，赠金 {info.granted}）"
            )
        text = "；".join(parts)
    state = "可正常调用" if balance.is_available else "余额不足，当前无法调用 API"
    return f"{text}，{state}"


class DeepSeekBalanceClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self._cache: dict[tuple[str, str], Balance] = {}
        self._errors: dict[tuple[str, str], tuple[float, BalanceError]] = {}
        self._lock = asyncio.Lock()

    async def fetch(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        proxy: str | None = None,
        timeout: float = 15.0,
        cache_seconds: float = 0.0,
    ) -> Balance:
        if not api_key:
            raise BalanceError("未配置 DeepSeek API Key")
        # 混入全角或不可见字符时 httpx 会在报错信息里带出整个请求头，提前拦下
        if not (api_key.isascii() and api_key.isprintable()):
            raise BalanceError("API Key 含有全角或不可见字符，请重新粘贴")
        base_url = normalize_base_url(base_url)
        key = (api_key, base_url)
        # 加锁避免多个会话同时提问时重复请求；失败结果也短暂缓存，免得排队的请求逐个等到超时
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cache_seconds > 0 and time.time() - cached.fetched_at < cache_seconds:
                return cached
            failed = self._errors.get(key)
            if failed and time.time() - failed[0] < ERROR_CACHE_SECONDS:
                raise failed[1]
            try:
                balance = await self._request(api_key, base_url, normalize_proxy(proxy), timeout)
            except BalanceError as e:
                self._errors[key] = (time.time(), e)
                raise
            self._errors.pop(key, None)
            self._cache[key] = balance
            return balance

    async def _request(self, api_key: str, base_url: str, proxy: str | None, timeout: float) -> Balance:
        kwargs = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        elif proxy:
            kwargs["proxy"] = proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                resp = await client.get(
                    f"{base_url}/user/balance",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                )
        except httpx.TimeoutException as e:
            raise BalanceError("请求 DeepSeek 超时") from e
        # 不把异常原文放进提示：它会进入日志和 LLM 能看到的工具结果
        except httpx.HTTPError as e:
            raise BalanceError(f"网络错误（{type(e).__name__}）") from e
        except Exception as e:  # InvalidURL、未知代理协议等配置错误不属于 HTTPError
            raise BalanceError(f"请求参数有误（{type(e).__name__}），请检查 API 地址和代理配置") from e

        body = resp.text[:200].replace(api_key, "***")
        if resp.status_code == 401:
            raise BalanceError("DeepSeek API Key 无效（401）")
        if resp.status_code >= 400:
            raise BalanceError(f"DeepSeek 返回 HTTP {resp.status_code}：{body}")
        try:
            data = resp.json()
        except ValueError as e:
            raise BalanceError(f"返回内容不是 JSON：{body}") from e
        return parse_balance(data)
