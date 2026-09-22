# glasses — G2 文字显示通道（Phase 2 / Milestone 3）

服务器把任意文本推到手机上的 Even Hub 插件，再经 BLE 写到 G2 镜片。

**只做显示通路。** 不接麦克风、ASR、router、LLM；没有上行音频；没有聚合 / 触发 / TTL / 预算 / 去重（那些是 M4）。

物理验收（戴上眼镜、输入中文、看见字）由使用者完成。本仓库只保证：代码齐、文档按官方页如实记录、本机可以把 `--file` 推给测试 WebSocket 客户端。

---

## 1. 文档调研（先于自造 API）

依据（2026-09-22 抓取）：

| 页 | URL |
| --- | --- |
| Device APIs | https://hub.evenrealities.com/docs/build/device-apis |
| Display & UI | https://hub.evenrealities.com/docs/build/display |
| Page Lifecycle | https://hub.evenrealities.com/docs/build/page-lifecycle |
| Background & Lifecycle | https://hub.evenrealities.com/docs/build/background-lifecycle |
| Networking | https://hub.evenrealities.com/docs/build/networking |
| Architecture | https://hub.evenrealities.com/docs/get-started/architecture |
| Quickstart | https://hub.evenrealities.com/docs/get-started/quickstart |
| Your First App | https://hub.evenrealities.com/docs/get-started/quickstart/first-app |
| Local Testing | https://hub.evenrealities.com/docs/test/local-testing |
| FAQ | https://hub.evenrealities.com/docs/reference/faq |
| CLI | https://hub.evenrealities.com/docs/reference/cli |
| Design Guidelines | https://hub.evenrealities.com/docs/build/design-guidelines |
| SDK README | https://www.npmjs.com/package/@evenrealities/even_hub_sdk （0.0.15） |

任务指定的 `https://hub.evenrealities.com/docs/getting-started/overview` 本次抓取几乎无正文（只有站点壳）。对应内容在 `docs/get-started/*`（architecture / quickstart / first-app）。**不要用 unofficial 仓库或 pretext 去补官方没写的数字。**

`docs/build/device-apis` 讲的是触控 / 麦 / 定位 / IMU / 存储，**不是**写字到镜片。写字在 Display & UI + Page Lifecycle + first-app。

### 1.1 往镜片写字的 API 签名

官方没有 `sendText` / 裸 BLE 发送函数。镜片不渲染 HTML；WebView 里的 JS 通过 SDK 桥创建 **text container**，再 in-place 升级内容。

必须先：

```js
const bridge = await waitForEvenAppBridge()
```

文档原文：在桥就绪前调用 SDK 会 **silently no-op**。

**首次建页（只调用一次）** — first-app 原样：

```js
import {
  waitForEvenAppBridge,
  TextContainerProperty,
  TextContainerUpgrade,
  CreateStartUpPageContainer,
} from '@evenrealities/even_hub_sdk'

const mainText = new TextContainerProperty({
  xPosition: 0,
  yPosition: 0,
  width: 576,
  height: 288,
  borderWidth: 0,
  borderColor: 5,
  paddingLength: 4,
  containerID: 1,
  containerName: 'main',
  content: 'Hello from G2!',
  isEventCapture: 1,
})

const result = await bridge.createStartUpPageContainer(
  new CreateStartUpPageContainer({
    containerTotalNum: 1,
    textObject: [mainText],
  }),
)
// result === 0 成功；1 invalid、2 oversize、3 OOM
```

Display 页的 stacking 示例、以及 npm SDK README，也接受 **普通对象**（不必 `new`）：

```js
await bridge.createStartUpPageContainer({
  containerTotalNum: 1,
  textObject: [{ /* 同上字段 */ }],
})
```

本插件两种都试：有构造器就 `new`，否则退回 plain object。

**后续改字（推荐，无闪烁）**：

```js
await bridge.textContainerUpgrade(new TextContainerUpgrade({
  containerID: 1,
  containerName: 'main',
  content: 'Updated text',
}))
```

`containerID` + `containerName` 必须与创建时完全一致，否则文档写 **silently no-op**。`textContainerUpgrade` 返回 `boolean`。

底层逃生口（Architecture / Page Lifecycle）：`bridge.callEvenApp(method, params)`。**文档没有列出写字对应的 method 字符串**，本插件不自造该方法名，只走上面的 typed API。

Web → 眼镜路径（Architecture）：`callEvenApp` → WebView 桥 → Even Realities App → Bluetooth → 眼镜。眼镜是渲染目标，逻辑在手机 WebView。

### 1.2 显示约束

| 项 | 文档怎么说 |
| --- | --- |
| 画布 | 每眼 576×288；原点左上；4-bit 绿灰阶 |
| 单行最大字符数 | **文档未说明**（只说按容器宽度自动换行） |
| 最大行数 | **文档未说明**（`\n` 换行；溢出且 `isEventCapture: 1` 时固件滚动） |
| 内容上限 | `createStartUpPageContainer` / `rebuildPageContainer` 1000 字；`textContainerUpgrade` 2000 字 |
| 满屏大约 | 全屏 text container 大约 400–500 字（Display）；翻页建议按约 400–500 字分页（Design Guidelines） |
| CJK / 日文 | Unicode「只要字形在固件字体里」就能显示；缺字 **silently dropped**。`supported_languages` 含 `zh` / `ja`（这是插件语言列表，不是字表）。**固件是否覆盖全部中日汉字 / 假名：文档未说明** |
| 字号 | **不可调**。无对齐、无粗斜体、无自选字体。单一 LVGL 固件字体 |
| 亮度 | `textColor` 0–4（SDK 0.0.14+）是亮度不是颜色；省略则创建时默认 4 |
| emoji | FAQ：不能渲染 |
| 其它 | 无背景填充；无任意像素；无字体控制（Device APIs「What the SDK doesn't expose」） |

CJK 是否完整：不要信 unofficial `@evenrealities/pretext` / 社区字表来代替官方。用 `fixtures/display_test.txt` 在真机上测，填下面「实测显示效果」。

### 1.3 开发时怎么把插件装进 Even Hub

官方路径是 **Developer Mode + QR sideload**（不是商店安装）：

1. 手机装 Even Realities App，用同一账号登录 [hub.evenrealities.com/login](https://hub.evenrealities.com/login)。
2. 强杀并重开手机 App。Even Hub 页右上出现开发者区 / **Scan QR**。
3. 本机起 `glasses/app` 的 Vite（`:5173`）。
4. `npm install -g @evenrealities/evenhub-cli`
5. `evenhub qr --url "http://<本机LAN-IP>:5173"`（`glasses/app` Vite）
6. 手机 Scan QR。眼镜应在约 1 秒内出画面。

其它官方方式：

- `evenhub pack` 打 `.ehpk`，经开发者门户装到自己的设备（Private build）。
- `npx evenhub-simulator http://localhost:5173` 只看布局，不经 BLE。

QR sideload 是否跳过 `app.json` `network` 白名单：**文档未说明**（Local Testing 写 “Some permission prompts are skipped during dev”，没有点名 WebSocket / 白名单）。打正式包时必须把服务器 **完整 origin** 写进 whitelist，且 **不支持通配符**。

### 1.4 WebView 能不能开 WebSocket

FAQ 原文： **Yes — same whitelist rules.** 后台时预期会断。Networking：插件里的 `fetch` / XHR / **WebSockets** 都走 WebView，受白名单 + 浏览器 CORS 两道门。WebSocket 握手本身不是 CORS 的 `fetch` 预检；**文档未说明** WebView 是否额外拦未白名单的 `ws://`（按 FAQ「same whitelist rules」应按 origin 白名单处理）。

### 1.5 锁屏 / 进后台会不会挂起插件

Background & Lifecycle：

| 平台 | 引擎 | 后台 / 锁屏 |
| --- | --- | --- |
| iOS | WKWebView | WebView **继续跑**；内存 JS 还在 |
| Android | Chromium WebView | **可能被挂起**；超内存阈值进程被回收，内存态丢失 |

| 资源 | 后台 / 锁屏 |
| --- | --- |
| `localStorage` | 一定还在 |
| 内存 JS | iOS 还在；Android 挂起则可能没了 |
| 已打开的 WebSocket | iOS 通常还在；**Android 挂起时通常断开** |
| 后台再发网络请求 | FAQ：**不能**。WebView 挂起后在途请求停住 |

Local Testing 另写：开发态 QR 页在锁屏后 HMR WebSocket 会挂，恢复常要 **重新扫 QR**。这是开发页行为，与正式 `.ehpk` 的后台策略不是同一条路径。

---

## 2. 局域网拉起

两端必须在同一局域网（或手机热点）。办公室 AP isolation 会让「扫了 QR 没反应」——Even Local Testing 表里写过，这时换热点。

### 2.1 查本机 IP

```bash
# Linux
hostname -I | awk '{print $1}'

# macOS
ipconfig getifaddr en0

# Windows
ipconfig | findstr /i "IPv4"
```

把下面命令里的 `LAN_IP` 换成这台机器的局域网地址（不要用 `127.0.0.1`：手机访问不到）。

### 2.2 推送服务（8766）

```bash
pip install -r requirements.txt
python glasses/display_server.py
# 或
python glasses/display_server.py --file glasses/fixtures/display_test.txt
```

默认 `0.0.0.0:8766`。终端输入一行回车 = 向所有客户端推 `{"text":"..."}`。`--file` 等至少一个客户端连上后，每隔 3 秒推一行。`--cert` + `--key` 开 wss。

依赖：`websockets`（已写入仓库根 `requirements.txt`）。标准库没有 WebSocket 服务端。

### 2.3 Windows 防火墙放行 8766

Even 文档的 Network & Firewall 页本次抓取没有逐步规则（几乎空页）。按常规 inbound TCP：

1. Windows 安全中心 → 防火墙 → 高级设置 → 入站规则 → 新建规则。
2. 端口 → TCP → 特定本地端口 `8766`。
3. 允许连接；配置文件按你的网络勾选；命名例如 `hud-router display 8766`。
4. 若还要用 QR 拉插件，同样放行静态 HTTP 端口（下面示例 `8088`）。

Linux 本机自测不需要这条。云主机 / 公网另开安全组。

### 2.4 静态插件页

```bash
cd glasses/app && npm install && npm run dev
```

遗留单文件在 `glasses/legacy/index.html`。输入框默认 `ws://` + 页面 `hostname` + `:8766`。

QR sideload：

```bash
evenhub qr --url "http://LAN_IP:5173"
```

`glasses/app` 用 npm SDK，不再从 CDN 拉。打包时把推送服务器完整 origin 写入 `app.json` network whitelist（无通配）。错误全文画在手机页上。

### 2.5 为什么公网部署必须 wss

- Even Networking：**生产用 HTTPS**；`http://` 只适合打局域网开发服务器。
- 插件若从 `https://` origin 加载，浏览器 / WebView 会拦 **混合内容** 的 `ws://`。
- 公网明文 WebSocket 会被运营商或浏览器策略丢掉，也无法防窃听。

本机局域网 `http` 页 + `ws://LAN:8766` 够用。上公网：HTTPS 托管插件 + `--cert`/`--key` 的 `wss://`，并把该 origin 写入 `app.json` network whitelist。

---

## 3. 本里程碑文件

| 路径 | 作用 |
| --- | --- |
| `glasses/app/` | 官方 `evenhub-templates/asr` 脚手架 + M3 WS / 显示（Vite+TS，勿再压成单 HTML） |
| `glasses/legacy/index.html` | M3 单文件插件，仅作对照 |
| `glasses/display_server.py` | WS 推送；`--policy` 走 `server/display_policy.py` |
| `glasses/SDK_NOTES.md` | Step 0 文档结论（每条有 URL） |
| `glasses/CAPACITY.md` | 容量阶梯怎么测 |
| `server/display_policy.py` | 置信 / 预算 / 去重 / TTL / 长度 |
| `server/README.md` | 策略旋钮 |

`--file` 会跳过空行与 `#` 注释，并把字面量 `\n` `\t` `\\` 展开。

## 3.1 模板客户端

```bash
cd glasses/app
npm install
npm run dev
# 另一终端
npx evenhub-simulator http://localhost:5173
# 或真机
npx evenhub qr --url "http://<LAN>:5173"
```

启动页 `createStartUpPageContainer` 一次（576×288 文本容器），之后只 `textContainerUpgrade`。根页双击 `shutDownPageContainer(1)`。点镜腿 = 连/断 WS。`src/asr/stt.ts` 仍是官方空 stub，M4 **不**开麦。

## 3.2 模拟器（不用戴眼镜）

官方包是 `@evenrealities/evenhub-simulator`，**没有**叫 `even-dev` 的官方 npm。详见 `SDK_NOTES.md` §10。

```bash
npm install -g @evenrealities/evenhub-simulator
cd glasses/app && npm run dev
npx evenhub-simulator http://localhost:5173
```

社区启动器（仍调官方模拟器）：

```bash
git clone https://github.com/BxNxM/even-dev.git
cd even-dev && npm install
APP_PATH=/path/to/hud-router/glasses/app ./start-even.sh
```

模拟器不是硬件仿真：无真实 BLE / 权限 / 后台。能看容器和 `{"text":...}` 是否上屏。

## 3.3 清屏

| 方案 | 文档 | 实现 |
| --- | --- | --- |
| hide/close text API | **文档未说明**（无此方法） | 不用 |
| `content: ""` | **文档未说明**；项目经验：不清，旧字还在 | 不发送空串 |
| 半角 `" "` / 全角 `U+3000` / `"\n"` | 文档未写能清 | `clear_probe.py` 可推；**硬件确认 pending** |
| `textContainerUpgrade` 换成单字 | 文档写明 upgrade 改 content | **`clearDisplay()` 默认 `・`**（`POLICY_CLEAR_PLACEHOLDER` / localStorage `hud-router-clear-placeholder`） |

TTL 到期服务器发 `{"clear":true}`，插件调用 `clearDisplay()`。

```bash
python glasses/clear_probe.py
```

---

## 4. 已知风险

- Android 锁屏 / 进后台：WS 通常断；指数退避重连（上限 10s）。
- `createStartUpPageContainer` 只调一次；之后只 upgrade。notes：再 create 会 invalid 并堵约 2.1s。
- 官方：upgrade 硬件无闪烁；rebuild 有 flicker。notes：upgrade ~83ms，rebuild ~165ms。
- 无 hide API；空串不清。
- BLE 固定调用成本大（见 SDK_NOTES §9）。
- `speakerRole` 是 App 算法，不是固件身份。

---

## 5. 实测显示效果

真机量得（2026-09-22，用户）：**每行 28 个全角、最多 10 行**。此前 `display_test.txt`「正常显示」只是通路冒烟。

Phase 1 输出上限已改为 28（`prompts.py` 一句 + `MAX_ANSWER_CHARS`）。策略层 >28 且 ≤56 折两行，>56 丢弃。本环境无 API key，5+5 live eval 见 `eval_reports/28char/UNAVAILABLE.md`。

```bash
python glasses/display_server.py --file glasses/fixtures/capacity_probe.txt --pause
```

