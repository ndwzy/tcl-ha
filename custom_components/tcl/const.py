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

# 云端快照轮询：正常情况下 MQTT 会推送状态变化，轮询只作为兜底。
# 基准间隔 300 秒，并叠加随机抖动（最晚约 500 秒），避免固定节奏访问云端。
# 每次收到 MQTT 消息都会重置计时，设备活跃时基本不会触发轮询。
CONF_SCAN_INTERVAL = 'scan_interval'
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 3600

# 轮询抖动系数：实际间隔 = 基准间隔 * [1.0, POLL_JITTER_MAX]
POLL_JITTER_MAX = 1.6
# 分段等待的粒度(秒)，越小对 MQTT 活动/停止信号响应越快
POLL_WAIT_CHUNK = 30

# 下发控制命令后延迟多久拉一次云端快照做校准(秒)
RECONCILE_DELAY = 3

# ---------------- 设备保活参数 ----------------
# 云控链路是"云端等设备确认"：设备醒着时单次请求 0.2 秒，而 WiFi 模块
# 进入深睡后要等 30~60 秒才被唤醒（实测：闲置 12 分钟后 60.3 秒）。
# 这里定期下发一条"同值"指令，把唤醒成本摊到后台，用户真正操作时就是秒回。
# 注意：快照接口(/device/status)读的是云端缓存，不会触达设备，起不到保活作用。
CONF_KEEPALIVE_INTERVAL = 'keepalive_interval'
DEFAULT_KEEPALIVE_INTERVAL = 120
MIN_KEEPALIVE_INTERVAL = 60
# 保活用的属性：必须能触达设备，且与当前值相同，不改变任何状态
KEEPALIVE_ATTRIBUTE = 'powerSwitch'
# 单次保活请求超时(秒)：超时说明设备已经睡死，本轮放弃，等下一轮
KEEPALIVE_TIMEOUT = 30
# 超过这个耗时说明保活没拦住休眠，打一条 warning 便于排查
KEEPALIVE_SLOW_THRESHOLD = 5

# ---------------- MQTT 连接参数 ----------------
MQTT_KEEPALIVE = 30
# 首次建连/重建连接的最小与最大退避(秒)
MQTT_RETRY_MIN = 5
MQTT_RETRY_MAX = 300
# 掉线后等待自动重连的宽限时间，超过则主动重建连接
MQTT_DISCONNECT_GRACE = 60
# 连接仍在但长时间收不到任何消息时的看门狗阈值(秒)
MQTT_SILENT_TIMEOUT = 1800

# ---------------- token 续期参数 ----------------
# 续期余量按"剩余有效期"的比例计算，并夹在 [1 小时, 24 小时] 之间：
# 30 天有效期的 token 会在到期前 24 小时续期，即使刷新连续失败也有充足的重试窗口。
# 余量放大不影响刷新次数（仍然是约 29 天一次），但失败时不再只剩 5 分钟可用。
TOKEN_REFRESH_MARGIN_RATIO = 0.05
TOKEN_REFRESH_MARGIN_MIN = 3600
TOKEN_REFRESH_MARGIN_MAX = 24 * 3600
# 刷新失败后的重试退避(秒)，避免 token 已经过期时秒级重试把接口打爆
TOKEN_RETRY_MIN = 60
TOKEN_RETRY_MAX = 1800

# 登录方式
LOGIN_METHOD_TOKEN = 'token'
LOGIN_METHOD_PASSWORD = 'password'
LOGIN_METHOD_SMS = 'sms'
