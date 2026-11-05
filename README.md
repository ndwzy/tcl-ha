# TCL空调
https://bbs.hassbian.com/thread-29746-1-1.html
本插件可将TCL空调设备接入HomeAssistant，理论上支持所有小程序设备。
主要用来适配本人的小蓝鸟空调，小程序不支持的设备需要获取设备zip配置文件进行适配，工作量大且手头也没那么多tcl设备所以目前不准备适配。
目前手头家居大多都换上小米设备了，不出问题的话可能较长时间都不会更新

使用抓包工具抓到小程序登录时的account_id和refreshToken即可
目前已支持账号直接登录

感谢[banto6](https://github.com/banto6/haier)提供的参考
## 已支持实体
- Switch
- Number
- Select
- Sensor
- CLIMATE
## 安装

方法1：下载并复制`custom_components/tcl`文件夹到HomeAssistant根目录下的`custom_components`文件夹即可完成安装

方法2：已经安装了HACS，可以点击按钮快速安装 [![通过HACS添加集成](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ndwzy&repository=tcl-ha&category=integration)

## 配置

配置 > 设备与服务 >  集成 >  添加集成 > 搜索`tcl`

或者点击: [![添加集成](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=tcl-ha)

## 登录方式（重要）

集成有两条登录通道，**会不会把手机上的 TCL App 挤下线，取决于走哪条**：

| 登录方式 | 通道 appId | 对 TCL App 的影响 |
| --- | --- | --- |
| **Token 登录（推荐）** | `55141607047147220`（小程序） | 不挤下线 |
| 密码登录 / 短信验证码登录 | `28411606743791229`（iOS） | **会把 App 挤下线** |

原因：TCL 账号系统按 appId 隔离会话。iOS 通道和手机上的 TCL App 属于同一个 appId，
拿同一账号再登录一次就会顶掉原有会话；小程序通道是另一个 appId，两条会话互不干扰。

### 用 Token 登录（不会挤掉 App）

1. 用抓包工具（Reqable / Charles 等）抓一次 TCL 小程序的登录请求
   `POST https://cn.account.tcl.com/auth/thirdParty/bindUserByAuthCode`；
2. 从响应 JSON 里复制 `refreshToken`（`eyJ` 开头的三段式字符串，约 1200 字符）；
3. HA 里「添加集成 → TCL → Token 登录」，粘贴 refreshToken；`Account Id` 留空即可，
   集成会从 JWT 载荷里自动解析。

登录之后：

- accessToken 有效期 **30 天**，集成在到期前 5 分钟自动续期；
- refreshToken 有效期 **60 天**，每次续期都换成新的，可以一直续下去；
- 续期只是延长同一个会话，**不会重新登录**，所以 App 不会被挤掉。

## 状态同步（重要）

设备状态变化（包括用遥控器、空调面板操作）会**通过 MQTT 实时推送**，集成收到后立刻更新实体，
正常情况下不需要等待。

轮询只作为**兜底**：只有长时间收不到任何推送时，才向云端拉取一次完整快照。规则是：

- 基准间隔默认 **300 秒**，实际间隔叠加随机抖动（最晚约 1.6 倍，即 500 秒左右）；
- **每收到一条 MQTT 消息，计时就重新开始**，所以设备活跃时几乎不会真正发起请求；
- MQTT 完全静默（断网、云端异常等）时，才按上述间隔兜底刷新。

可在 `设置 > 设备与服务 > TCL > 配置 > 状态轮询` 中调整基准间隔，设为 `0` 关闭轮询。

MQTT 连接本身带自动重连：断线后由客户端退避重连，重连成功会重新订阅主题并补拉一次快照；
如果长时间连不上、或连上后长时间收不到任何消息，集成会主动重建连接，并在重建前检查 token 是否过期。

## 调试
在`configuration.yaml`中加入以下配置来打开调试日志。

```yaml
logger:
  default: warn
  logs:
    custom_components.tcl: debug
```
