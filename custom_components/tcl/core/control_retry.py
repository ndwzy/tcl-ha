"""控制下发的补发策略（纯标准库，便于单测）。

背景：TCL 云控是「云端等设备确认」模型。设备在活跃状态时 0.2~0.3s 就返回；
一旦这次事务在云端被挂起，客户端只能等到 60s 硬超时。实测（phase1，25/25）
用新 msgId 补发能让云端重新投递、设备在 0.1~0.2s 内执行，所以不必干等。

策略：
- 前 N-1 次各等 retry_timeout 秒，超时即切断并废弃该 msgId，换新 msgId 重发
- 第 N 次放宽到 final_timeout（60s 硬超时 + 余量），保证最终一定有结果
- 被废弃 msgId 的迟到回执由 AbandonRegistry 过滤掉
"""
import asyncio
import logging
import random
import time

_LOGGER = logging.getLogger(__name__)


def new_msgid() -> str:
    """生成与小程序一致的 msgId 格式：miniprogram_<随机>_<毫秒>。"""
    return "miniprogram_%d_%d" % (int(random.random() * 1e5), int(time.time() * 1000))


class AbandonRegistry:
    """记录已放弃的事务 msgId，在 TTL 窗口内忽略其回执。"""

    def __init__(self, ttl: float = 75.0):
        self._ttl = ttl
        self._map: dict[str, float] = {}

    def abandon(self, msg_id: str) -> None:
        now = time.monotonic()
        self._map[msg_id] = now + self._ttl
        for key, expire in list(self._map.items()):
            if expire < now:
                self._map.pop(key, None)

    def is_abandoned(self, msg_id: str) -> bool:
        expire = self._map.get(msg_id)
        if expire is None:
            return False
        if expire < time.monotonic():
            self._map.pop(msg_id, None)
            return False
        return True

    def __len__(self) -> int:
        return len(self._map)


async def send_with_retry(post, registry: AbandonRegistry, *,
                          retry_timeout: float = 3.0,
                          max_attempts: int = 3,
                          final_timeout: float = 65.0,
                          describe: str = "",
                          logger: logging.Logger = _LOGGER) -> str:
    """反复下发直到云端返回或次数用尽，返回最终成功的 msg_id。

    post(msg_id) 必须是一个协程函数，内部完成一次 HTTP 请求。

    前 max_attempts-1 次在 retry_timeout 后切断并补发；最后一次放宽到
    final_timeout（云端 60s 硬超时 + 余量），保证「最坏情况下仍拿得到结果」，
    同时避免请求无限挂住回调。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last_error = None
    for attempt in range(1, max_attempts + 1):
        msg_id = new_msgid()
        final = attempt >= max_attempts
        limit = final_timeout if final else retry_timeout
        try:
            await asyncio.wait_for(post(msg_id), timeout=limit)
            if attempt > 1:
                logger.info("%s 第 %d 次补发成功（msgId=%s）", describe, attempt, msg_id)
            return msg_id
        except asyncio.TimeoutError:
            last_error = "timeout"
            registry.abandon(msg_id)
            if final:
                logger.warning("%s 最终尝试 %.0fs 超时（msgId=%s）", describe, limit, msg_id)
                raise
            logger.info("%s %.1fs 未返回，补发（旧 msgId=%s 已废弃）",
                        describe, retry_timeout, msg_id)
        except Exception as err:  # noqa: BLE001
            last_error = err
            registry.abandon(msg_id)
            if final:
                raise
            logger.info("%s 失败（%s），补发第 %d 次", describe, err, attempt + 1)

    raise RuntimeError("unreachable: %s" % last_error)
