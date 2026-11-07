import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
    SWING_ON,
    SWING_OFF,
    SWING_BOTH,
    SWING_VERTICAL,
    SWING_HORIZONTAL,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature, Platform
from homeassistant.core import HomeAssistant

from ..core.attribute import TclAttribute
from ..core.config import DeviceFilterConfig
from ..core.device import TclDevice
from ..entity import TclAbstractEntity

_LOGGER = logging.getLogger(__name__)

MODE_MAP = {
    "auto": HVACMode.AUTO,
    "cool": HVACMode.COOL,
    "dry": HVACMode.DRY,
    "fan_only": HVACMode.FAN_ONLY,
    "heat": HVACMode.HEAT,
}

REVERSE_MODE_MAP = {v: k for k, v in MODE_MAP.items()}
STR_TO_CODE = {
    "auto": 0,
    "cool": 1,
    "dry": 2,
    "fan_only": 3,
    "heat": 4
}
REVERSE_STR_TO_CODE = {v: k for k, v in STR_TO_CODE.items()}
# 新增风扇模式与风速百分比的映射
FAN_SPEED_MAP = {
    "自动": 0,
    "低": 20,
    "中低": 25,
    "中": 50,
    "高": 75,
    "全速": 100,
}

# 反向映射，用于从设备返回的百分比查找对应的模式名称
REVERSE_FAN_SPEED_MAP = {v: k for k, v in FAN_SPEED_MAP.items()}

# 设备/云端可能用字符串表示"关"的状态
POWER_OFF_VALUES = {"off", "0", "false", "close", "关机", ""}


def is_power_on(value: Any) -> bool:
    """把云端/MQTT 返回的开关值统一解析为 bool"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in POWER_OFF_VALUES
    return bool(value)


def to_number(value: Any):
    """把可能是字符串的数值转成 float，失败返回 None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities) -> None:
    """设置 TCL 空调实体"""
    domain = "tcl"
    devices = hass.data[domain]["devices"]

    entities = []
    for device in devices:
        # 遵循配置里的设备筛选，被排除的设备不应再创建空调实体
        if DeviceFilterConfig.is_skip(hass, config_entry, device.id):
            continue

        # 必须同时具备电源与模式属性才认为是空调设备
        keys = {attr.key for attr in device.attributes}
        if not {"powerSwitch", "workMode"}.issubset(keys):
            _LOGGER.debug('Device %s is not an air conditioner, skip climate entity', device.id)
            continue

        # 为该空调设备创建一个"虚拟"的 TclAttribute，用于兼容 TclAbstractEntity 的构造函数
        climate_attr = TclAttribute(
            key="climate_control",  # 为气候实体定义一个通用 key
            display_name=f"{device.name} 空调",  # 气候实体显示名称
            platform=Platform.CLIMATE  # 指定平台为 Climate
        )
        entities.append(TclClimateEntity(device, climate_attr))

    async_add_entities(entities)


class TclClimateEntity(TclAbstractEntity, ClimateEntity):
    """TCL 空调实体"""

    # 初始化支持的特性，温度单位和 HVAC 模式
    _attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            # 新增下面两行，开启 UI 的开关按钮
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        # 移除 ClimateEntityFeature.HVAC_MODE，因为您的 HomeAssistant 版本可能不支持此属性
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    # 更新风扇模式列表为我们定义的中文名称
    _attr_fan_modes = list(FAN_SPEED_MAP.keys())
    _attr_hvac_modes = [HVACMode.OFF] + list(MODE_MAP.values())

    def __init__(self, device: TclDevice, attribute: TclAttribute):
        """初始化空调实体。"""
        # 调用 TclAbstractEntity 的构造函数，它会处理 unique_id、name、device_info 以及事件监听
        super().__init__(device, attribute)

        # TclAbstractEntity 会从 dummy attribute 设置 _attr_name 和 _attr_unique_id，这里无需再次设置
        # 初始化记忆模式，默认给个自动，防止第一次无数据
        self._last_on_mode = HVACMode.AUTO

    @property
    def _snapshot(self) -> dict:
        return self._device.attribute_snapshot_data

    def _update_value(self) -> None:
        """从设备属性数据中更新实体状态。"""
        snapshot = self._snapshot

        # 检查电源开关状态
        power = snapshot.get("powerSwitch")
        if power is None:
            # 如果 powerSwitch 不存在，可能设备离线或数据未完全加载
            self._attr_available = False
            return

        self._attr_available = True

        # 设置 HVAC 模式
        if is_power_on(power):
            mode = snapshot.get("workMode")
            # 云端可能返回 int 1 或字符串 "1"，统一转成 int 再映射
            if isinstance(mode, str):
                try:
                    mode = int(mode.strip())
                except ValueError:
                    pass
            mode_key = REVERSE_STR_TO_CODE.get(mode, "auto")
            self._attr_hvac_mode = MODE_MAP.get(mode_key, HVACMode.AUTO)
            # 只要不是关机，就实时记录当前模式到记忆变量
            self._last_on_mode = self._attr_hvac_mode
        else:
            self._attr_hvac_mode = HVACMode.OFF

        # 设置 HVAC 动作
        hvac_action_map = {
            HVACMode.HEAT: HVACAction.HEATING,
            HVACMode.COOL: HVACAction.COOLING,
            HVACMode.DRY: HVACAction.DRYING,
            HVACMode.FAN_ONLY: HVACAction.FAN,
            HVACMode.AUTO: HVACAction.IDLE,
            HVACMode.OFF: HVACAction.OFF,
        }
        self._attr_hvac_action = hvac_action_map.get(self._attr_hvac_mode, HVACAction.IDLE)

        # 设置目标温度(注意用 None 判断，不能用 or，否则 0 会被覆盖成默认值)
        target_temperature = to_number(snapshot.get("targetTemperature"))
        self._attr_target_temperature = target_temperature if target_temperature is not None else 24

        # 更新当前温度
        self._attr_current_temperature = to_number(snapshot.get("currentTemperature"))

        # 设置风扇模式：从设备获取实际风速百分比，取最接近的预设档位
        wind_speed_percentage = to_number(snapshot.get("windSpeedPercentage"))
        if wind_speed_percentage is not None:
            closest_speed = min(FAN_SPEED_MAP.values(), key=lambda x: abs(x - wind_speed_percentage))
            self._attr_fan_mode = REVERSE_FAN_SPEED_MAP.get(closest_speed, "自动")  # 默认"自动"
        else:
            self._attr_fan_mode = "自动"  # 如果数据缺失，默认"自动"

        # 更新摆动模式
        vertical_wind = is_power_on(snapshot.get("verticalWind"))
        horizontal_wind = is_power_on(snapshot.get("horizontalWind"))
        if vertical_wind and horizontal_wind:
            self._attr_swing_mode = SWING_BOTH
        elif vertical_wind:
            self._attr_swing_mode = SWING_VERTICAL
        elif horizontal_wind:
            self._attr_swing_mode = SWING_HORIZONTAL
        else:
            self._attr_swing_mode = SWING_OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """设置 HVAC 模式。"""
        if hvac_mode == HVACMode.OFF:
            # 乐观更新，先让 UI 立刻反映关机，再下发命令
            self._attr_hvac_mode = HVACMode.OFF
            self._attr_hvac_action = HVACAction.OFF
            self._write_state()
            self._send_command({"powerSwitch": 0})
            return

        # 机身处于关闭状态时，把开机和模式放在同一条命令里下发。
        # 分两次发送 + sleep 的写法存在竞态：设备刚上电可能还没收到第二条命令。
        payload: dict = {}
        if not is_power_on(self._snapshot.get("powerSwitch")):
            payload["powerSwitch"] = 1

        mode_key = REVERSE_MODE_MAP.get(hvac_mode, "auto")
        payload["workMode"] = STR_TO_CODE.get(mode_key, 0)

        self._attr_hvac_mode = hvac_mode
        self._last_on_mode = hvac_mode
        self._write_state()
        self._send_command(payload)

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        # 获取记忆模式
        target_mode = self._last_on_mode

        # 安全防御：如果记忆模式是 OFF (极少见)，则强制转为自动
        if target_mode == HVACMode.OFF:
            target_mode = HVACMode.AUTO

        # 调用自身的设置模式方法 (注意要加 await 和 self)
        await self.async_set_hvac_mode(target_mode)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        # 关机前最后保存一次当前模式 (双重保险)
        if self._attr_hvac_mode not in (HVACMode.OFF, None):
            self._last_on_mode = self._attr_hvac_mode

        # 设置为 OFF
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """设置风扇模式。"""
        # 将自定义模式名称转换为对应的风速百分比
        target_speed = FAN_SPEED_MAP.get(fan_mode)
        if target_speed is None:
            _LOGGER.warning("无法识别的风扇模式: %s", fan_mode)
            return

        self._attr_fan_mode = fan_mode
        self._write_state()
        self._send_command({"windSpeedPercentage": target_speed})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """设置目标温度。"""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        self._attr_target_temperature = temp
        self._write_state()
        self._send_command({"targetTemperature": temp})

    @property
    def swing_modes(self):
        """摆动列表"""
        return [SWING_OFF, SWING_BOTH, SWING_VERTICAL, SWING_HORIZONTAL]

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """设置摆动"""
        if swing_mode == SWING_OFF:
            payload = {"verticalWind": 0, "horizontalWind": 0}
        elif swing_mode == SWING_BOTH:
            payload = {"verticalWind": 1, 'verticalDirection': 1, "horizontalWind": 1, 'horizontalDirection': 1}
        elif swing_mode == SWING_VERTICAL:
            payload = {"verticalWind": 1, 'verticalDirection': 1, "horizontalWind": 0}
        elif swing_mode == SWING_HORIZONTAL:
            payload = {"verticalWind": 0, "horizontalWind": 1, 'horizontalDirection': 1}
        else:
            _LOGGER.warning("无法识别的摆动模式: %s", swing_mode)
            return

        self._attr_swing_mode = swing_mode
        self._write_state()
        self._send_command(payload)
