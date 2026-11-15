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
# 校准重试间隔：设备还没回报时继续拉，直到确认或超过保护窗口
RECONCILE_RETRY = 10

# ---------------- 乐观状态保护 ----------------
# 云端每 30 秒重试一次唤醒，深睡时最坏 60 秒设备才真正执行。
# 在这段时间里 /v1/thing/status 读到的仍是旧值，若拿它去校准就会把
# 乐观状态打回原样，出现"变了→跳回→再变"的闪烁。这里给刚下发的属性
# 加一个保护窗口：窗口内不接受云端旧快照覆盖，直到设备报回期望值。
OPTIMISTIC_PENDING_WINDOW = 90

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
