# MetaCubeXD for fnOS

Mihomo Dashboard — manage rules, nodes, and connections. Connects directly to local Mihomo API.

[![GitHub release](https://img.shields.io/github/v/release/techysy/metacubexd-fnos?label=Release&color=blue)](https://github.com/techysy/metacubexd-fnos/releases)
[![Downloads](https://img.shields.io/github/downloads/techysy/metacubexd-fnos/total?label=Downloads&color=green)](https://github.com/techysy/metacubexd-fnos/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/techysy/metacubexd-fnos/blob/main/LICENSE)
[![fnOS 1.1.31xx](https://img.shields.io/badge/fnOS-1.1.31xx+-orange.svg)](https://developer.fnnas.com/docs/guide)
[![MetaCubeXD](https://img.shields.io/github/v/release/metacubex/metacubexd?label=MetaCubeXD&color=purple)](https://github.com/metacubex/metacubexd)

> Packages [MetaCubeXD](https://github.com/metacubex/metacubexd) as a fnOS desktop window app.

- [中文 README](./README.md)

---

## ✨ Features

- 🖥️ **Mihomo dashboard** — manage rules, nodes, connections
- 🔗 **Same-origin Mihomo proxy** — works with local/LAN cores and FN Connect HTTPS
- 🔄 **Verified stable UI updates** — atomic GitHub Release updates with rollback
- ⬆️ **Stable core updates** — forwards the core updater with `channel=stable&force=true`
- 📥 **Server-side subscription import** — fetches remote profiles as a Mihomo client when providers reject browser CORS/User-Agent requests
- 📦 **One-click deploy** — App Center manual install

## 🚀 Quick Install

1. Download `metacubexd-x.x.x.fpk` from [Releases](https://github.com/Tuohai-Li/metacubexd-fnos/releases)
2. fnOS **App Center → Manual Install** → select the fpk
3. Click the **MetaCubeXD** icon (port 9091)
4. The panel connects through the fnOS same-origin proxy (the NAS targets `http://127.0.0.1:9090` by default)

## 📖 Usage

### App Settings

Configure the Mihomo API address in **App Center → App Settings** (no code changes needed):

| Setting | Default | Description |
|---------|---------|-------------|
| Mihomo API address | `http://127.0.0.1:9090` | Server-side external-controller URL; another LAN HTTP/HTTPS URL is also supported |

**Restart** the app to apply (App Center → Stop → Start).

### Ports

| Service | Port |
|---------|------|
| Dashboard | 9091 |
| Mihomo API | 9090 |
| Mihomo HTTP proxy | 7890 |

## Remote Access and Updates

LAN and FN Connect access both use the app's same-origin `/mihomo` HTTP/WebSocket proxy. This keeps a local HTTP core usable from an HTTPS fnOS remote page without mixed-content or CORS failures.

If the Core listens only on the NAS LAN address, a failed default loopback connection falls back to the private NAS IP used to open the app. You can also set `http://NAS-LAN-IP:9090` explicitly in the app settings.

When MetaCubeXD reports a stable UI update, click the UI version badge. The fnOS adapter downloads the official `compressed-dist.tgz`, verifies GitHub's SHA-256 digest, and atomically switches the persisted UI. A failed update leaves the current UI untouched.

The Core version button invokes the separately deployed Mihomo core's own updater through the same-origin proxy. It explicitly selects the stable channel and forces a fresh update check; the dashboard never overwrites an external core binary itself.

Remote profile imports are fetched by the fnOS adapter and then passed to MetaCubeXD's existing authenticated Core import flow. Private/reserved destinations, unsafe redirects, oversized responses, and non-configuration content are rejected.

> Detailed troubleshooting: [TROUBLESHOOTING.md](./TROUBLESHOOTING.md)

## 🐛 Troubleshooting

Install/connect/mixed-content issues: see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

## 🛠️ Build from Source

```bash
# On the fnOS NAS
fnpack build   # produces metacubexd.fpk
```

## 🔮 Roadmap

The scheduled GitHub workflow tracks stable [MetaCubeXD releases](https://github.com/MetaCubeX/metacubexd/releases) and opens a validated synchronization PR. Manual synchronization is also available:

```bash
python3 scripts/sync_upstream.py
python3 scripts/validate_package.py
python3 scripts/build_fpk.py  # downloads/verifies official fnpack 1.2.3, then builds and audits
fnpack build                 # direct alternative when official fnpack is installed
```

## 📚 Related

- [metacubex/metacubexd](https://github.com/metacubex/metacubexd) — upstream · [live demo](https://metacubex.github.io/metacubexd/)
- [Hermes WebUI](https://github.com/techysy/hermes-webui-fnos) · [9Router](https://github.com/techysy/9router-fnos) · [Strava Panel](https://github.com/techysy/strava-panel-fnos) — more fnOS apps
- [fnOS Developer Docs](https://developer.fnnas.com/docs/guide)

## License

MIT — same as [metacubex/metacubexd](https://github.com/metacubex/metacubexd)
