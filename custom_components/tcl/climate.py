import asyncio
import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature, Platform
from homeassistant.core import HomeAssistant

from .entity import TclAbstractEntity
from .core.attribute import TclAttribute
from .core.device import TclDevice

_LOGGER = logging.getLogger(__name__)

MODE_MAP = {
    "auto": HVACMode.AUTO,
    "cool": HVACMode.COOL,
    "dry": HVACMode.DRY,
    "fan_only": HVACMode.FAN_ONLY,
    "heat": HVACMode.HEAT,
}

REVERSE_MODE_MAP = {v: k for k, v in MODE_MAP.items()}

# 新增风扇模式与风速百分比的映射
FAN_SPEED_MAP = {
    "低": 20,
    "中低": 25,
    "中": 50,
    "高": 75,
    "全速": 100,
}

# 反向映射，用于从设备返回的百分比查找对应的模式名称
REVERSE_FAN_SPEED_MAP = {v: k for k, v in FAN_SPEED_MAP.items()}


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities) -> None:
    """设置 TCL 空调实体"""
    domain = "tcl"
    devices = hass.data[domain]["devices"]

    entities = []
    for device in devices:
        # 检查设备是否包含空调相关的属性，以此判断是否为空调设备
        # 注意：这里将属性键改为驼峰命名法以匹配设备数据
        has_climate_attrs = any(attr.key in ("powerSwitch", "workMode", "targetTemperature", "windSpeedPercentage") for attr in device.attributes)
        if has_climate_attrs:
            # 为该空调设备创建一个“虚拟”的 TclAttribute，用于兼容 TclAbstractEntity 的构造函数
            climate_attr = TclAttribute(
                key="climate_control",  # 为气候实体定义一个通用 key
                display_name=f"{device.name} 空调", # 气候实体显示名称
                platform=Platform.CLIMATE # 指定平台为 Climate
            )
            entities.append(TclClimateEntity(device, climate_attr))

    async_add_entities(entities)


class TclClimateEntity(TclAbstractEntity, ClimateEntity):
    """TCL 空调实体"""

    # 初始化支持的特性，温度单位和 HVAC 模式
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        # 移除 ClimateEntityFeature.HVAC_MODE，因为您的 HomeAssistant 版本可能不支持此属性
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    # 更新风扇模式列表为我们定义的中文名称
    _attr_fan_modes = list(FAN_SPEED_MAP.keys())
    _attr_hvac_modes = [HVACMode.OFF] + list(MODE_MAP.values())


    def __init__(self, device: TclDevice, attribute: TclAttribute):
        """初始化空调实体。"""
        # 调用 TclAbstractEntity 的构造函数，它会处理 unique_id、name、device_info 以及事件监听
        super().__init__(device, attribute)

        # TclAbstractEntity 会从 dummy attribute 设置 _attr_name 和 _attr_unique_id，这里无需再次设置

    def _update_value(self) -> None:
        """从设备属性数据中更新实体状态。"""
        # 检查电源开关状态
        # 注意：这里将属性键改为驼峰命名法以匹配设备数据
        power = self._device.attribute_snapshot_data.get("powerSwitch")
        if power is None:
            # 如果 powerSwitch 不存在，可能设备离线或数据未完全加载
            self._attr_available = False
            return
        else:
            self._attr_available = True

        # 设置 HVAC 模式
        # 注意：这里将属性键改为驼峰命名法以匹配设备数据
        if power in ["off", False, 0]:
            self._attr_hvac_mode = HVACMode.OFF
        else:
            mode = self._device.attribute_snapshot_data.get("workMode")
            self._attr_hvac_mode = MODE_MAP.get(mode, HVACMode.AUTO)

        # 设置 HVAC 动作
        if self._attr_hvac_mode == HVACMode.HEAT:
            self._attr_hvac_action = HVACAction.HEATING
        elif self._attr_hvac_mode == HVACMode.COOL:
            self._attr_hvac_action = HVACAction.COOLING
        elif self._attr_hvac_mode == HVACMode.DRY:
            self._attr_hvac_action = HVACAction.DRYING
        elif self._attr_hvac_mode == HVACMode.FAN_ONLY:
            self._attr_hvac_action = HVACAction.FAN
        elif self._attr_hvac_mode == HVACMode.AUTO:
            self._attr_hvac_action = HVACAction.IDLE # 自动模式下设为空闲
        else:
            self._attr_hvac_action = HVACAction.IDLE

        # 设置目标温度
        # 注意：这里将属性键改为驼峰命名法以匹配设备数据
        self._attr_target_temperature = self._device.attribute_snapshot_data.get("targetTemperature") or 24 # 默认温度为24

        # 设置风扇模式
        # 从设备获取实际风速百分比
        wind_speed_percentage = self._device.attribute_snapshot_data.get("windSpeedPercentage")
        # 查找最接近的自定义模式名称
        if wind_speed_percentage is not None:
            # 找到最接近的预设风速
            closest_speed = min(FAN_SPEED_MAP.values(), key=lambda x: abs(x - wind_speed_percentage))
            self._attr_fan_mode = REVERSE_FAN_SPEED_MAP.get(closest_speed, "中") # 默认"中"
        else:
            self._attr_fan_mode = "中" # 如果数据缺失，默认"中"

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """设置 HVAC 模式。"""
        if hvac_mode == HVACMode.OFF:
            # 注意：这里将属性键改为驼峰命名法以匹配设备数据
            self._send_command({"powerSwitch": False})
        else:
            # 如果当前是关闭状态，先打开电源
            # 注意：这里将属性键改为驼峰命名法以匹配设备数据
            if self._device.attribute_snapshot_data.get("powerSwitch") in ["off", False, 0]:
                self._send_command({"powerSwitch": True})
                asyncio.sleep(0.5) # 稍微延迟，确保电源状态已更新
            # 注意：这里将属性键改为驼峰命名法以匹配设备数据
            self._send_command({"workMode": REVERSE_MODE_MAP.get(hvac_mode, "auto")})

    def set_fan_mode(self, fan_mode: str) -> None:
        """设置风扇模式。"""
        # 将自定义模式名称转换为对应的风速百分比
        target_speed = FAN_SPEED_MAP.get(fan_mode)
        if target_speed is not None:
            # 注意：这里将属性键改为驼峰命名法以匹配设备数据
            self._send_command({"windSpeedPercentage": target_speed})
        else:
            _LOGGER.warning(f"无法识别的风扇模式: {fan_mode}")

    def set_temperature(self, **kwargs: Any) -> None:
        """设置目标温度。"""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp:
            # 注意：这里将属性键改为驼峰命名法以匹配设备数据
            self._send_command({"targetTemperature": temp})