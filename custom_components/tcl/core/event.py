import asyncio
from typing import Callable, Coroutine, Any

from homeassistant.core import HomeAssistant, CALLBACK_TYPE, Event

from custom_components.tcl.const import DOMAIN

EVENT_DEVICE_CONTROL = 'device_control'
EVENT_DEVICE_DATA_CHANGED = 'device_data_changed'
EVENT_GATEWAY_STATUS_CHANGED = 'gateway_status_changed'


def wrap_event(name: str) -> str:
    return '{}_{}'.format(DOMAIN, name)


def _async_fire(hass: HomeAssistant, event_name: str, data: dict) -> None:
    """在事件循环线程中真正投递事件"""
    # HA 2025.x 起 EventBus.async_fire 是同步方法(返回 None)，
    # 早期误按协程处理会导致 "a coroutine was expected, got None"，
    # 这里两种形态都兼容。
    result = hass.bus.async_fire(event_name, data)
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        hass.async_create_task(result)


def fire_event(hass: HomeAssistant, event: str, data: dict) -> None:
    """
    触发事件。

    调用方可能来自 MQTT 的网络线程(paho)，也可能是事件循环线程，
    因此这里统一做线程安全处理；不再使用已废弃的 hass.bus.fire()。
    """
    event_name = wrap_event(event)
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    try:
        if running_loop is hass.loop:
            _async_fire(hass, event_name, data)
        else:
            hass.loop.call_soon_threadsafe(_async_fire, hass, event_name, data)
    except RuntimeError:
        # 事件循环已关闭(HA 正在停止)，忽略即可
        pass


def listen_event(
        hass: HomeAssistant,
        event: str,
        callback: Callable[[Event], Coroutine[Any, Any, None] | None]
) -> CALLBACK_TYPE:
    return hass.bus.async_listen(wrap_event(event), callback)
