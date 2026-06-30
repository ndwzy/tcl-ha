import logging
from abc import abstractmethod, ABC

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import Platform

from ..helpers import ATTR_NAME

_LOGGER = logging.getLogger(__name__)


class TclAttribute:

    def __init__(self, key: str, display_name: str, platform: Platform, options: dict = None, ext: dict = None):
        self._key = key
        self._display_name = display_name
        self._platform = platform
        self._options = options if options is not None else {}
        self._ext = ext if ext is not None else {}

    @property
    def key(self) -> str:
        return self._key

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def platform(self) -> Platform:
        return self._platform

    @property
    def options(self) -> dict:
        return self._options

    @property
    def ext(self) -> dict:
        return self._ext


class TclAttributeParser(ABC):

    @abstractmethod
    def parse_attribute(self, attribute: dict) -> TclAttribute:
        pass

class V1SpecAttributeParser(TclAttributeParser, ABC):

    def parse_attribute(self, attribute: dict) -> TclAttribute:
        # 按钮处理
        if 'bool' in attribute['type']:
            return self._parse_as_switch(attribute)
        # 模式选择
        if 'enum' in attribute['type']:
            return self._parse_as_select(attribute)
        # 数值类型
        if 'int' in attribute['type'] or 'double' in attribute['type'] or 'float' in attribute['type']:
            return self._parse_as_number(attribute)
        # 结构体
        if 'struct' in attribute['type']:
            return self._parse_as_sensor(attribute)

        return None

    @staticmethod
    def _parse_as_sensor(attribute):
        options = {}
        ext = {}
        value_comparison_table = {}
        
        # 保存结构体的整体信息
        ext['struct_info'] = {
            'title': attribute['title'],
            'description': attribute.get('description', ''),
            'function': attribute.get('function', '')
        }
        
        for item in attribute['specs']:
            data_type = item['dataType']['type']
            data_id = item['identifier']
            data_opthons = {}
            data_ext = {
                'name': item['name']  # 保存字段的中文名称
            }
            data_value_comparison_table = {}
            
            # 处理枚举类型
            if 'enum' in data_type:
                for key, value in item['dataType']['specs'].items():
                    data_value_comparison_table[str(key)] = value
                data_opthons['device_class'] = SensorDeviceClass.ENUM
                data_opthons['options'] = list(data_value_comparison_table.values())
                data_ext['value_comparison_table'] = data_value_comparison_table
            
            # 处理数值类型
            if 'int' in data_type or 'double' in data_type or 'float' in data_type:
                specs = item['dataType']['specs']
                data_opthons['device_class'] = "number"
                data_opthons = {
                    'native_min_value': float(specs.get('min', 0)),
                    'native_max_value': float(specs.get('max', 100)),
                    'native_step': float(specs.get('step', 1))
                }
                
                # 添加单位信息
                if 'unit' in specs:
                    data_opthons['native_unit_of_measurement'] = specs['unit']
                    data_ext['unit'] = specs['unit']
                    if 'unitName' in specs:
                        data_ext['unit_name'] = specs['unitName']
            
            # 保存映射类型信息
            if 'mappingType' in item['dataType']:
                data_ext['mapping_type'] = item['dataType']['mappingType']
            
            options[str(data_id)] = data_opthons
            ext[str(data_id)] = data_ext
            value_comparison_table[str(data_id)] = data_value_comparison_table

        return TclAttribute(attribute['identifier'], ATTR_NAME.get(attribute['identifier'],attribute['title']), Platform.SENSOR, options, ext)

    @staticmethod
    def _parse_as_number(attribute):
        step = attribute['specs']
        options = {
            'native_min_value': float(step['min']),
            'native_max_value': float(step['max']),
            'native_unit_of_measurement': step['unit'],
            'native_step': step['step']
        }

        return TclAttribute(attribute['identifier'], ATTR_NAME.get(attribute['identifier'],attribute['title']), Platform.NUMBER, options)

    @staticmethod
    def _parse_as_select(attribute):
        value_comparison_table = {}
        optionslist = []
        for key, value in attribute['specs'].items():
            value_comparison_table[str(key)] = value
            optionslist.append(value)

        ext = {
            'value_comparison_table': attribute['specs']
        }

        options = {
            'options': optionslist
        }

        return TclAttribute(attribute['identifier'], ATTR_NAME.get(attribute['identifier'],attribute['title']), Platform.SELECT, options, ext)

    @staticmethod
    def _parse_as_switch(attribute):
        options = {
            'device_class': SwitchDeviceClass.SWITCH
        }

        return TclAttribute(attribute['identifier'], ATTR_NAME.get(attribute['identifier'],attribute['title']), Platform.SWITCH, options)
