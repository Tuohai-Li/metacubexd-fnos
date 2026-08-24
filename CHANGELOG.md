# CHANGELOG

## v1.273.0-fnos.3 (2026-08-25)

- 远程订阅改由 fnOS 服务端使用 Mihomo 客户端标识拉取，兼容拒绝浏览器请求的订阅服务
- 每次订阅请求和重定向均执行公网地址校验与 DNS 固定，阻止 SSRF、内网访问和代理循环
- 限制订阅 URL、响应大小及重定向次数，并拒绝 HTML、二进制和不可识别的配置内容
- 订阅 URL 通过 POST 请求体传递，不出现在 fnOS HTTP 访问日志中；原有 Core secret 导入流程保持不变

## v1.273.0-fnos.2 (2026-08-25)

- 核心更新请求固定补全 `channel=stable&force=true`，跟随 Mihomo 最新稳定版
- 核心下载等待时间延长至 5 分钟，并允许页面重载后继续完成请求
- 包版本增加 fnOS 修订号，便于从 `fnos.1` 直接覆盖升级

## v1.273.0-fnos.1 (2026-08-25)

- 上游 UI 更新至 MetaCubeXD v1.273.0，并校验官方 Release SHA-256
- 新增 fnOS 同源 HTTP/WebSocket 代理，支持同 NAS、LAN core 与 FN Connect HTTPS
- 接管 UI 在线更新，使用持久化目录、原子切换和包内回退
- 新增每日上游同步 PR、包结构校验及协议级自动测试

## 2026-08-03 (文档更新)

### 文档 / Docs
- README 新增「远程访问注意事项」—— 通过 fnOS HTTPS 远程访问时面板连 HTTP Mihomo 被混合内容拦截；url 版不解决；提供局域网直连 / fnOS 桌面 Chrome / 公网 HTTPS 三种方案

## v1.271.0 (2026-08-01)

### 更新 / Update

- 上游版本升级至 MetaCubeXD v1.271.0
- Upstream upgraded to MetaCubeXD v1.271.0
- 重打 url + iframe 两个 fpk（构建目录 `/vol1/1000/fnOS App/build/metacubexd-fnos`）
- Rebuilt url + iframe fpk variants (build dir `/vol1/1000/fnOS App/build/metacubexd-fnos`)
- 图标沿用官方（轻微圆角，对齐 fnOS app 圆角规范 ~2%）
- Icon kept official (slight rounded corners, matches fnOS app corner convention ~2%)

## v1.270.6 (2026-08-01)

### 初始版本 / Initial Release

- 首次打包 MetaCubeXD v1.270.6 为 fnOS 应用 / First fpk package
- 端口 9091，Python http.server，iframe 桌面窗口
- Mihomo API: http://192.168.31.31:9090
