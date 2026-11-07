import asyncio
import logging
import random
import threading
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    DOMAIN,
    SUPPORTED_PLATFORMS,
    FILTER_TYPE_EXCLUDE,
    FILTER_TYPE_INCLUDE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    POLL_JITTER_MAX,
    POLL_WAIT_CHUNK,
    CONF_KEEPALIVE_INTERVAL,
    DEFAULT_KEEPALIVE_INTERVAL,
    MIN_KEEPALIVE_INTERVAL,
    KEEPALIVE_ATTRIBUTE,
    KEEPALIVE_TIMEOUT,
    KEEPALIVE_SLOW_THRESHOLD,
    TOKEN_REFRESH_MARGIN_RATIO,
    TOKEN_REFRESH_MARGIN_MIN,
    TOKEN_REFRESH_MARGIN_MAX,
    TOKEN_RETRY_MIN,
    TOKEN_RETRY_MAX,
)
from .core.attribute import TclAttribute
from .core.client import TclClient, EVENT_DEVICE_DATA_CHANGED, TclClientException, TokenInfo
from .core.config import AccountConfig, DeviceFilterConfig, EntityFilterConfig
from .core.device import TclDevice
from typing import List

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    hass.data.setdefault(DOMAIN, {
        'devices': [],
        'signals': []
    })

    # 启动时先确认 token 可用，失败也不要让集成加载失败(网络还没起来时很常见)。
    # 必须带超时：这里在 setup 阶段被串行等待，任何一次网络卡住都会把整个
    # Home Assistant 启动流程一起拖死(表现为前端能打开、日志停在 loader 警告)。
    try:
        await asyncio.wait_for(try_update_token(hass, entry), 25)
    except asyncio.TimeoutError:
        _LOGGER.warning('启动时检查 token 超时(25s)，先用现有凭据继续')
    except Exception as err:  # noqa: BLE001 用现有凭据继续尝试，后面还会重试
        _LOGGER.warning('启动时检查 token 失败，继续使用现有凭据: %s', err)
    # 定时更新token
    token_signal = threading.Event()
    hass.async_create_background_task(token_updater(hass, entry, token_signal), 'tcl-token-updater')
    hass.data[DOMAIN]['signals'].append(token_signal)

    account_cfg = AccountConfig(hass, entry)
    client = TclClient(
        hass,
        account_cfg.account_id,
        account_cfg.token,
        refresh_token=account_cfg.refresh_token,
        entry=entry,
    )
    devices = await asyncio.wait_for(client.get_devices(), 25)
    _LOGGER.debug('共获取到{}个设备'.format(len(devices)))

    hass.data[DOMAIN]['devices'] = devices
    # 轮询以"最近一次收到 MQTT 消息"为基准，初始化成当前时间
    hass.data[DOMAIN]['last_mqtt_activity'] = time.monotonic()

    # 监听设备数据
    device_signal = threading.Event()
    hass.async_create_background_task(client.listen_devices(devices, device_signal), 'tcl-device-data')
    hass.data[DOMAIN]['signals'].append(device_signal)

    # 兜底轮询：正常状态变化由 MQTT 推送，只有长时间没有推送时才向云端拉快照
    device_updater_signal = threading.Event()
    hass.async_create_background_task(
        data_updater(hass, entry, devices, device_updater_signal),
        'tcl-device-data-updater'
    )
    hass.data[DOMAIN]['signals'].append(device_updater_signal)

    # 保活：定期向设备下发"同值"指令，把 WiFi 模块留在浅睡眠。
    # 快照轮询读的是云端缓存，不会触达设备，所以必须单独跑一个任务。
    keepalive_signal = threading.Event()
    hass.async_create_background_task(
        keepalive_updater(hass, entry, devices, keepalive_signal),
        'tcl-device-keepalive'
    )
    hass.data[DOMAIN]['signals'].append(keepalive_signal)

    # 修正历史版本生成的错误实体ID域名(tcl.xxx -> select.tcl_xxx 等)，
    # 必须在实体创建之前完成，否则会留下两条指向同一 unique_id 的记录
    _migrate_entity_ids(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, SUPPORTED_PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(entry_update_listener))

    return True


def _migrate_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    把历史版本生成的 "tcl.设备_属性" 实体ID迁移到正确域名。

    实体ID的域名必须与所属平台一致(select/number/switch/sensor)，
    HA 2027.5 起会拒绝 "tcl.xxx" 这种写法。迁移可以保留原有的
    unique_id 关联，避免实体重复创建。
    """
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover
        return

    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not item.entity_id.startswith(DOMAIN + '.'):
            continue

        # "tcl.41781497_workmode" -> "select.41781497_workmode"
        new_entity_id = '{}.{}'.format(item.platform, item.entity_id.split('.', 1)[1])
        try:
            registry.async_update_entity(item.entity_id, new_entity_id=new_entity_id)
            _LOGGER.info('已修正实体ID: %s -> %s', item.entity_id, new_entity_id)
        except Exception as err:  # noqa: BLE001 迁移失败不应阻断集成加载
            _LOGGER.warning(
                '实体ID迁移失败(将重新创建): %s -> %s, %s',
                item.entity_id, new_entity_id, err
            )
            try:
                registry.async_remove(item.entity_id)
            except Exception:  # noqa: BLE001
                pass


def _get_scan_interval(entry: ConfigEntry) -> int:
    """获取轮询间隔(秒)，0 表示关闭轮询"""
    raw = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        interval = DEFAULT_SCAN_INTERVAL

    if interval <= 0:
        return 0
    return max(interval, MIN_SCAN_INTERVAL)


async def _interruptible_sleep(signal: threading.Event, seconds: int) -> bool:
    """可被卸载信号打断的等待，返回 True 表示收到停止信号"""
    for _ in range(max(int(seconds), 1)):
        if signal.is_set():
            return True
        await asyncio.sleep(1)
    return signal.is_set()


async def data_updater(hass: HomeAssistant, entry: ConfigEntry, targetDevices: List[TclDevice], signal: threading.Event):
    """
    兜底轮询云端快照。

    正常的状态变化由 MQTT 推送，这里只在"长时间没有任何推送"时向云端拉一次全量快照：
    - 基准间隔可配置(默认 300 秒)，并叠加随机抖动，避免固定节奏访问云端；
    - 每收到一条 MQTT 消息都会把计时基准往后顺延，设备活跃时几乎不会真正发起请求。
    """
    interval = _get_scan_interval(entry)
    if interval <= 0:
        _LOGGER.info('device snapshot polling disabled')
        return

    state = hass.data.setdefault(DOMAIN, {})
    now = time.monotonic()
    state.setdefault('last_mqtt_activity', now)

    def _next_due(anchor: float) -> float:
        return anchor + interval * random.uniform(1.0, POLL_JITTER_MAX)

    last_seen_activity = state['last_mqtt_activity']
    next_due = _next_due(last_seen_activity)

    _LOGGER.info(
        'device snapshot polling enabled, base=%ss max=%ss, timer resets on MQTT activity',
        interval, int(interval * POLL_JITTER_MAX)
    )

    while not signal.is_set():
        activity = state.get('last_mqtt_activity', last_seen_activity)
        if activity > last_seen_activity:
            # MQTT 有消息，说明本地状态已经是最新的，重新开始计时
            last_seen_activity = activity
            next_due = _next_due(activity)

        now = time.monotonic()
        if now < next_due:
            # 分段等待，MQTT 一有消息或收到停止信号就能立刻反应
            if await _interruptible_sleep(signal, min(next_due - now, POLL_WAIT_CHUNK)):
                break
            continue

        _LOGGER.debug('snapshot polling round start, %d device(s)', len(targetDevices))

        for device in targetDevices:
            if signal.is_set():
                break
            try:
                await device.async_refresh()
            except Exception as err:  # noqa: BLE001 单个设备失败不影响其他设备
                _LOGGER.warning('Device %s polling failed: %s', device.id, err)

        last_seen_activity = state.get('last_mqtt_activity', last_seen_activity)
        next_due = _next_due(time.monotonic())


def _get_keepalive_interval(entry: ConfigEntry) -> int:
    """获取保活间隔(秒)，0 表示关闭保活"""
    raw = entry.options.get(CONF_KEEPALIVE_INTERVAL, DEFAULT_KEEPALIVE_INTERVAL)
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        interval = DEFAULT_KEEPALIVE_INTERVAL

    if interval <= 0:
        return 0
    return max(interval, MIN_KEEPALIVE_INTERVAL)


async def keepalive_updater(hass: HomeAssistant, entry: ConfigEntry,
                            targetDevices: List[TclDevice], signal: threading.Event):
    """
    周期性保活，把空调的 WiFi 模块留在浅睡眠。

    云控是"云端等设备确认"的模式，耗时随休眠深度变化：刚操作过 0.1~0.2 秒，
    闲置 12 分钟后 60.3 秒（云端大约每 30 秒尝试唤醒一次，睡越深需要越多轮）。
    这里每隔 interval 秒下发一条与当前值相同的电源指令：能触达设备、又不会
    改变任何状态，把唤醒成本挪到后台，用户真正操作时就是秒回。

    不用快照接口的原因是它读云端缓存(/device/status 恒定 0.2 秒)，根本不碰
    设备，对阻止休眠没有任何作用。
    """
    interval = _get_keepalive_interval(entry)
    if interval <= 0:
        _LOGGER.info('device keepalive disabled')
        return

    _LOGGER.info(
        'device keepalive enabled, every %ss for %d device(s)',
        interval, len(targetDevices)
    )

    while not signal.is_set():
        # 集成刚加载完先等一个间隔再发，避免启动瞬间就去打云端接口
        if await _interruptible_sleep(signal, interval):
            break

        for device in targetDevices:
            if signal.is_set():
                break

            snapshot = device.attribute_snapshot_data or {}
            if KEEPALIVE_ATTRIBUTE not in snapshot:
                # 该设备没有这个属性，跳过而不是瞎猜一个
                continue

            client = device.getClient
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    client.send_command(
                        client.getSession,
                        client.getToken,
                        device.id,
                        {KEEPALIVE_ATTRIBUTE: snapshot[KEEPALIVE_ATTRIBUTE]},
                    ),
                    KEEPALIVE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    'keepalive %s timeout after %ss, device may be in deep sleep',
                    device.id, KEEPALIVE_TIMEOUT
                )
                continue
            except Exception as err:  # noqa: BLE001 单个设备失败不影响其他设备
                _LOGGER.warning('keepalive %s failed: %s', device.id, err)
                continue

            elapsed = time.monotonic() - started
            if elapsed > KEEPALIVE_SLOW_THRESHOLD:
                _LOGGER.warning(
                    'keepalive %s took %.1fs, device was already asleep (interval too long?)',
                    device.id, elapsed
                )
            else:
                _LOGGER.debug('keepalive %s ok in %.2fs', device.id, elapsed)


def _refresh_margin(remaining: float) -> float:
    """
    续期余量：按剩余有效期的比例计算，并夹在 [1 小时, 24 小时]。

    30 天有效期的 token 会提前 24 小时续期。余量放大不会增加刷新频率
    (仍然是约 29 天一次)，但刷新失败时能多出几十次重试机会。
    """
    return min(
        max(remaining * TOKEN_REFRESH_MARGIN_RATIO, TOKEN_REFRESH_MARGIN_MIN),
        TOKEN_REFRESH_MARGIN_MAX,
    )


def _token_check_delay(hass: HomeAssistant, entry: ConfigEntry) -> float:
    """
    距离下次检查 token 还要等多久(秒)。

    expires_at 来自刷新接口返回的 JWT 载荷(exp)，能精确到秒，按需提前续期。
    登录流程拿到的是小程序渠道的老格式串，解析不出有效期，此时 expires_at=0，
    退化为每 30 分钟做一次轻量校验，等第一次刷新后就会自动补上真实有效期。

    已过期的 token 不再返回 0，否则 token_updater 会退化成每秒重试一次，
    连续失败时等于每秒打两次接口并把日志刷满。
    """
    try:
        expires_at = AccountConfig(hass, entry).expires_at or 0
    except Exception:  # noqa: BLE001 配置读取失败按未知有效期处理
        expires_at = 0

    if expires_at > 10 ** 11:  # 毫秒时间戳
        expires_at = expires_at / 1000

    if not expires_at:
        return 1800

    remaining = expires_at - time.time()
    if remaining <= 0:
        # 已经过期还停在循环里，说明之前的刷新都失败了，退到分钟级重试
        return TOKEN_RETRY_MIN

    return max(remaining - _refresh_margin(remaining), TOKEN_RETRY_MIN)


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化成便于阅读的时长，用于日志。"""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return '{}天{}小时'.format(days, hours)
    if hours:
        return '{}小时{}分'.format(hours, mins)
    if mins:
        return '{}分{}秒'.format(mins, secs)
    return '{}秒'.format(secs)


def _fmt_expires_at(ts) -> str:
    """格式化到期时间。

    用系统本地时间而不是 HA 的 time_zone：日志前缀走的就是系统时区，
    两者不一致时（容器 TZ=Asia/Shanghai，而 HA time_zone 为 UTC）
    日志里会出现"前缀 09:42、正文 01:42"这种对不上的情况。
    """
    if not ts:
        return '未知'
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(int(ts)))
    except Exception:  # noqa: BLE001 时间格式化失败不该影响续期结果
        return str(ts)


async def token_updater(hass: HomeAssistant, entry: ConfigEntry, signal: threading.Event):
    """
    定时检查 token：失效就刷新并重载集成。

    这里必须兜住所有异常——后台任务一旦抛出未捕获异常就会永久退出，
    之后 token 再也没人续期，只能等用户手动重载集成。
    刷新失败走 60s→1800s 的指数退避，成功一次即重置，避免接口被打爆。
    """
    delay = _token_check_delay(hass, entry)
    retry_delay = TOKEN_RETRY_MIN
    _LOGGER.info('token 续期已排期：%s 后检查', _fmt_duration(delay))

    while not signal.is_set():
        if await _interruptible_sleep(signal, delay):
            break

        try:
            refreshed = await try_update_token(hass, entry)
        except Exception as err:  # noqa: BLE001 任何异常都不能让任务退出
            _LOGGER.warning('检查 token 失败，%ss 后重试: %s', retry_delay, err)
            delay = retry_delay
            retry_delay = min(retry_delay * 2, TOKEN_RETRY_MAX)
            continue

        retry_delay = TOKEN_RETRY_MIN

        if not refreshed:
            delay = _token_check_delay(hass, entry)
            _LOGGER.info('token 仍有效，下次检查：%s 后', _fmt_duration(delay))
            continue

        _LOGGER.info('token refreshed, reload integration...')
        try:
            await hass.config_entries.async_reload(entry.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning('重载集成失败: %s', err)
        break


async def try_update_token(hass: HomeAssistant, entry: ConfigEntry):
    """
    尝试刷新token，刷新成功返回True
    """
    cfg = AccountConfig(hass, entry)
    client = TclClient(hass, cfg.account_id, cfg.token)

    try:
        await asyncio.wait_for(client.get_user_info(), 20)
        return False
    except TclClientException:
        pass  # token无效，尝试刷新
    except asyncio.TimeoutError:
        _LOGGER.debug('检查 token 超时，本次跳过刷新')
        return False

    if not cfg.refresh_token:
        raise TclClientException('token 已失效且没有 refresh_token，请重新登录')

    token_info = await client.refresh_token(cfg.refresh_token)
    cfg.token = token_info.token
    cfg.refresh_token = token_info.refresh_token
    if token_info.expires_at:
        # 刷新接口返回的 accessToken 是 JWT，载荷里有 exp，能算出真实有效期
        cfg.expires_at = token_info.expires_at
    cfg.save()

    # 续期成功必须留痕：启动阶段的续期原来完全静默，用户只能从"实体还在"
    # 猜出来，排查"token 到底有没有自动续"时完全没有依据。
    _LOGGER.info(
        'token 已失效，已用 refresh_token 续期，新有效期至 %s(剩余 %s)',
        _fmt_expires_at(cfg.expires_at),
        _fmt_duration((cfg.expires_at or 0) - time.time()),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    for platform in SUPPORTED_PLATFORMS:
        if not await hass.config_entries.async_forward_entry_unload(entry, platform):
            return False

    for signal in hass.data[DOMAIN]['signals']:
        signal.set()

    del hass.data[DOMAIN]

    return True


async def entry_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.debug('reload tcl integration...')
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, config: ConfigEntry, device: DeviceEntry) -> bool:
    device_id = list(device.identifiers)[0][1]

    _LOGGER.info('Device [{}] removing...'.format(device_id))

    for dev in hass.data[DOMAIN]['devices']:
        if dev.id.lower() == device_id:
            target_device = dev
            break
    else:
        _LOGGER.error('Device [{}] not found'.format(device_id))
        return False

    cfg = DeviceFilterConfig(hass, config)
    if cfg.filter_type == FILTER_TYPE_EXCLUDE:
        cfg.add_device(target_device.id)
    else:
        cfg.remove_device(target_device.id)

    cfg.save()

    _LOGGER.info('Device [{}] removed'.format(device_id))

    return True


async def async_register_entity(hass: HomeAssistant, entry: ConfigEntry, async_add_entities, platform, setup) -> None:
    entities = []
    for device in hass.data[DOMAIN]['devices']:
        if DeviceFilterConfig.is_skip(hass, entry, device.id):
            continue

        for attribute in device.attributes:
            if attribute.platform != platform:
                continue

            if EntityFilterConfig.is_skip(hass, entry, device.id, attribute.key):
                continue

            entities.append(setup(device, attribute))

    async_add_entities(entities)
