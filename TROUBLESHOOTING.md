# TROUBLESHOOTING / 故障排除

## 面板无法连接 Core

1. 在 fnOS App Center 的 MetaCubeXD 设置中确认 Mihomo API 地址。
2. core 与面板在同一台 NAS 时使用 `http://127.0.0.1:9090`；core 在其他设备时填写 NAS 能访问的 LAN HTTP/HTTPS 地址。
3. 重启 MetaCubeXD 后访问 `http://<NAS-IP>:9091/healthz`，确认服务返回 `status: ok`。
4. 查看应用数据目录中的 `metacubexd.log`。`502 Mihomo core is unavailable` 通常表示 core 未启动、端口错误或 HTTPS 证书无效。

浏览器显示的 endpoint 应是当前面板地址下的 `/mihomo`，而不是直接的 `127.0.0.1:9090`。应用会在首次升级时自动迁移旧 endpoint，并保留已保存的 secret。

## FN Connect 下没有实时流量或日志

实时数据使用 WebSocket。确认 fnOS/FN Connect 入口允许 WebSocket Upgrade，并检查浏览器网络面板中 `/mihomo/traffic`、`/mihomo/logs` 是否返回 `101 Switching Protocols`。

## UI 更新失败

- NAS 必须能够访问 `api.github.com` 和 GitHub Release 下载地址。
- 更新只接受 MetaCubeXD 正式稳定版，并强制校验 Release 资产的 SHA-256 digest。
- 下载、校验或解压失败时当前 UI 不会被替换；详细原因记录在 `metacubexd.log`。
- 包内 `app/www` 是只读兜底。删除数据目录下损坏的 `ui/current` 后重启应用，会从包内版本重新初始化。

## Core 更新后版本没有变化

- 面板会把更新请求转成 `/upgrade?channel=stable&force=true`，当前目标是 Mihomo 最新正式稳定版。
- Core 是独立部署的服务，本应用不会直接覆盖它的可执行文件；实际替换仍由 Mihomo 自带 updater 完成。
- 下载最长等待 5 分钟。更新期间暂时断开属于 core 重启的正常现象，完成后刷新页面确认版本。
- 若仍未变化，请查看 core 自身日志；常见原因是可执行文件不可写、容器映射为只读、运行方式禁用了自更新或 NAS 无法下载 GitHub Release。

## 远程订阅无法导入

- 从配置页使用“拉取远程配置”。`fnos.3` 起，外部 HTTP/HTTPS 请求会由 fnOS 服务端使用 `clash.meta` 客户端标识拉取，再交给当前 Core 导入。
- 订阅服务必须返回 UTF-8 的 Mihomo/Clash YAML 或 JSON；登录页、限流 HTML、二进制内容和超过 16 MiB 的响应会被拒绝。
- 为避免 SSRF，订阅地址及每次重定向只能指向公网 IP，不能访问 NAS、局域网、localhost 或保留地址。
- `400` 表示 URL 不安全或格式错误，`422` 表示响应不是可识别配置，`502` 表示域名、TLS、网络或订阅服务响应失败。
- 订阅 URL 中的 token 等同密码。泄露后应立即在服务商后台重新生成。
