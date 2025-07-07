from homeassistant.const import Platform

DOMAIN = 'tcl'

SUPPORTED_PLATFORMS = [
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.CLIMATE # 添加这一行
]

FILTER_TYPE_INCLUDE = 'include'
FILTER_TYPE_EXCLUDE = 'exclude'