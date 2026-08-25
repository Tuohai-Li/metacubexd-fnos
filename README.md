# MetaCubeXD for fnOS

[![GitHub release](https://img.shields.io/github/v/release/Tuohai-Li/metacubexd-fnos?label=Latest&color=blue)](https://github.com/Tuohai-Li/metacubexd-fnos/releases)
[![Validate](https://github.com/Tuohai-Li/metacubexd-fnos/actions/workflows/validate.yml/badge.svg)](https://github.com/Tuohai-Li/metacubexd-fnos/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/Tuohai-Li/metacubexd-fnos/blob/main/LICENSE)
[![fnOS 1.1.31xx](https://img.shields.io/badge/fnOS-1.1.31xx+-orange.svg)](https://developer.fnnas.com/docs/guide)
[![MetaCubeXD](https://img.shields.io/github/v/release/MetaCubeX/metacubexd?label=MetaCubeXD&color=purple)](https://github.com/MetaCubeX/metacubexd/releases)

> 面向飞牛 NAS（fnOS）的 MetaCubeXD 应用包：管理 Mihomo 的节点、规则、连接、日志与配置。
>
> 内置同源 HTTP/WebSocket 代理，可在局域网和 FN Connect HTTPS 环境中访问 NAS 内部的 Mihomo API。

将 [MetaCubeXD](https://github.com/metacubex/metacubexd) 打包为飞牛 NAS (fnOS) 桌面窗口应用。

## 主要特性

- **FN Connect 可用**：浏览器只访问面板自身的 `/mihomo`，NAS 服务端再连接 Mihomo，避免 HTTPS 混合内容与浏览器跨域限制。
- **同 NAS 默认可用**：默认后端为 `http://127.0.0.1:9090`，这里的回环地址指向 NAS，而不是访问者设备。
- **完整实时代理**：支持普通 HTTP API，以及流量、日志、连接等 WebSocket 数据。
- **安全更新与回退**：UI 更新校验官方 SHA-256 后原子切换，失败时保留旧版并可回退到包内资源。
- **服务端订阅导入**：由 fnOS 服务端下载远程配置，兼容限制浏览器 User-Agent 或 CORS 的订阅服务，并包含 SSRF 防护。
- **自动跟进上游**：每日检查 MetaCubeXD Stable Release，通过 PR 更新 UI 与包版本。

---

## 快速开始 / Quick Start

1. 从 [Releases](https://github.com/Tuohai-Li/metacubexd-fnos/releases) 下载 `metacubexd-x.x.x.fpk`
2. 飞牛 App Center → **手动安装** → 选择 fpk 文件
3. 桌面出现 **MetaCubeXD** 图标，点击打开面板（端口 9091）
4. 面板通过 fnOS 同源代理自动连接 Mihomo API（默认由 NAS 访问 `http://127.0.0.1:9090`）

### 使用条件

- fnOS `1.1.31xx` 或更高版本
- x86_64 NAS
- Mihomo `external-controller` 已在 NAS 的 `9090` 端口运行，或存在其他 NAS 可访问的 HTTP/HTTPS 控制地址

## 应用设置 / App Settings

安装后可在 **App Center → 应用设置** 中配置 Mihomo API 地址，无需手动改代码：

| 设置项 | 默认值 | 说明 |
|---|---|---|
| Mihomo API 地址 | `http://127.0.0.1:9090` | NAS 服务端访问的 external-controller 地址；也可填其他 LAN HTTP/HTTPS 地址 |

修改后需**重启应用**生效（App Center → 停止 → 启动）。浏览器始终连接面板自身的 `/mihomo`，因此 `127.0.0.1` 正确指向 NAS，而不是访问者电脑。

### 访问链路

```text
浏览器（局域网或 FN Connect HTTPS）
  └─ 面板 :9091/mihomo/*
       └─ fnOS 同源代理
            └─ Mihomo http://127.0.0.1:9090/*
```

无需在浏览器中放行“不安全内容”，也无需把 Mihomo 的 `9090` 控制端口直接暴露到公网。

## 连接与更新

- **局域网与 FN Connect**：HTTP API 和实时流量/日志 WebSocket 都经同源代理转发，避免跨域和 HTTPS 混合内容拦截。
- **UI 在线更新**：MetaCubeXD 检测到稳定版后，点击 UI 版本即可下载官方 `compressed-dist.tgz`；应用校验 GitHub SHA-256 后原子切换，失败保留旧版。
- **Core 在线更新**：点击 Core 版本时由同源代理调用外部 Mihomo 的自更新接口，并明确使用 `channel=stable&force=true`；core 二进制仍由其自身安全替换。
- **远程配置导入**：配置页的“拉取远程配置”由 fnOS 服务端以 Mihomo 客户端身份下载，再沿用 MetaCubeXD 原有流程导入 Core；支持会拒绝浏览器 User-Agent/CORS 的订阅服务。
- **包内兜底**：运行时 UI 保存在 fnOS 数据目录，`app/www` 始终保留为离线恢复副本。
- **上游同步**：仓库每天检查 MetaCubeXD Stable Release，并通过 PR 更新包内 UI 与 manifest，不直接写入 `main`。

## 端口 / Port

- **面板端口**：9091
- **Mihomo API**：9090
- **Mihomo HTTP 代理**：7890

## 开发与构建

上游同步脚本可手动运行：

```bash
python3 scripts/sync_upstream.py
python3 scripts/validate_package.py
python3 scripts/build_fpk.py  # 无 fnpack 的开发机
fnpack build                 # fnOS/Linux 官方工具
```

同步只替换官方 UI 发行资源；fnOS 的 manifest、生命周期、代理与数据目录结构保持独立。

> 订阅 URL 通常包含访问密钥。请勿公开分享；如果曾经泄露，请在服务商后台重新生成。

> 📖 上游项目：[metacubex/metacubexd](https://github.com/metacubex/metacubexd) · [在线 Demo](https://metacubex.github.io/metacubexd/)

## 相关链接 / Links

- [Mihomo Core](https://github.com/techysy/mihomo-core-fnos) — 独立 mihomo 内核服务（本面板对应的内核）
- [Hermes WebUI](https://github.com/techysy/hermes-webui-fnos) — Hermes 相关 fnOS 应用（WebUI 浏览器访问）
- [9Router](https://github.com/techysy/9router-fnos) — Hermes 相关 fnOS 应用（FREE AI 路由器 / API 代理）
- [Strava Panel](https://github.com/techysy/strava-panel-fnos) — Hermes 相关 fnOS 应用（Strava 骑行数据面板）
- [fnOS 开发者文档](https://developer.fnnas.com/docs/guide)

## License

MIT — 与 [metacubex/metacubexd](https://github.com/metacubex/metacubexd) 一致
