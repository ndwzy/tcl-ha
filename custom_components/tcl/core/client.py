import asyncio
import base64
import functools
import json
import logging
import random
import ssl
import threading
import time
import uuid
from typing import List, Optional

import aiohttp
import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util.ssl import client_context
from .device import TclDevice
from .event import EVENT_DEVICE_DATA_CHANGED, EVENT_GATEWAY_STATUS_CHANGED
from .event import fire_event
from ..ac.panel_config_fallback import get_fallback_attributes, merge_attributes
from ..const import (
    DOMAIN,
    KEEPALIVE_ACTIVITY_KEY,
    MQTT_KEEPALIVE,
    MQTT_RETRY_MIN,
    MQTT_RETRY_MAX,
    MQTT_DISCONNECT_GRACE,
    MQTT_SILENT_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


def _parse_msgid_time(msg_id) -> Optional[float]:
    """
    从 miniprogram_<随机>_<毫秒戳> 中解析出指令发出时刻(wall clock 秒)。

    解析不出来时返回 None，此时调用方应放弃顺延保活而不是猜一个时间。
    """
    if not isinstance(msg_id, str):
        return None
    tail = msg_id.rsplit('_', 1)[-1]
    if not tail.isdigit():
        return None
    value = int(tail)
    return value / 1000.0 if value > 1e11 else float(value)


APP_ID = '55141607047147220'
APP_TENANT_ID = 'tcl'
APP_PLATFORM_ID = '16'
APP_SECRET = '48980a392dc2078cbda5b3035a084a7bcee34a69cace18baa715e61d790b01d4'
APP_THIRD_PARTY = 'wxed3f11c6ee178737'
APP_VERSION = '2.7.33'
APP_UUID = 'TCL+'
APP_PLATFORM_TYPE = 'MemberMiniProgram'
APP_ENCRYPT_VERSION = '2.0'

# iOS App 凭据 (登录用)
IOS_APP_ID = '28411606743791229'
IOS_APP_SECRET = '8f2538acdb620574bf334cd35114f7178c8e790bf216edb7ba749bd6f6d86b44'
IOS_TENANT_ID = 'TCLPLUS'
IOS_APP_VERSION = '2.6.1(1344)'
IOS_PLATFORM_TYPE = 'iOS'
IOS_STORE_UUID = 'TCL+'
IOS_USER_AGENT = 'TCLPlus/2.6.1 (iPhone; iOS 15.4.1; Scale/3.00)'
IOS_REPORT_STATE = '{"os":"iOS","osVersion":"15.4.1","appVersion":"2.6.1","deviceModel":"iPhone"}'

REFRESH_TOKEN_API = 'https://cn.account.tcl.com/auth/auth/refershToken'
LOGIN_API = 'https://cn.account.tcl.com/auth/auth/login'
QUICK_LOGIN_API = 'https://cn.account.tcl.com/auth/auth/quickLogin'
SMS_CAPTCHA_API = 'https://cn.account.tcl.com/captcha/captcha/new/smsCaptcha'
GET_USER_INFO_API = 'https://cn.account.tcl.com/user/user/getUserInfoByToken'
GET_DEVICES_API = 'https://io.zx.tcljd.com/v1/tclplus/weChat/user/user_devices'
GET_MQTT_CONFIG_API = 'https://io.zx.tcljd.com/v1/auth/service/loadBalance'
CONTROL_DEVICE_API = 'https://io.zx.tcljd.com/v1/control/property/{deviceId}'
DEVICE_STATUS_API = 'https://io.zx.tcljd.com/v1/thing/status'
GET_DIGITAL_MODEL_API = 'https://io.zx.tcljd.com/v1/tclplus/panel/rn-panel-config'
ELECTRICITY_SUMMARY_API = 'https://io.zx.tcljd.com/v1/ac/statistics/electricity/summary'


def random_str(length: int = 32) -> str:
    return ''.join(random.choice('abcdef1234567890') for _ in range(length))


async def _sleep_or_stop(signal: threading.Event, seconds: float) -> bool:
    """可被停止信号打断的等待，返回 True 表示收到了停止信号"""
    deadline = time.monotonic() + max(float(seconds), 0)
    while True:
        if signal.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(remaining, 1.0))


def _mark_mqtt_activity(hass: HomeAssistant) -> None:
    """
    记录最近一次收到 MQTT 消息的时刻。

    轮询任务以此为基准顺延，设备在主动上报时就不必再去云端拉快照。
    """
    if hass is None:
        return
    try:
        hass.data.setdefault(DOMAIN, {})['last_mqtt_activity'] = time.monotonic()
    except Exception:  # noqa: BLE001 记录时间戳不允许影响消息处理
        pass



def _parse_jwt_exp(token: str) -> int:
    """
    从 JWT 载荷里取过期时间(秒级时间戳)，取不到返回 0。

    refreshToken 接口返回的 accessToken 是标准 JWT，载荷里的 exp 就是有效期
    (实测 30 天)；短信/密码登录拿到的是小程序渠道的老格式串，解析不出来，
    返回 0 表示"有效期未知"，由上层退化为探活。
    """
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get('exp')
        return int(exp) if exp else 0
    except Exception:  # noqa: BLE001 非 JWT 或字段缺失都按"有效期未知"处理
        return 0


def account_id_from_refresh_token(token: str) -> str:
    """
    从 refreshToken 里解析 accountId，省掉用户手工填写。

    refreshToken 是 RS256 JWT，载荷形如
        {"exp":..., "iat":..., "refreshToken": {"token": "<内层 JWT>"}}
    内层 JWT 的载荷里有 userInfo（一个 JSON 字符串），accountId 就在里面；
    实测也可以直接取载荷里的 sub / accountId。

    解析失败返回空串，调用方按"需要手工填写"处理。
    """
    def _payload(raw: str) -> dict:
        seg = raw.split('.')[1]
        seg += '=' * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))

    try:
        outer = _payload(token)
    except Exception:  # noqa: BLE001 非 JWT 直接放弃
        return ''

    # 1) 内层 token -> userInfo -> accountId
    inner_token = None
    nested = outer.get('refreshToken')
    if isinstance(nested, dict):
        inner_token = nested.get('token')
    elif isinstance(nested, str):
        inner_token = nested
    if inner_token:
        try:
            inner = _payload(inner_token)
            user_info = inner.get('userInfo')
            if isinstance(user_info, str):
                user_info = json.loads(user_info)
            if isinstance(user_info, dict) and user_info.get('accountId'):
                return str(user_info['accountId'])
            if inner.get('sub'):
                return str(inner['sub'])
        except Exception:  # noqa: BLE001 内层结构变化时继续往后兜
            pass

    # 2) 直接取外层字段
    for key in ('accountId', 'sub', 'userId'):
        if outer.get(key):
            return str(outer[key])
    return ''


class TokenInfo:

    def __init__(self, token: str, refresh_token: str, expires_at: int = 0):
        self._token = token
        self._refresh_token = refresh_token
        self._expires_at = expires_at

    @property
    def token(self) -> str:
        return self._token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def expires_at(self) -> int:
        """accessToken 过期时间(秒级时间戳)，0 表示未知"""
        return self._expires_at


class TclClientException(Exception):
    pass


class TclClient:

    def __init__(self, hass: HomeAssistant, accountId: str, token: str,
                 refresh_token: str = None, entry=None):
        self._account_id = accountId
        self._token = token
        self._refresh_token = refresh_token
        # accessToken 的过期时间(秒级时间戳)，0 表示未知(JWT 里取不到)
        self._expires_at = 0
        self._entry = entry
        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._user_id = None
        # 预创建 SSLContext 对象
        self.ssl_context = None  # 初始化为 None

    @property
    def getToken(self):
        return self._token

    @property
    def getSession(self):
        return self._session

    @property
    def hass(self):
        return self._hass

    async def initialize(self):
        """异步初始化 SSLContext"""
        self.ssl_context = await self._create_ssl_context()

    async def _create_ssl_context(self):
        context = await self.hass.async_add_executor_job(ssl.create_default_context)
        return context

    async def refresh_token(self, refresh_token: str) -> TokenInfo:
        """
        刷新token
        :return:
        """
        api_url = REFRESH_TOKEN_API + '?appId=' + APP_ID + '&accountId=' + self._account_id + '&tenantId=' + APP_TENANT_ID + '&appSecret=' + APP_SECRET

        api_headers = {
            "Host": "cn.account.tcl.com",
            "Content-Type": "application/json;charset=UTF-8",
            "t-platform-type": "iOS",
            "TCL-Authorization": self._token,
            "token": self._token,
            "EncryptVersion": APP_ENCRYPT_VERSION,
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "User-Agent": "TCLPlus/2.6.1 (iPhone; iOS 15.4.1; Scale/3.00)",
            "Encrypt": "false",
            "refreshToken": refresh_token
        }

        async with self._session.get(
            url=api_url,
            headers=api_headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            token_info = await response.json()
            if 'accessToken' in token_info:
                return TokenInfo(
                    token_info['accessToken'],
                    token_info['refreshToken'],
                    _parse_jwt_exp(token_info['accessToken'])
                )
            else:
                raise TclClientException('接口返回异常: ' + str(token_info))

    async def ensure_valid_token(self) -> bool:
        """
        确认当前 token 仍然可用，失效则用 refresh_token 换新。

        在重建 MQTT 连接之前调用：否则凭据过期时会拿着废 token 无限重试。
        刷新成功后会写回配置项（会触发集成重载），返回 True 表示刷新过。
        """
        try:
            await asyncio.wait_for(self.get_user_info(), 20)
            return False
        except TclClientException:
            pass  # 明确的鉴权失败，需要刷新
        except asyncio.TimeoutError:
            _LOGGER.debug('检查 token 超时，跳过刷新')
            return False
        except Exception as err:  # noqa: BLE001 网络抖动不应触发换 token
            _LOGGER.debug('检查 token 时网络异常，跳过刷新: %s', err)
            return False

        if not self._refresh_token:
            _LOGGER.warning('token 已失效且本地没有 refresh_token，请在集成设置中重新登录')
            return False

        try:
            token_info = await asyncio.wait_for(self.refresh_token(self._refresh_token), 20)
        except Exception as err:  # noqa: BLE001 刷新失败交给外层退避重试
            _LOGGER.warning('刷新 token 失败: %s', err)
            return False

        self._token = token_info.token
        self._refresh_token = token_info.refresh_token
        self._expires_at = token_info.expires_at
        self._persist_token()
        if self._expires_at:
            expire_text = time.strftime('%Y-%m-%d %H:%M', time.localtime(self._expires_at))
        else:
            expire_text = '未知(接口未返回有效期)'
        _LOGGER.info('token 已刷新，有效期至 %s，将使用新凭据重建 MQTT 连接', expire_text)
        return True

    def _persist_token(self) -> None:
        """把刷新后的 token 写回配置项，避免重启后仍使用旧的 refresh_token"""
        if self._entry is None or self._hass is None:
            return
        try:
            from .config import AccountConfig

            cfg = AccountConfig(self._hass, self._entry)
            cfg.token = self._token
            cfg.refresh_token = self._refresh_token
            if self._expires_at:
                cfg.expires_at = self._expires_at
            cfg.save()
        except Exception as err:  # noqa: BLE001 持久化失败不影响内存中的新 token
            _LOGGER.warning('保存刷新后的 token 失败: %s', err)

    async def get_user_info(self) -> dict:
        """
        根据token获取用户信息
        :return:
        """
        api_url = GET_USER_INFO_API + '?appId=' + APP_ID + '&tenantId=' + APP_TENANT_ID + '&appSecret=' + APP_SECRET

        api_headers = {
            "Host": "cn.account.tcl.com",
            "TCL-Authorization": self._token,
            # 刷新接口换来的是标准 JWT，实测要同时带 token 头服务端才认；
            # 老的小程序渠道 token 两个头都带也正常，统一都给上
            "token": self._token,
            "xweb_xhr": "1",
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "t-platform-type": APP_PLATFORM_TYPE,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13)XWEB/13639",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Referer": "https://servicewechat.com/wxed3f11c6ee178737/243/page-frame.html"
        }
        # 显式超时：这个调用在集成 setup 阶段被串行等待，卡住会拖死整个启动流程。
        # 超时抛 asyncio.TimeoutError，交给上层按"网络问题"处理，不要当成鉴权失败
        async with self._session.get(
            url=api_url,
            headers=api_headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            content = await response.json(content_type=None)
            if 'FAILED' in str(content.get('status') or ''):
                raise TclClientException('Error getting user info, error: {}'.format(content.get('failCause')))
            userinfo = content.get('data') or {}
            return {
                'accountId': userinfo.get('accountId'),
                'mobile': userinfo.get('phone'),
                'username': userinfo.get('username')
            }

    def _get_io_headers(self, content_type: str = "application/x-www-form-urlencoded;charset=utf-8") -> dict:
        """获取 io.zx.tcljd.com 通用的请求头"""
        return {
            "Host": "io.zx.tcljd.com",
            "accessToken": self._token,
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13)XWEB/13639",
            "Content-Type": content_type,
            "xweb_xhr": "1",
            "miniGramSetup": "1",
            "t-platform-type": APP_PLATFORM_TYPE,
            "Referer": "https://servicewechat.com/wxed3f11c6ee178737/243/page-frame.html"
        }

    async def get_mqtt_config(self) -> List[TclDevice]:
        """
        获取MQTT配置
        """
        api_headers = self._get_io_headers()
        async with self._session.get(url=GET_MQTT_CONFIG_API, headers=api_headers) as response:
            content = await response.json(content_type=None)
            self._assert_response_successful(content)
            return content['data']

    async def get_devices(self) -> List[TclDevice]:
        """
        获取设备列表
        """
        api_headers = self._get_io_headers()
        async with self._session.get(
            url=GET_DEVICES_API,
            headers=api_headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            content = await response.json(content_type=None)
            self._assert_response_successful(content)

            devices = []
            for raw in content['data']:
                _LOGGER.debug('Device Info: {}'.format(raw))
                device = TclDevice(self, raw)
                # 暂时只支持微信小程序可控制设备
                if device.is_control == "1":
                    await device.async_init()
                    devices.append(device)
                # 后续若需支持其他设备需要适配APP下载的设备的配置包.zip文件，若有需要可自行扩充
            return devices

    async def get_digital_model(self, productKey: str) -> list:
        """
        获取设备attributes
        :param deviceId:
        :return:
        """
        api_url = GET_DIGITAL_MODEL_API + '?productKey=' + productKey

        api_headers = self._get_io_headers()
        async with self._session.get(url=api_url, headers=api_headers) as response:
            content = await response.json(content_type=None)
            self._assert_response_successful(content)
            if 'pages' not in content['data'] or 'home' not in content['data']['pages']:
                _LOGGER.warning("Device {} get digital model fail. response: {}".format(
                    productKey,
                    json.dumps(content, ensure_ascii=False)
                ))
                return []

            return content['data']['pages']['home']

    async def get_digital_model_from_cache(self, device: TclDevice) -> list:
        """
        获取设备 attributes。
        有本地 fallback 时：先请求 API（主），再用 fallback 补充缺失属性（合并去重）。
        无本地 fallback 时：走 HA Storage 缓存 → API 远程获取 逻辑。
        :param device:
        :return:
        """
        local_attrs = get_fallback_attributes(device.product_key)

        # 有本地 fallback：API 为主 + fallback 合并，不使用缓存
        if local_attrs:
            try:
                api_attrs = await self.get_digital_model(device.product_key)
            except Exception as e:
                _LOGGER.warning(
                    "Device %s API request failed (%s), using local fallback only (%d attributes)",
                    device.id, e, len(local_attrs)
                )
                api_attrs = []

            merged = merge_attributes(api_attrs, device.product_key)
            _LOGGER.info(
                "Device %s (productKey=%s) merged: API=%d, fallback=%d, merged=%d",
                device.id, device.product_key,
                len(api_attrs) if api_attrs else 0,
                len(local_attrs),
                len(merged)
            )
            return merged

        # 无本地 fallback：走缓存 + API 逻辑
        store = Store(self._hass, 1, 'tcl/device_{}.json'.format(device.id))
        cache = None
        try:
            cache = await store.async_load()
            if isinstance(cache, str):
                raise RuntimeError('cache is invalid')
        except Exception:
            _LOGGER.warning("Device {} cache is invalid".format(device.id))
            await store.async_remove()
            cache = None

        if cache:
            _LOGGER.info("Device {} get digital model from cache successful".format(device.id))
            return cache['attributes']

        _LOGGER.info("Device {} get digital model from cache fail, attempt to obtain remotely".format(device.id))
        attributes = await self.get_digital_model(device.product_key)
        await store.async_save({
            'device': {
                'name': device.name,
                'type': device.type,
                'product_key': device.product_key,
                'is_online': device.is_online,
                'is_control': device.is_control
            },
            'attributes': attributes
        })

        return attributes

    async def get_device_snapshot_data(self, deviceId: str) -> dict:
        """
        获取指定设备最新的属性数据
        :param deviceId:
        :return:
        """
        api_headers = self._get_io_headers("application/json;charset=UTF-8")

        api_body = {"deviceId": deviceId}
        # 显式设置超时：轮询任务串行拉取，一次请求卡住会把整个轮询循环拖死，
        # 表现为实体状态长时间不再更新
        try:
            async with self._session.post(
                url=DEVICE_STATUS_API,
                headers=api_headers,
                json=api_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                content = await response.json(content_type=None)
        except asyncio.TimeoutError:
            _LOGGER.warning("Device %s snapshot request timeout (15s)", deviceId)
            return {}
        self._assert_response_successful(content)
        if 'status' not in content['data']:
            _LOGGER.warning("Device {} get digital snapshot data fail. response: {}".format(
                deviceId,
                json.dumps(content, ensure_ascii=False)
            ))
            return {}
        return content['data']['status']

    async def listen_devices(self, targetDevices: List[TclDevice], signal: threading.Event):
        """
        监听设备数据变化（MQTT over WebSocket）。

        外层循环负责“取配置 -> 建连 -> 监控 -> 必要时重建”，任何一步失败都只是
        退避重试，不会让监听任务结束；只有集成卸载时的停止信号才能真正退出。
        """
        process_id = str(uuid.uuid4())
        self._hass.data['current_listen_devices_process_id'] = process_id

        device_list = [
            {'deviceId': device.id, 'productKey': device.product_key}
            for device in targetDevices
        ]
        # 设备真实存在的属性 key，用于过滤通知消息里的无关字段
        known_keys = {
            device.id: {attribute.key for attribute in device.attributes}
            for device in targetDevices
        }

        retry_delay = MQTT_RETRY_MIN

        while not signal.is_set():
            try:
                mqtt_config = await asyncio.wait_for(self.get_mqtt_config(), 30)
            except Exception as err:  # noqa: BLE001 拿不到配置也要重试，不能就此罢休
                _LOGGER.warning('获取 MQTT 配置失败，%ss 后重试: %s', retry_delay, err)
                self._notify_gateway_down(retry_delay)
                if await _sleep_or_stop(signal, retry_delay):
                    break
                retry_delay = min(retry_delay * 2, MQTT_RETRY_MAX)
                continue

            ever_connected, reason = await self._mqtt_session(
                mqtt_config, device_list, known_keys, targetDevices, signal, process_id
            )
            if reason == 'signal' or signal.is_set():
                break

            # 需要重建：先确认凭据没过期，否则重建后依旧会被服务端拒绝
            await self.ensure_valid_token()

            if ever_connected:
                # 连上过说明网络和服务端都是通的，重建时从最小退避开始
                retry_delay = MQTT_RETRY_MIN
            _LOGGER.warning('MQTT 连接需要重建，%ss 后重试', retry_delay)
            self._notify_gateway_down(retry_delay)
            if await _sleep_or_stop(signal, retry_delay):
                break
            retry_delay = min(retry_delay * 2, MQTT_RETRY_MAX)

        if process_id == self._hass.data.get('current_listen_devices_process_id'):
            fire_event(self._hass, EVENT_GATEWAY_STATUS_CHANGED, {'status': False})
        _LOGGER.info('listen device stopped.')

    def _notify_gateway_down(self, retry_delay: int) -> None:
        """
        短暂的重建不通知 UI，避免实体在可用/不可用之间闪烁；
        退避时间已经拉长，说明短时间内恢复不了，才标记为不可用。
        """
        if retry_delay < MQTT_DISCONNECT_GRACE:
            return
        fire_event(self._hass, EVENT_GATEWAY_STATUS_CHANGED, {'status': False})

    async def _mqtt_session(self, mqtt_config, device_list, known_keys,
                            targetDevices, signal, process_id):
        """
        建立并维持一条 MQTT 连接。返回 (是否连接成功过, 结束原因)。

        结束原因为 'signal'（收到停止信号）或 'reset'（连接需要整体重建：
        长时间连不上，或者连上了但长时间收不到任何消息）。
        """
        host = 'iotws-prod.tcliot.com'
        port = 443
        user_id = mqtt_config['userId']
        self._user_id = user_id

        if self.ssl_context is None:
            await self.initialize()

        stats = {
            'connected': False,
            'ever_connected': False,
            'last_inbound': time.monotonic(),
            'last_disconnect': time.monotonic(),
            'suppress_status': False,
        }

        def on_connect(client, userdata, flags, rc):
            if rc != 0:
                # 4/5 通常代表用户名密码/token 无效，重建前会尝试刷新 token
                _LOGGER.warning('MQTT 连接被服务器拒绝，返回码 %s', rc)
                return

            _LOGGER.info('成功连接到MQTT服务器')
            stats['connected'] = True
            stats['ever_connected'] = True
            stats['last_disconnect'] = None
            stats['last_inbound'] = time.monotonic()

            for device in device_list:
                device_id = device['deviceId']
                product_key = device['productKey']
                subscriptions = (
                    # 属性上报：设备/面板/遥控器改变状态后推送
                    (f"/sys/{product_key}/{device_id}/thing/event/property/post", '属性上报'),
                    # 设备级事件（含上下线等）。单层通配不会与上面重复订阅
                    (f"/sys/{product_key}/{device_id}/thing/event/+", '设备事件'),
                    # 控制回执：确认下发的命令是否被设备接受
                    (f"/sys/{product_key}/{device_id}/thing/service/property/set_reply", '控制回执'),
                )
                for topic, label in subscriptions:
                    client.subscribe(topic, qos=1)
                    _LOGGER.debug('已订阅%s主题: %s', label, topic)

            # 账号级通知（上下线/告警）。小程序订阅的是 5 层主题 /sys/{userId}/...
            notice_topic = f"/sys/{user_id}/thing/event/pushnotice"
            client.subscribe(notice_topic, qos=1)
            _LOGGER.debug('已订阅账号通知主题: %s', notice_topic)

            fire_event(self._hass, EVENT_GATEWAY_STATUS_CHANGED, {'status': True})

            # 断线期间可能漏掉状态变化，连上后主动拉一次快照补齐
            for device in targetDevices:
                try:
                    self._hass.loop.call_soon_threadsafe(
                        self._hass.async_create_task, device.async_refresh()
                    )
                except Exception:  # noqa: BLE001 回调线程不能抛异常，否则会打断网络循环
                    _LOGGER.exception('schedule refresh for device %s failed', device.id)

        def on_message(client, userdata, msg):
            stats['last_inbound'] = time.monotonic()
            _mark_mqtt_activity(self._hass)

            try:
                payload_raw = msg.payload.decode()
            except Exception:  # noqa: BLE001
                payload_raw = repr(msg.payload)
            _LOGGER.debug('收到消息 主题:%s 内容:%s', msg.topic, payload_raw)

            try:
                payload = json.loads(payload_raw)
            except ValueError:
                _LOGGER.warning('MQTT 消息不是合法 JSON，主题:%s 内容:%s', msg.topic, payload_raw[:300])
                return
            if not isinstance(payload, dict):
                return

            try:
                if '/thing/event/property/post' in msg.topic:
                    self._handle_property_post(msg.topic, payload)
                elif '/thing/service/property/set_reply' in msg.topic:
                    self._handle_set_reply(msg.topic, payload)
                elif '/thing/event/pushnotice' in msg.topic:
                    self._handle_push_notice(msg.topic, payload, known_keys)
                else:
                    _LOGGER.debug('未处理的主题: %s', msg.topic)
            except Exception as err:  # noqa: BLE001 单条消息处理失败不能中断网络循环
                _LOGGER.exception('处理消息时出错，主题:%s 错误:%s', msg.topic, err)

        def on_disconnect(client, userdata, rc):
            stats['connected'] = False
            stats['last_disconnect'] = time.monotonic()

            if rc == 0:
                _LOGGER.info('MQTT 连接已关闭')
                return

            _LOGGER.warning('MQTT 意外断开（rc=%s），等待自动重连', rc)
            if (not stats['suppress_status']
                    and process_id == self._hass.data.get('current_listen_devices_process_id')):
                fire_event(self._hass, EVENT_GATEWAY_STATUS_CHANGED, {'status': False})

        client = None
        try:
            # ---- 首次建连：失败不放弃，指数退避重试 ----
            delay = MQTT_RETRY_MIN
            while True:
                if signal.is_set():
                    return stats['ever_connected'], 'signal'

                # 每次重试都用全新的客户端，避免失败后残留半初始化的状态
                client = self._build_mqtt_client(user_id)
                client.on_connect = on_connect
                client.on_message = on_message
                client.on_disconnect = on_disconnect

                try:
                    await self._hass.async_add_executor_job(
                        functools.partial(client.connect, host, port, MQTT_KEEPALIVE)
                    )
                    break
                except Exception as err:  # noqa: BLE001 网络/DNS/证书问题一律重试
                    _LOGGER.warning('MQTT 连接失败，%ss 后重试: %s', delay, err)
                    self._close_client(client)
                    client = None
                    if await _sleep_or_stop(signal, delay):
                        return stats['ever_connected'], 'signal'
                    delay = min(delay * 2, 60)

            client.loop_start()
            _LOGGER.debug('MQTT 网络循环已启动，等待连接结果')

            # ---- 看门狗：连接是否真的还活着 ----
            while not signal.is_set():
                # 用可打断的等待，卸载时能立刻退出，不必干等一轮
                if await _sleep_or_stop(signal, 5):
                    return stats['ever_connected'], 'signal'
                now = time.monotonic()

                if stats['connected']:
                    silent = now - stats['last_inbound']
                    if silent > MQTT_SILENT_TIMEOUT:
                        # keepalive 没报警但确实收不到东西，重建一次最稳妥
                        _LOGGER.warning(
                            'MQTT 保持连接但 %d 分钟没有收到任何消息，主动重建连接',
                            int(silent // 60)
                        )
                        return stats['ever_connected'], 'reset'
                    continue

                since = stats['last_disconnect'] or now
                if now - since > MQTT_DISCONNECT_GRACE:
                    _LOGGER.warning('MQTT 断开超过 %ss 仍未恢复，重建连接', MQTT_DISCONNECT_GRACE)
                    return stats['ever_connected'], 'reset'

            return stats['ever_connected'], 'signal'
        finally:
            # 主动重建时不希望实体在可用/不可用之间闪烁
            stats['suppress_status'] = True
            self._close_client(client)

    def _build_mqtt_client(self, user_id: str):
        """创建一个配置好传输层与凭据的 MQTT 客户端（尚未连接）"""
        # clientId 每轮重新生成，避免与上一轮的会话互相抢占
        suffix = str(random.randint(1000, 9999))
        timestamp = str(int(time.time() * 1000))
        client_id = f"{user_id}@miniprogram@{suffix}{timestamp}"

        # paho-mqtt 2.x 要求显式传入回调 API 版本，1.x 没有这个参数，这里做兼容
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=client_id,
                clean_session=True,
                protocol=mqtt.MQTTv311,
                transport="websockets"
            )
        except (AttributeError, TypeError):
            client = mqtt.Client(
                client_id=client_id,
                clean_session=True,
                protocol=mqtt.MQTTv311,
                transport="websockets"
            )

        client.username_pw_set(username=user_id, password=self._token)
        client.tls_set_context(self.ssl_context)
        client.ws_set_options(path="/mqtt")
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    @staticmethod
    def _close_client(client) -> None:
        """安静地释放一个 MQTT 客户端，任何异常都不向外抛"""
        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _handle_property_post(self, topic: str, payload: dict) -> None:
        """处理设备属性上报"""
        parts = topic.split('/')
        if len(parts) < 4:
            return
        device_id = parts[3]

        params = payload.get('params')
        if not isinstance(params, dict):
            return

        attributes = {}
        for key, value in params.items():
            # 正常情况下是 {"value": x, "time": ...}，也兼容直接给标量
            if isinstance(value, dict) and 'value' in value:
                attributes[key] = value['value']
            else:
                attributes[key] = value

        if attributes:
            fire_event(self._hass, EVENT_DEVICE_DATA_CHANGED, {
                'deviceId': device_id,
                'attributes': attributes
            })

    def _handle_set_reply(self, topic: str, payload: dict) -> None:
        """处理控制命令回执，确认下发的命令是否被设备接受"""
        parts = topic.split('/')
        device_id = parts[3] if len(parts) >= 4 else None

        code = payload.get('code', payload.get('status'))
        ok = str(code).strip().lower() in ('0', '200', 'success', 'ok')

        if ok:
            _LOGGER.debug('设备 %s 控制回执成功: %s', device_id, payload)
        else:
            _LOGGER.warning('设备 %s 控制回执失败: %s', device_id, payload)

        try:
            store = self._hass.data.setdefault(DOMAIN, {})
            store['last_set_reply'] = {
                'deviceId': device_id,
                'code': code,
                'ok': ok,
                'payload': payload,
                'time': time.time(),
            }
            if ok and device_id:
                # 一条成功回执 = 设备刚被真实触达过(HA、小程序或别的客户端发的都算)。
                # 锚点取 msgId 里的"指令发出时刻"而非"回执到达时刻"：
                # 1) 保活自己那条指令的时刻不晚于本轮发送时刻，不会把下一轮
                #    顺延零点几秒，间隔仍严格等于 interval；
                # 2) 设备深睡时回执可能晚到几十秒，也不会把间隔拖过 interval。
                # 解析不出 msgId 就不更新锚点，保活退回固定 interval，安全。
                sent_wall = _parse_msgid_time(payload.get('msgId'))
                if sent_wall is not None:
                    sent_mono = time.monotonic() - max(time.time() - sent_wall, 0.0)
                    store.setdefault(KEEPALIVE_ACTIVITY_KEY, {})[device_id] = sent_mono
        except Exception:  # noqa: BLE001
            pass

        # 回执里若带回了设备属性，一并同步，省掉一次快照请求
        data = payload.get('data')
        if isinstance(data, dict) and data and device_id:
            attributes = {
                key: (value['value'] if isinstance(value, dict) and 'value' in value else value)
                for key, value in data.items()
            }
            if attributes:
                fire_event(self._hass, EVENT_DEVICE_DATA_CHANGED, {
                    'deviceId': device_id,
                    'attributes': attributes
                })

    def _handle_push_notice(self, topic: str, payload: dict, known_keys: dict) -> None:
        """
        处理通知类消息。

        实际结构（抓包确认）是：
            {"type": "bugReport", "notice": {"message": "{\\"bugInfoList\\":[]}"}}
        早先假设的 params.value 结构并不存在，这里两种都兼容。
        """
        notice_type = payload.get('type')
        body = payload.get('notice')

        data = None
        if isinstance(body, dict):
            message = body.get('message')
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                except ValueError:
                    data = None
            if data is None and body:
                data = body
        if data is None:
            legacy = (payload.get('params') or {}).get('value')
            if isinstance(legacy, dict):
                data = legacy

        if not isinstance(data, dict) or not data:
            _LOGGER.debug('推送通知(%s)无结构化内容: %s', notice_type, payload)
            return

        parts = topic.split('/')
        # 账号级主题是 /sys/{userId}/thing/event/pushnotice，设备 id 只能从内容里取；
        # 设备级主题是 /sys/{productKey}/{deviceId}/thing/event/pushnotice
        device_id = data.get('deviceId')
        if not device_id and isinstance(body, dict):
            # 实测账号级通知里 deviceId 在外层 notice 上，内层 message 里没有
            device_id = body.get('deviceId')
        if not device_id and len(parts) >= 8:
            device_id = parts[3]

        status = data.get('status', data.get('online'))
        if status is None and isinstance(body, dict):
            status = body.get('status', body.get('online'))
        if status is not None and device_id:
            _LOGGER.info('设备 %s 在线状态变更: %s', device_id, status)

        if not device_id:
            _LOGGER.debug('推送通知(%s)无法定位设备，跳过: %s', notice_type, data)
            return

        keys = known_keys.get(device_id) or set()
        attributes = {
            key: value for key, value in data.items()
            if key in keys and key not in ('status', 'deviceId')
        }
        if attributes:
            fire_event(self._hass, EVENT_DEVICE_DATA_CHANGED, {
                'deviceId': device_id,
                'attributes': attributes
            })

    @staticmethod
    async def send_command(session, token, deviceId: str, attributes: dict):
        api_url = CONTROL_DEVICE_API.format(deviceId=deviceId)
        api_headers = {
            "Host": "io.zx.tcljd.com",
            "accessToken": token,
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13)XWEB/13639",
            "Content-Type": "application/json;charset=UTF-8",
            "xweb_xhr": "1",
            "miniGramSetup": "1",
            "t-platform-type": APP_PLATFORM_TYPE,
            "Referer": "https://servicewechat.com/wxed3f11c6ee178737/243/page-frame.html"
        }
        r = str(int(random.random() * 1e5))
        i = str(int(time.time() * 1000))
        params = []
        for key, value in attributes.items():
            attr = {}
            attr[str(key)] = value
            params.append(attr)
        api_body = {
            "msgId": f"miniprogram_{r}_{i}",
            "source": "miniprogram",
            "version": "3.0.0",
            "params": params
        }
        async with session.post(url=api_url, headers=api_headers, json=api_body) as response:
            content = await response.json(content_type=None)
            if not isinstance(content, dict):
                _LOGGER.warning('Device %s control response is not a json object: %s', deviceId, content)
                return

            # 注意：code 可能是 int 200，也可能是字符串 '200'，统一转成字符串比较
            code = str(content.get('code', '')).strip()
            if 'traceId' in content and code not in ('200', '0'):
                raise TclClientException(
                    '接口返回异常: {}'.format(content.get('message') or content)
                )
            _LOGGER.debug('Device %s control %s -> %s', deviceId, attributes, content.get('message'))

    # ============================================================
    # 登录方法 (iOS App 凭据 + RSA 加密)
    # ============================================================

    def _build_login_headers(self) -> dict:
        """构建登录请求头"""
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "t-platform-type": IOS_PLATFORM_TYPE,
            "uid": "",
            "Accept": "*/*",
            "cid": str(uuid.uuid4()).upper(),
            "Accept-Language": "zh-Hans-CN;q=1, zh-Hant-CN;q=0.9, en-CN;q=0.8",
            "token": "",
            "EncryptVersion": "2.0",
            "t-app-version": IOS_APP_VERSION,
            "t-store-uuid": IOS_STORE_UUID,
            "Encrypt": "true",
            "User-Agent": IOS_USER_AGENT,
            "t-application-name": "",
        }

    async def login_by_password(self, phone: str, password: str) -> dict:
        """密码登录"""
        from .crypto import md5_hash, encrypt_url_params, encrypt_body

        device_id = str(uuid.uuid4()).upper()
        params = {
            "deviceId": device_id,
            "password": md5_hash(password),
            "channel": IOS_STORE_UUID,
            "tenantId": IOS_TENANT_ID,
            "appSecret": IOS_APP_SECRET,
            "username": phone,
            "appId": IOS_APP_ID,
            "reportState": IOS_REPORT_STATE,
        }

        url = f"{LOGIN_API}?{encrypt_url_params(params)}"
        body = encrypt_body(params)
        headers = self._build_login_headers()

        async with self._session.post(url=url, headers=headers, data=body) as resp:
            result = await resp.json(content_type=None)
            if 'accessToken' not in result:
                raise TclClientException('登录失败: ' + str(result.get('message', result)))
            return {
                'accountId': result['accountId'],
                'accessToken': result['accessToken'],
                'refreshToken': result['refreshToken'],
            }

    async def send_sms_captcha(self, phone: str) -> None:
        """发送短信验证码"""
        from .crypto import encrypt_url_params, generate_sign, generate_nonce

        import time as _time
        base_params = {
            "appId": IOS_APP_ID,
            "appSecret": IOS_APP_SECRET,
            "tenantId": IOS_TENANT_ID,
            "timestamp": str(int(_time.time() * 1000)),
            "nonce": generate_nonce(),
            "mobile": phone,
            "bType": "LOGIN",
        }
        base_params["sign"] = generate_sign(base_params)

        url = f"{SMS_CAPTCHA_API}?{encrypt_url_params(base_params)}"
        headers = self._build_login_headers()

        async with self._session.get(url=url, headers=headers) as resp:
            result = await resp.json(content_type=None)
            status = result.get('status', '')
            code = result.get('code')
            if status != 'SUCCESS' and code not in (1, 200, 0, '1', '200', '0'):
                raise TclClientException('短信发送失败: ' + str(result.get('message', result)))

    async def login_by_sms(self, phone: str, code: str) -> dict:
        """验证码登录 (quickLogin)"""
        from .crypto import encrypt_url_params, encrypt_body

        device_id = str(uuid.uuid4()).upper()
        params = {
            "deviceId": device_id,
            "bType": "LOGIN",
            "appSecret": IOS_APP_SECRET,
            "username": phone,
            "appId": IOS_APP_ID,
            "tenantId": IOS_TENANT_ID,
            "validCode": code,
            "reportState": IOS_REPORT_STATE,
        }

        url = f"{QUICK_LOGIN_API}?{encrypt_url_params(params)}"
        body = encrypt_body(params)
        headers = self._build_login_headers()

        async with self._session.post(url=url, headers=headers, data=body) as resp:
            result = await resp.json(content_type=None)
            if 'accessToken' not in result:
                raise TclClientException('登录失败: ' + str(result.get('message', result)))
            return {
                'accountId': result['accountId'],
                'accessToken': result['accessToken'],
                'refreshToken': result['refreshToken'],
            }

    async def get_electricity_summary(self, deviceId: str, product_key: str, time_type: int = 1) -> dict:
        """
        获取设备电量统计
        :param deviceId: 设备ID
        :param product_key: 设备 productKey (作为请求头)
        :param time_type: 时间类型 (1=周, 2=月, 3=年)
        :return: API 原始 data 字段，包含 ecoDetails 和 workModeDetails
        """
        api_url = ELECTRICITY_SUMMARY_API + '?timeType=' + str(time_type)
        api_headers = self._get_io_headers()
        api_headers['productKey'] = product_key
        api_headers['deviceId'] = deviceId
        if self._user_id:
            api_headers['userId'] = self._user_id
        async with self._session.get(url=api_url, headers=api_headers) as response:
            content = await response.json(content_type=None)
            self._assert_response_successful(content)
            return content.get('data', {})

    @staticmethod
    def _assert_response_successful(resp):
        if 'traceId' in resp and str(resp['code']) not in ('200', '0'):
            raise TclClientException('接口返回异常: ' + resp['message'])
