import logging
from typing import Any, Dict

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.config_validation import multi_select

from .const import DOMAIN, FILTER_TYPE_EXCLUDE, FILTER_TYPE_INCLUDE
from .const import LOGIN_METHOD_TOKEN, LOGIN_METHOD_PASSWORD, LOGIN_METHOD_SMS
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
from .core.client import TclClientException, TclClient, account_id_from_refresh_token
from .core.config import AccountConfig, DeviceFilterConfig, EntityFilterConfig

_LOGGER = logging.getLogger(__name__)

ACCOUNT_ID = 'account_id'
REFRESH_TOKEN = 'refresh_token'


# ============================================================
# 登录辅助函数
# ============================================================

async def _do_token_login(hass, account_id, refresh_token):
    """Token 登录, 返回 (account_id, token, refresh_token, mobile)"""
    client = TclClient(hass, account_id, '')
    await client.initialize()
    token_info = await client.refresh_token(refresh_token)
    client2 = TclClient(hass, account_id, token_info.token)
    await client2.initialize()
    user_info = await client2.get_user_info()
    return (account_id, token_info.token, token_info.refresh_token,
            user_info['mobile'], token_info.expires_at)


async def _do_password_login(hass, phone, password):
    """密码登录, 返回 (account_id, token, refresh_token, mobile)"""
    client = TclClient(hass, '', '')
    await client.initialize()
    result = await client.login_by_password(phone, password)
    client2 = TclClient(hass, result['accountId'], result['accessToken'])
    await client2.initialize()
    user_info = await client2.get_user_info()
    return result['accountId'], result['accessToken'], result['refreshToken'], user_info['mobile']


async def _do_sms_send(hass, phone):
    """发送短信验证码"""
    client = TclClient(hass, '', '')
    await client.initialize()
    await client.send_sms_captcha(phone)


async def _do_sms_login(hass, phone, code):
    """验证码登录, 返回 (account_id, token, refresh_token, mobile)"""
    client = TclClient(hass, '', '')
    await client.initialize()
    result = await client.login_by_sms(phone, code)
    client2 = TclClient(hass, result['accountId'], result['accessToken'])
    await client2.initialize()
    user_info = await client2.get_user_info()
    return result['accountId'], result['accessToken'], result['refreshToken'], user_info['mobile']


def _login_method_selector(default=LOGIN_METHOD_TOKEN):
    return vol.In({
        LOGIN_METHOD_TOKEN: 'Token 登录（推荐：不会挤掉 TCL App）',
        LOGIN_METHOD_PASSWORD: '密码登录（iOS 通道：会让 App 掉线）',
        LOGIN_METHOD_SMS: '短信验证码登录（iOS 通道：会让 App 掉线）',
    })


class TclConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """选择登录方式"""
        if user_input is not None:
            method = user_input['login_method']
            if method == LOGIN_METHOD_TOKEN:
                return await self.async_step_token()
            elif method == LOGIN_METHOD_PASSWORD:
                return await self.async_step_password()
            elif method == LOGIN_METHOD_SMS:
                return await self.async_step_sms_phone()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required('login_method', default=LOGIN_METHOD_TOKEN): _login_method_selector()
            })
        )

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Token 登录

        推荐入口：从 TCL 小程序抓一次 refreshToken 粘进来即可。
        这条路径走的是小程序通道的会话，不会让手机上的 TCL App 掉线；
        之后 accessToken 每 30 天自动续期，refreshToken 60 天一轮换，长期不用再登录。
        """
        errors: Dict[str, str] = {}
        if user_input is not None:
            refresh_token = (user_input.get(REFRESH_TOKEN) or '').strip()
            account_id = (user_input.get(ACCOUNT_ID) or '').strip()
            if not account_id:
                # 允许只粘贴 refreshToken，账号 id 直接从 JWT 载荷里解析
                account_id = account_id_from_refresh_token(refresh_token)
            if not account_id:
                errors['base'] = 'account_id_required'
            else:
                try:
                    account_id, token, refresh_token, mobile, expires_at = await _do_token_login(
                        self.hass, account_id, refresh_token
                    )
                    return self.async_create_entry(
                        title="Tcl - {}".format(mobile),
                        data={'account': {
                            'login_method': LOGIN_METHOD_TOKEN,
                            'phone': '',
                            'account_id': account_id,
                            'token': token,
                            'refresh_token': refresh_token,
                            'expires_at': expires_at,
                            'default_load_all_entity': user_input['default_load_all_entity']
                        }}
                    )
                except TclClientException as e:
                    _LOGGER.warning(str(e))
                    errors['base'] = 'auth_error'
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({
                vol.Optional(ACCOUNT_ID, default=''): str,
                vol.Required(REFRESH_TOKEN): str,
                vol.Required('default_load_all_entity', default=True): bool,
            }),
            errors=errors
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """密码登录"""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                account_id, token, refresh_token, mobile = await _do_password_login(
                    self.hass, user_input['phone'], user_input['password']
                )
                return self.async_create_entry(
                    title="Tcl - {}".format(mobile),
                    data={'account': {
                        'login_method': LOGIN_METHOD_PASSWORD,
                        'phone': user_input['phone'],
                        'account_id': account_id,
                        'token': token,
                        'refresh_token': refresh_token,
                        'default_load_all_entity': user_input['default_load_all_entity']
                    }}
                )
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'auth_error'
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({
                vol.Required('phone'): str,
                vol.Required('password'): str,
                vol.Required('default_load_all_entity', default=True): bool,
            }),
            errors=errors
        )

    async def async_step_sms_phone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """短信登录 - 输入手机号"""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                await _do_sms_send(self.hass, user_input['phone'])
                self._sms_phone = user_input['phone']
                self._sms_default_load_all_entity = user_input['default_load_all_entity']
                return await self.async_step_sms_code()
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'sms_error'
        return self.async_show_form(
            step_id="sms_phone",
            data_schema=vol.Schema({
                vol.Required('phone'): str,
                vol.Required('default_load_all_entity', default=True): bool,
            }),
            errors=errors
        )

    async def async_step_sms_code(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """短信登录 - 输入验证码"""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                account_id, token, refresh_token, mobile = await _do_sms_login(
                    self.hass, self._sms_phone, user_input['code']
                )
                return self.async_create_entry(
                    title="Tcl - {}".format(mobile),
                    data={'account': {
                        'login_method': LOGIN_METHOD_SMS,
                        'phone': self._sms_phone,
                        'account_id': account_id,
                        'token': token,
                        'refresh_token': refresh_token,
                        'default_load_all_entity': getattr(self, '_sms_default_load_all_entity', True)
                    }}
                )
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'auth_error'
        return self.async_show_form(
            step_id="sms_code",
            data_schema=vol.Schema({
                vol.Required('code'): str,
            }),
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._sms_phone = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """功能菜单"""
        return self.async_show_menu(
            step_id="init",
            menu_options=['account', 'device', 'entity_device_selector', 'polling']
        )

    async def async_step_polling(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """状态轮询设置"""
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            # 更新 options 会触发配置项更新监听器，集成会自动重载
            self.hass.config_entries.async_update_entry(self.config_entry, options=options)
            return self.async_create_entry(title='', data=options)

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="polling",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=current):
                    vol.All(vol.Coerce(int), vol.Range(min=0, max=3600))
            })
        )

    # ============================================================
    # 账号设置 (多渠道登录)
    # ============================================================

    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """选择登录方式"""
        if user_input is not None:
            method = user_input['login_method']
            if method == LOGIN_METHOD_TOKEN:
                return await self.async_step_account_token()
            elif method == LOGIN_METHOD_PASSWORD:
                return await self.async_step_account_password()
            elif method == LOGIN_METHOD_SMS:
                return await self.async_step_account_sms_phone()

        cfg = AccountConfig(self.hass, self.config_entry)
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({
                vol.Required('login_method', default=cfg.login_method or LOGIN_METHOD_TOKEN): _login_method_selector()
            })
        )

    async def async_step_account_token(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Token 登录"""
        errors: Dict[str, str] = {}
        cfg = AccountConfig(self.hass, self.config_entry)
        if user_input is not None:
            try:
                account_id, token, refresh_token, mobile = await _do_token_login(
                    self.hass, user_input[ACCOUNT_ID], user_input[REFRESH_TOKEN]
                )
                cfg.login_method = LOGIN_METHOD_TOKEN
                cfg.phone = ''
                cfg.account_id = account_id
                cfg.token = token
                cfg.refresh_token = refresh_token
                cfg.default_load_all_entity = user_input['default_load_all_entity']
                cfg.save(mobile)
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title='', data={})
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'auth_error'
        return self.async_show_form(
            step_id="account_token",
            data_schema=vol.Schema({
                vol.Required(ACCOUNT_ID, default=cfg.account_id): str,
                vol.Required(REFRESH_TOKEN, default=cfg.refresh_token): str,
                vol.Required('default_load_all_entity', default=cfg.default_load_all_entity): bool,
            }),
            errors=errors
        )

    async def async_step_account_password(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """密码登录"""
        errors: Dict[str, str] = {}
        cfg = AccountConfig(self.hass, self.config_entry)
        if user_input is not None:
            try:
                account_id, token, refresh_token, mobile = await _do_password_login(
                    self.hass, user_input['phone'], user_input['password']
                )
                cfg.login_method = LOGIN_METHOD_PASSWORD
                cfg.phone = user_input['phone']
                cfg.account_id = account_id
                cfg.token = token
                cfg.refresh_token = refresh_token
                cfg.default_load_all_entity = user_input['default_load_all_entity']
                cfg.save(mobile)
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title='', data={})
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'auth_error'
        return self.async_show_form(
            step_id="account_password",
            data_schema=vol.Schema({
                vol.Required('phone', default=cfg.phone): str,
                vol.Required('password'): str,
                vol.Required('default_load_all_entity', default=cfg.default_load_all_entity): bool,
            }),
            errors=errors
        )

    async def async_step_account_sms_phone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """短信登录 - 输入手机号"""
        errors: Dict[str, str] = {}
        cfg = AccountConfig(self.hass, self.config_entry)
        if user_input is not None:
            try:
                await _do_sms_send(self.hass, user_input['phone'])
                self._sms_phone = user_input['phone']
                return await self.async_step_account_sms_code()
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'sms_error'
        return self.async_show_form(
            step_id="account_sms_phone",
            data_schema=vol.Schema({
                vol.Required('phone', default=cfg.phone): str,
            }),
            errors=errors
        )

    async def async_step_account_sms_code(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """短信登录 - 输入验证码"""
        errors: Dict[str, str] = {}
        cfg = AccountConfig(self.hass, self.config_entry)
        if user_input is not None:
            try:
                account_id, token, refresh_token, mobile = await _do_sms_login(
                    self.hass, self._sms_phone, user_input['code']
                )
                cfg.login_method = LOGIN_METHOD_SMS
                cfg.phone = self._sms_phone
                cfg.account_id = account_id
                cfg.token = token
                cfg.refresh_token = refresh_token
                cfg.save(mobile)
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title='', data={})
            except TclClientException as e:
                _LOGGER.warning(str(e))
                errors['base'] = 'auth_error'
        return self.async_show_form(
            step_id="account_sms_code",
            data_schema=vol.Schema({
                vol.Required('code'): str,
            }),
            errors=errors
        )

    # ============================================================
    # 设备/实体筛选 (保持不变)
    # ============================================================

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """筛选设备"""
        cfg = DeviceFilterConfig(self.hass, self.config_entry)

        if user_input is not None:
            cfg.set_filter_type(user_input['filter_type'])
            cfg.set_target_devices(user_input['target_devices'])
            cfg.save()

            return self.async_create_entry(title='', data={})

        devices = {}
        for item in self.hass.data[DOMAIN]['devices']:
            devices[item.id] = item.name

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required('filter_type', default=cfg.filter_type): vol.In({
                        FILTER_TYPE_EXCLUDE: 'Exclude',
                        FILTER_TYPE_INCLUDE: 'Include',
                    }),
                    vol.Optional('target_devices', default=cfg.target_devices): multi_select(devices)
                }
            )
        )

    async def async_step_entity_device_selector(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """筛选实体（设备选择）"""
        if user_input is not None:
            self.hass.data[DOMAIN]['entity_filter_target_device'] = user_input['target_device']
            return await self.async_step_entity_filter()

        devices = {}
        for item in self.hass.data[DOMAIN]['devices']:
            devices[item.id] = item.name

        return self.async_show_form(
            step_id="entity_device_selector",
            data_schema=vol.Schema(
                {
                    vol.Required('target_device'): vol.In(devices)
                }
            )
        )

    async def async_step_entity_filter(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """筛选实体"""
        cfg = EntityFilterConfig(self.hass, self.config_entry)

        if user_input is not None:
            cfg.set_filter_type(user_input['device_id'], user_input['filter_type'])
            cfg.set_target_entities(user_input['device_id'], user_input['target_entities'])
            cfg.save()

            await self.hass.config_entries.async_reload(self.config_entry.entry_id)

            return self.async_create_entry(title='', data={})

        target_device_id = self.hass.data[DOMAIN].pop('entity_filter_target_device', '')
        for device in self.hass.data[DOMAIN]['devices']:
            if device.id == target_device_id:
                target_device = device
                break
        else:
            raise ValueError('Device [{}] not found'.format(target_device_id))

        entities = {}
        for attribute in target_device.attributes:
            entities[attribute.key] = attribute.display_name

        filtered = [item for item in cfg.get_target_entities(target_device_id) if item in entities]

        return self.async_show_form(
            step_id="entity_filter",
            data_schema=vol.Schema(
                {
                    vol.Required('device_id', default=target_device_id): str,
                    vol.Required('filter_type', default=cfg.get_filter_type(target_device_id)): vol.In({
                        FILTER_TYPE_EXCLUDE: 'Exclude',
                        FILTER_TYPE_INCLUDE: 'Include',
                    }),
                    vol.Optional('target_entities', default=filtered): multi_select(
                        entities
                    )
                }
            )
        )
