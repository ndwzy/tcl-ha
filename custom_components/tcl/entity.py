import logging
from abc import ABC, abstractmethod

from homeassistant.core import Event, callback
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_call_later

from . import DOMAIN
from .const import RECONCILE_DELAY
from .core.attribute import TclAttribute
from .core.client import TclClient
from .core.device import TclDevice
from .core.event import EVENT_DEVICE_DATA_CHANGED, EVENT_GATEWAY_STATUS_CHANGED, EVENT_DEVICE_CONTROL
from .core.event import listen_event, fire_event

_LOGGER = logging.getLogger(__name__)


class TclAbstractEntity(Entity, ABC):
    _device: TclDevice
    _client: TclClient
    _attribute: TclAttribute

    def __init__(self, device: TclDevice, attribute: TclAttribute):
        self._attr_unique_id = '{}.{}_{}'.format(DOMAIN, device.id, attribute.key).lower()
        # entity_id 的域名必须是所属平台(select/number/switch/...)。
        # 早期版本直接用了 "tcl.xxx"，HA 2027.5 起会拒绝。
        platform = getattr(attribute.platform, 'value', attribute.platform)
        self.entity_id = '{}.{}_{}'.format(platform, device.id, attribute.key).lower()
        self._attr_should_poll = False

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id.lower())},
            name=device.name,
            manufacturer='TCL',
            model=device.product_key
        )

        self._attr_name = attribute.display_name
        for key, value in attribute.options.items():
            setattr(self, '_attr_' + key, value)

        self._device = device
        self._client = device.getClient
        self._attribute = attribute
        # 保存当前设备下所有attribute的数据
        self._attributes_data = {}
        # 取消监听回调
        self._listen_cancel = []
        # 命令下发后的校准定时器
        self._reconcile_cancel = None

    def _send_command(self, attributes):
        """
        发送控制命令
        :param attributes:
        :return:
        """
        # 先做本地乐观更新，不必等云端往返；真正的状态由控制回调/轮询校准
        self._device.update_attributes(attributes)
        fire_event(self.hass, EVENT_DEVICE_CONTROL, {
            'entityId': self.entity_id,
            'deviceId': self._device.id,
            'attributes': attributes
        })

    @abstractmethod
    def _update_value(self):
        pass

    @callback
    def _write_state(self) -> None:
        """安全地写入状态(实体可能还没加入 hass)"""
        if self.hass is None or not self.entity_id:
            return
        try:
            self.async_write_ha_state()
        except RuntimeError:
            # 实体已被移除
            pass

    @callback
    def _schedule_reconcile(self) -> None:
        """
        命令下发后延迟从云端重新拉取一次状态。

        MQTT 的上报可能丢失或延迟，本地乐观更新也可能和真实状态不一致，
        这里做一次校准，保证 UI 最终和设备一致。
        """
        if self._reconcile_cancel is not None:
            self._reconcile_cancel()

        async def _reconcile(_now) -> None:
            self._reconcile_cancel = None
            await self._device.async_refresh()

        self._reconcile_cancel = async_call_later(self.hass, RECONCILE_DELAY, _reconcile)

    async def async_update(self) -> None:
        """
        强制刷新时(例如调用 homeassistant.update_entity)从云端拉取最新状态。
        实体平时不轮询，轮询由集成统一的后台任务负责。
        """
        await self._device.async_refresh()
        self._update_value()

    async def async_added_to_hass(self) -> None:
        # 监听状态
        @callback
        def status_callback(event):
            self._attr_available = bool(event.data.get('status'))
            self._write_state()

        self._listen_cancel.append(listen_event(self.hass, EVENT_GATEWAY_STATUS_CHANGED, status_callback))

        # 监听数据变化事件
        @callback
        def data_callback(event):
            data = event.data or {}
            if data.get('deviceId') == self._device.id:
                # MQTT 上报的是增量，这里合并成完整快照，避免部分实体被"清零"
                self._device.update_attributes(data.get('attributes') or {})
                self._attributes_data = self._device.attribute_snapshot_data
            self._update_value()
            self._write_state()

        self._listen_cancel.append(listen_event(self.hass, EVENT_DEVICE_DATA_CHANGED, data_callback))
        # 填充快照值
        data_callback(Event('', data={
            'deviceId': self._device.id,
            'attributes': self._device.attribute_snapshot_data
        }))

        # 监听事件总线来的控制命令
        async def control_callback(e):
            # 每个实体都会注册该事件，目前根据entityId进行判断防治多次操作
            if self.entity_id != (e.data or {}).get('entityId'):
                return

            attributes = e.data.get('attributes') or {}

            # 先把乐观更新写进 HA，再发云端请求。
            # 云控是"云端等设备确认"的模式：设备醒着时 0.2 秒返回，模块进入
            # 深睡后要等 30~60 秒。如果先 await 再写状态，UI 会在这段时间里
            # 一动不动，看起来就像"点了没生效"。
            self._attributes_data = self._device.attribute_snapshot_data
            self._update_value()
            self._write_state()

            try:
                await self._client.send_command(
                    self._client.getSession,
                    self._client.getToken,
                    e.data['deviceId'],
                    attributes
                )
            except Exception as err:  # noqa: BLE001 单次控制失败不应影响事件监听
                _LOGGER.error(
                    'Entity [%s] send command %s failed: %s',
                    self.entity_id, attributes, err
                )
                # 命令没发出去，立即用云端真实状态纠正本地的乐观更新
                await self._device.async_refresh()
                return

            # 延迟向云端校准一次真实状态
            self._schedule_reconcile()

        self._listen_cancel.append(listen_event(self.hass, EVENT_DEVICE_CONTROL, control_callback))

    async def async_will_remove_from_hass(self) -> None:
        if self._reconcile_cancel is not None:
            self._reconcile_cancel()
            self._reconcile_cancel = None

        for cancel in self._listen_cancel:
            cancel()
        self._listen_cancel = []
