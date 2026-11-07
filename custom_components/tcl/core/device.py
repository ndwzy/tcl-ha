import json
import logging
import time
from typing import List

from .attribute import TclAttribute, V1SpecAttributeParser

_LOGGER = logging.getLogger(__name__)


class TclDevice:
    _raw_data: dict
    _attributes: List[TclAttribute]
    _attribute_snapshot_data: dict

    def __init__(self, client, raw: dict):
        self._client = client
        self._raw_data = raw
        self._attributes = []
        self._attribute_snapshot_data = {}
        # 乐观更新保护表(设备级共享): key -> (期望值, 到期时刻 monotonic)
        self._pending_optimistic = {}
        self._pending_deadline = 0.0

    @property
    def id(self):
        return self._raw_data['deviceId']

    @property
    def name(self):
        return self._raw_data['nickName'] if 'nickName' in self._raw_data else self.id

    @property
    def type(self):
        return self._raw_data['category'] if 'category' in self._raw_data else None

    @property
    def product_key(self):
        return self._raw_data['productKey'] if 'productKey' in self._raw_data else None

    @property
    def is_online(self):
        return self._raw_data['isOnline'] if 'isOnline' in self._raw_data else None

    @property
    def is_control(self):
        return self._raw_data['weChatControl']

    @property
    def attributes(self) -> List[TclAttribute]:
        return self._attributes

    @property
    def attribute_snapshot_data(self) -> dict:
        return self._attribute_snapshot_data

    @property
    def getClient(self):
        return self._client

    def update_attribute_snapshot_data(self, new_data: dict):
        # 可以在这里添加数据验证逻辑
        self._attribute_snapshot_data = new_data

    @property
    def has_pending_optimistic(self) -> bool:
        return bool(self._pending_optimistic)

    @property
    def optimistic_deadline(self) -> float:
        return self._pending_deadline

    def register_optimistic(self, patch: dict, window: float) -> None:
        """登记刚下发的命令，保护这些属性不被云端旧快照覆盖。"""
        deadline = time.monotonic() + window
        self._pending_deadline = deadline
        for key, value in (patch or {}).items():
            self._pending_optimistic[str(key)] = (value, deadline)

    def clear_optimistic(self) -> None:
        """命令失败或实体卸载时撤销全部保护。"""
        self._pending_optimistic.clear()
        self._pending_deadline = 0.0

    def _filter_pending(self, patch: dict) -> dict:
        now = time.monotonic()
        result = dict(patch or {})
        for key, (expected, deadline) in list(self._pending_optimistic.items()):
            if now >= deadline:
                # 超时: 不再保护，以云端真实状态为准
                del self._pending_optimistic[key]
                continue
            if key not in result:
                continue
            if result[key] == expected:
                # 设备已确认，撤销保护
                del self._pending_optimistic[key]
            else:
                # 云端影子还是旧值，本次不覆盖乐观状态
                result.pop(key)
                _LOGGER.debug(
                    'Device %s keep optimistic %s=%r, cloud reports %r',
                    self.id, key, expected, patch[key]
                )
        return result

    def update_attributes(self, patch: dict, protect: bool = True) -> dict:
        """
        把增量属性合并进快照(原地更新，所有实体共享同一份数据)。

        protect=True 用于来自云端/设备的更新：保护期内的属性若仍是旧值，
        先过滤掉再合并，避免 3 秒校准把乐观状态打回。乐观写入本身要传
        protect=False，否则会把刚登记的保护立刻当成"已确认"清掉。
        """
        if protect and self._pending_optimistic:
            patch = self._filter_pending(patch)
        data = self._attribute_snapshot_data
        for key, value in patch.items():
            data[str(key)] = value
        self._attribute_snapshot_data = data
        return data

    async def async_refresh(self) -> bool:
        """
        主动从云端拉取一次设备最新状态，并广播 device_data_changed 事件。

        MQTT 只在设备主动上报时才有消息，用遥控器/空调面板操作时经常
        不上报，必须靠这里补一次状态，否则 HA 会一直停留在旧状态。
        """
        # 局部导入避免模块循环引用
        from .event import EVENT_DEVICE_DATA_CHANGED, fire_event

        try:
            snapshot = await self._client.get_device_snapshot_data(self.id)
        except Exception as err:  # noqa: BLE001 云端接口异常不应影响其他设备
            _LOGGER.debug('Tcl device %s refresh snapshot failed: %s', self.id, err)
            return False

        if not snapshot:
            _LOGGER.debug('Tcl device %s refresh returned empty snapshot', self.id)
            return False

        self.update_attributes(snapshot)
        _LOGGER.debug(
            'Tcl device %s refreshed from cloud, %d attributes',
            self.id, len(snapshot)
        )
        fire_event(self._client.hass, EVENT_DEVICE_DATA_CHANGED, {
            'deviceId': self.id,
            'attributes': snapshot
        })
        return True

    async def async_init(self):
        # 解析Attribute
        # noinspection PyBroadException
        try:
            parser = V1SpecAttributeParser()
            attributes = await self._client.get_digital_model_from_cache(self)

            _LOGGER.info(
                'Device %s (productKey=%s) got %d raw attributes',
                self.id, self.product_key, len(attributes) if attributes else 0
            )

            for item in (attributes or []):
                try:
                    attr = parser.parse_attribute(item)
                    if attr:
                        self._attributes.append(attr)
                        _LOGGER.info(
                            'Device %s parsed: key=%s -> platform=%s',
                            self.id, attr.key, attr.platform
                        )
                    else:
                        _LOGGER.warning(
                            'Device %s attribute %s (type=%s) returned None',
                            self.id, item.get('identifier'), item.get('type')
                        )
                except Exception:
                    _LOGGER.exception(
                        "Tcl device %s attribute %s parsing error occurred",
                        self.id, item.get('name', item.get('identifier', 'unknown'))
                    )

            snapshot_data = await self._client.get_device_snapshot_data(self.id)
            _LOGGER.debug(
                'device %s snapshot data fetch successful. data: %s',
                self.id,
                json.dumps(snapshot_data)
            )
            self._attribute_snapshot_data = snapshot_data
        except Exception:
            _LOGGER.exception('Tcl device %s init failed', self.id)

    def __str__(self) -> str:
        return json.dumps({
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'product_key': self.product_key,
            'is_online': self.is_online,
            'is_control': self.is_control
        })
