import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import async_register_entity
from .core.attribute import TclAttribute
from .core.device import TclDevice
from .entity import TclAbstractEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    await async_register_entity(
        hass,
        entry,
        async_add_entities,
        Platform.SENSOR,
        lambda device, attribute: TclSensor(device, attribute)
    )


class TclSensor(TclAbstractEntity, SensorEntity):

    def __init__(self, device: TclDevice, attribute: TclAttribute):
        super().__init__(device, attribute)
        self._attr_extra_state_attributes = {}
        self._formatted_value = None

    def _update_value(self):
        try:
            values = self._attributes_data.get(self._attribute.key, {})
            if not values:
                self._attr_native_value = "No data"
                self._attr_extra_state_attributes = {}
                return
                
            ext = self._attribute.ext
            ret_values = {}
            formatted_value = ""
            
            # 获取结构体整体信息
            struct_info = ext.get('struct_info', {})
            
            # 处理每个字段
            for key, value in values.items():
                if key not in ext:
                    ret_values[key] = value
                    continue
                    
                field_ext = ext.get(key, {})  # 使用get避免KeyError
                field_name = field_ext.get('name', key)
             
                # 处理枚举类型
                try:
                    if field_ext.get('mapping_type') == 'enum' and 'mapping' in field_ext:
                        mapping = field_ext.get('mapping', {})
                        str_value = str(value)
                        if str_value in mapping:
                            ret_values[f"{key}_text"] = mapping[str_value]
                            formatted_value += f"{field_name}: {mapping[str_value]}, "
                        else:
                            ret_values[f"{key}_text"] = str_value
                            formatted_value += f"{field_name}: {value}, "
                    
                    # 添加单位信息
                    if 'unit' in field_ext and (isinstance(value, (int, float)) or (isinstance(value, str) and value.replace('.', '', 1).isdigit())):
                        unit = field_ext.get('unit', '')
                        unit_name = field_ext.get('unit_name', '')
                        if isinstance(value, str) and value.replace('.', '', 1).isdigit():
                            try:
                                value = float(value)
                            except (ValueError, TypeError):
                                pass  # 保持原始值
                        ret_values[f"{key}_with_unit"] = f"{value} {unit}"
                        
                        # 添加单位名称说明
                        if unit_name:
                            ret_values[f"{key}_unit_name"] = unit_name
                        
                        formatted_value += f"{field_name}: {value} {unit}, "
                    else:
                        # 非枚举非数值类型，直接显示原始值
                        if not formatted_value or f"{field_name}: " not in formatted_value:
                            formatted_value += f"{field_name}: {value}, "
                except Exception as e:
                    # 处理字段时出错，记录错误并继续处理其他字段
                    ret_values[f"{key}_error"] = str(e)
                    formatted_value += f"{field_name}: [处理错误], "
            
            # 移除最后的逗号和空格
            if formatted_value.endswith(", "):
                formatted_value = formatted_value[:-2]
                
            # 设置传感器值和额外属性
            self._attr_native_value = formatted_value if formatted_value else str(values)
            self._formatted_value = formatted_value
            
            # 保存原始数据到额外属性
            self._attr_extra_state_attributes = {
                "raw_data": values,
                "original_values": ret_values,
                "struct_info": struct_info
            }
        except Exception as e:
            # 捕获整个方法中的任何异常
            _LOGGER.error(f"Error updating sensor value: {e}")
            self._attr_native_value = f"Error: {str(e)}"
            self._attr_extra_state_attributes = {
                "error": str(e),
                "raw_data": self._attributes_data
            }
