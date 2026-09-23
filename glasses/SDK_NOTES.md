# Even Hub / G2 SDK 笔记（Step 0）

只根据下列已读来源归纳。有答案的条目带 URL。来源没写的一律「文档未说明」，不猜、不设计测法。

**必读来源**

| # | 来源 | URL |
| --- | --- | --- |
| 1 | even-g2-notes（社区，作者声明非官方） | https://github.com/nickustinov/even-g2-notes |
| 1a | notes 索引 | https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/README.md |
| 1b | notes Display | https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/display.md |
| 1c | notes Page lifecycle | https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/page-lifecycle.md |
| 1d | notes Performance | https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/performance.md |
| 1e | notes Device APIs | https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/device-apis.md |
| 1f | notes Simulator / even-dev | https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/simulator.md |
| 2 | npm `@evenrealities/even_hub_sdk` 0.0.15 | https://www.npmjs.com/package/@evenrealities/even_hub_sdk |
| 3 | Official Device APIs | https://hub.evenrealities.com/docs/build/device-apis |
| 4 | Official First App | https://hub.evenrealities.com/docs/get-started/quickstart/first-app |
| 5 | Official templates | https://github.com/even-realities/evenhub-templates |
| 6 | even-dev | https://github.com/BxNxM/even-dev |
| — | Official Display（Device APIs 页不写字；跟了此页） | https://hub.evenrealities.com/docs/build/display |
| — | Official Page Lifecycle | https://hub.evenrealities.com/docs/build/page-lifecycle |
| — | Official Background & Lifecycle | https://hub.evenrealities.com/docs/build/background-lifecycle |
| — | Official FAQ | https://hub.evenrealities.com/docs/reference/faq |
| — | Official Simulator | https://hub.evenrealities.com/docs/test/simulator |

项目经验（**不是文档**）：推 `""` **不能**清屏，旧字还在。下面仍按文档找「正确清屏」——文档没有专用 API。

---

## 1. 怎样 clear / hide 文本容器？

- 官方 Device APIs / Display / Page Lifecycle / SDK README 的方法表里，**没有** hide / close / clear / blank text container。typed 方法是 create / rebuild / `textContainerUpgrade` / `updateImageRawData` / `shutDownPageContainer` / `callEvenApp`。  
  https://hub.evenrealities.com/docs/build/page-lifecycle  
  https://hub.evenrealities.com/docs/build/display  
  https://www.npmjs.com/package/@evenrealities/even_hub_sdk
- `EvenAppMethod` 枚举同样没有 hide/clear 文本项（只有 Create / Rebuild / TextContainerUpgrade / UpdateImageRawData / ShutDown 等）。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/page-lifecycle.md
- 官方对 `textContainerUpgrade` 的说明是：in-place 改 `content`；`containerID`+`containerName` 必须一致，否则 silently no-op。**没有**写 `content: ""` 会怎样。  
  https://hub.evenrealities.com/docs/build/display  
  https://hub.evenrealities.com/docs/build/page-lifecycle
- 推空字符串 `""` 是否清屏：**文档未说明**（官方与 notes 都没写）。项目经验：不清，旧内容仍在。
- notes「Clearing the display」讲的是 **image** 用空白图 vs `rebuildPageContainer` 的耗时交叉（1 个 image 用空白发送更快，≥2 用 rebuild），**不是**文本 hide API。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/performance.md
- 文档里和「去掉字」最接近、且写明了的路：再一次 `textContainerUpgrade` 换成别的 `content`（官方：非空新内容会更新）。用空格 / 全角空格 / 占位符 **是否在视觉上清空：文档未说明**。  
  https://hub.evenrealities.com/docs/build/display
- `rebuildPageContainer` 会整页销毁再建（官方：brief flicker；可换成别的布局）。用它「清成空白页」是否被文档承认为清屏手段：**文档未说明**（只说 replace entire page）。  
  https://hub.evenrealities.com/docs/build/page-lifecycle

**结论（文档）：没有 documented 的 hide/clear。** 实现上只能走已文档化的 `textContainerUpgrade(content=占位符)` 或 rebuild；空串行为文档未说明。

---

## 2. create / rebuild / textContainerUpgrade — 何时用；可靠性

- **`createStartUpPageContainer`**：启动时**恰好一次**建首页；返回码 0 成功，1 invalid，2 oversize，3 OOM。  
  https://hub.evenrealities.com/docs/get-started/quickstart/first-app  
  https://hub.evenrealities.com/docs/build/page-lifecycle
- 桥未就绪就调 SDK：**silently no-op**。必须先 `await waitForEvenAppBridge()`。  
  https://hub.evenrealities.com/docs/get-started/quickstart/first-app
- notes：第一次之后再调 create 会被拒，`invalid`，并阻塞约 **2.1 s**；应 latch「已经调用过」而不是「调用成功」。失败后只剩 `rebuildPageContainer`。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/page-lifecycle.md
- **`rebuildPageContainer`**：整页替换（改数量/类型/布局、翻页）。状态丢失；官方：**brief flicker on hardware**。只在布局变时用。  
  https://hub.evenrealities.com/docs/build/page-lifecycle
- **`textContainerUpgrade`**：已有容器上改字；更快、硬件上 flicker-free；ID+Name 必须一致。频繁计数/状态/直播数据用这个。  
  https://hub.evenrealities.com/docs/build/display  
  https://hub.evenrealities.com/docs/get-started/quickstart/first-app
- notes 实测耗时（固件 2.2.7.14 / App 2.2.7 / SDK 0.0.13）：upgrade **~83 ms/次**，rebuild **~165 ms 一口价**。2 个容器打平；5–6 个 upgrade 大约比一次 rebuild 慢 3 倍。升级适合已经在场的 **1–2** 个文本容器，不是 rebuild 的替代品。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/performance.md
- notes：退出确认框出现后，rebuild 仍可能返回 ok、文本仍能画，但 `updateImageRawData` 会立刻 `sendFailed`（1–3 ms），直到进程重启。这是 **image 通道**，不是文本 upgrade 失败。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/page-lifecycle.md
- 官方 vs 社区对「rebuild 在真机上是否偶发失败」：**官方未说明失败率**。notes 写 flicker + 退出框后的 image wedge，没有写「rebuild 整页经常失败」。
- 根页退出必须 `shutDownPageContainer(1)`（系统确认框）。mode 0 或自绘退出 UI 会被 QA 拒。  
  https://hub.evenrealities.com/docs/build/page-lifecycle  
  https://hub.evenrealities.com/docs/get-started/quickstart/first-app  
  https://github.com/even-realities/evenhub-templates（四个模板都是 double-tap → mode 1）

---

## 3. `TextContainerProperty` 字段

官方 Display + notes Display（一致处合并）。https://hub.evenrealities.com/docs/build/display  
https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/display.md

**共享**

| 字段 | 类型 | 范围 | 含义 |
| --- | --- | --- | --- |
| `xPosition` | number | 0–576 | 左缘 px |
| `yPosition` | number | 0–288 | 上缘 px |
| `width` | number | 0–576 | 宽 |
| `height` | number | 0–288 | 高 |
| `containerID` | number | 页内唯一 | 升级用 |
| `containerName` | string | max 16 | 页内唯一，升级用 |
| `isEventCapture` | 0/1 | 每页恰好一个 1 | 谁收输入 |
| `zOrderIndex` | number | 页内唯一；全有或全无 | 大者在前（SDK 0.0.12+） |

**边框（文本/列表）**

| 字段 | 范围 | 含义 |
| --- | --- | --- |
| `borderWidth` | 0–5 | 0=无边框 |
| `borderColor` | 文本 0–16 / 列表 0–15 | 灰阶 |
| `borderRadius` | 0–10 | 圆角（官方保留 SDK 拼写） |
| `paddingLength` | 0–32 | 四边同一 padding |

**文本特有**

| 字段 | 含义 |
| --- | --- |
| `content` | 字符串 |
| `textColor` | 0–4 **亮度**不是颜色（SDK 0.0.14+）。创建/重建省略=设备默认 4；upgrade 省略=保持当前。超范围本地失败。https://hub.evenrealities.com/docs/build/display https://www.npmjs.com/package/@evenrealities/even_hub_sdk |

左对齐、顶对齐。无对齐/字号/粗斜体。无背景填充。  
https://hub.evenrealities.com/docs/build/display  
https://hub.evenrealities.com/docs/build/device-apis （What the SDK doesn't expose）

`TextContainerUpgrade`：必填 `containerID` / `containerName` / `content`；可选 `contentOffset` / `contentLength`（部分替换）、`textColor`。  
https://hub.evenrealities.com/docs/build/display

---

## 4. 换行与溢出

- **自动按容器宽度 wrap**。  
  https://hub.evenrealities.com/docs/build/display
- **`\n` 是硬换行**。  
  https://hub.evenrealities.com/docs/build/display
- 超出高度且 `isEventCapture: 1`：固件内部滚动；到顶/底发 `SCROLL_TOP_EVENT` / `SCROLL_BOTTOM_EVENT`。  
  https://hub.evenrealities.com/docs/build/display  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/display.md
- 无 `isEventCapture`：notes 写溢出被 **clip**。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/display.md
- 程序化设置/读取 scroll offset：**文档未说明**（官方明确没有 programmatic scroll position）。  
  https://hub.evenrealities.com/docs/build/device-apis
- 除 `\n` 外的「手动折行控制」：**文档未说明**。居中=自己垫空格。  
  https://hub.evenrealities.com/docs/build/display
- 满屏大约 400–500 字；Design Guidelines 建议按约 400–500 分页。  
  https://hub.evenrealities.com/docs/build/display  
  https://hub.evenrealities.com/docs/build/design-guidelines
- notes：末尾多余 `\n` 会多一行从而出现 scrollbar。  
  https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/display.md

官方**没有**写「每行 28 字 / 最多 10 行」——那是本项目真机量出来的，不是 SDK 文档。

---

## 5. 各 API 字符上限

| 方法 | 上限 | 来源 |
| --- | --- | --- |
| `createStartUpPageContainer` | 1,000 | https://hub.evenrealities.com/docs/build/display |
| `rebuildPageContainer` | 1,000 | 同上 |
| `textContainerUpgrade` | 2,000 | 同上 |

列表项另有每项 64 字、最多 20 项。  
https://hub.evenrealities.com/docs/build/display

---

## 6. 屏幕 / 字体 / 字号

- 每眼 **576×288**；原点左上；**4-bit 绿灰阶（16 级）**。  
  https://hub.evenrealities.com/docs/build/display  
  https://github.com/even-realities/evenhub-templates
- 单一 LVGL 固件字体；**不能选字体、不能改字号**；非等宽；缺字 silently drop；无 emoji。  
  https://hub.evenrealities.com/docs/build/display  
  https://hub.evenrealities.com/docs/reference/faq  
  https://hub.evenrealities.com/docs/build/device-apis
- `textColor` 只调亮度 0–4。  
  https://hub.evenrealities.com/docs/build/display

---

## 7. 音频 API（含 speakerRole）

官方 Device APIs + SDK 0.0.15 README。  
https://hub.evenrealities.com/docs/build/device-apis  
https://www.npmjs.com/package/@evenrealities/even_hub_sdk

```ts
await bridge.audioControl(true, AudioInputSource.Glasses) // 默认眼镜四麦；须先 createStartUpPageContainer
await bridge.audioControl(true, AudioInputSource.Phone)   // 手机麦，无启动页要求
await bridge.audioControl(false)

bridge.onEvenHubEvent(event => {
  const audio = event.audioEvent
  // audio.source: AudioInputSource.Glasses | Phone
  // audio.audioPcm: Uint8Array — PCM 16 kHz, s16le, mono
  // audio.direction: int16 | null   // SDK 0.0.14+
  // audio.speakerRole: AudioSpeakerRole.Self | Other | Unknown
})
```

| 字段 | 文档含义 |
| --- | --- |
| `audioPcm` | PCM 16 kHz，signed 16-bit LE，mono。`Uint8Array`。 |
| `source` | 这次 buffer 来自眼镜麦还是手机麦。 |
| `direction` | 与处理后 PCM 同帧的原始 int16 方向标签；SDK **不换算单位**。手机麦和旧 Host：`null`。 |
| `speakerRole` | **`Self` / `Other` / `Unknown`。是 App 音频算法结果，不是固件身份结论。** 手机麦/旧 Host → `Unknown`。 |

**能否区分佩戴者 vs 对方：** 文档说算法会标 `Self`（「App 算法将这一帧判定为本人说话」）和 `Other`。强调：这是 **App 侧算法**，不是固件保证的说话人 ID。  
https://www.npmjs.com/package/@evenrealities/even_hub_sdk

权限：`g2-microphone` / `phone-microphone`。  
https://hub.evenrealities.com/docs/build/device-apis

notes 的音频段较旧（只写了 PCM，**没写** `speakerRole` / `direction`）。  
https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/device-apis.md

模板 ASR：眼镜 PCM 16 kHz s16le mono；`CLICK_EVENT=0` 在 protobuf 里常被省略，须在 envelope 内把 `undefined` 当 click。  
https://github.com/even-realities/evenhub-templates

---

## 8. 后台 / 锁屏

官方 Background & Lifecycle + FAQ：  
https://hub.evenrealities.com/docs/build/background-lifecycle  
https://hub.evenrealities.com/docs/reference/faq

| | |
| --- | --- |
| iOS WKWebView | 进后台/锁屏后 WebView **继续跑**；内存 JS 还在。 |
| Android Chromium | **可能被挂起**；超阈值进程回收，内存态没了。 |
| `localStorage` | 官方：一定还在（杀进程、更新也在；卸装才清）。 |
| 打开的 WebSocket | iOS 通常还在；**Android 挂起通常断**。 |
| 后台再发网络 | FAQ：**不能**。 |
| 麦 / 连续定位 | 挂起则停，回前台要重开。 |

notes 另写：`.ehpk` WebView 里浏览器 `localStorage` **重启后会被清**，跨会话要用 `bridge.setLocalStorage` / `getLocalStorage`。与官方 FAQ 不完全一致；打包路径以 notes 这条为社区警告。  
https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/device-apis.md  
https://hub.evenrealities.com/docs/reference/faq

---

## 9. 已测调用开销（even-g2-notes）

来源写明：一副 G2，固件 2.2.7.14，Even App 2.2.7，SDK 0.0.13，约 40 次发送。绝对数字是一个数据点；形状是「每次宿主调用固定成本大、payload 几乎无所谓」。  
https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/performance.md

| 调用 | 测得 |
| --- | --- |
| `updateImageRawData` | **~104 ms 固定** + ~3.9 ms/KB gray4 |
| `rebuildPageContainer` | **~165 ms**，与容器数无关 |
| `textContainerUpgrade` | **~83 ms**/次 |
| `createStartUpPageContainer` | ~100–135 ms |
| 失败后再 create | 拒 `invalid`，阻塞 **~2.1 s** |

Image 示例：58×58 / 1682 B ≈110 ms；272×130 / 17680 B ≈173 ms。  
`getAppLocation` 真机约 **~3 s**（不要挡第一帧）。  
官方 Simulator / Device APIs **没有**对等的耗时表。  
https://hub.evenrealities.com/docs/test/simulator

---

## 10. 安装并跑模拟器（官方 evenhub-simulator + even-dev）

没有名为 `even-dev` 的官方 npm 包。官方模拟器是 **`@evenrealities/evenhub-simulator`**（Node+LVGL 窗口，**不是**硬件仿真）。even-dev 是社区启动器，内部仍调这个官方二进制。

**官方（不用戴眼镜）**

```bash
npm install -g @evenrealities/evenhub-cli @evenrealities/evenhub-simulator
# 或项目 devDependency 后：
npm run dev
npx evenhub-simulator http://localhost:5173
# 模板自带：
npm run simulate
```

https://hub.evenrealities.com/docs/test/simulator  
https://hub.evenrealities.com/docs/get-started/quickstart/first-app  
https://github.com/even-realities/evenhub-templates  
https://hub.evenrealities.com/docs/get-started/quickstart/install-tools

无头：`evenhub-simulator http://localhost:5173 --automation-port 9898`，`GET /api/ping`、`/api/screenshot/glasses`、`POST /api/input`。  
https://hub.evenrealities.com/docs/test/simulator

**社区 even-dev**

```bash
git clone https://github.com/BxNxM/even-dev.git
cd even-dev
npm install
./start-even.sh                 # 交互选 app
./start-even.sh timer           # 或 APP_NAME=timer
APP_PATH=/path/to/glasses/app ./start-even.sh
AUDIO_DEVICE="<exact-id>" ./start-even.sh stt
npx @evenrealities/evenhub-simulator@latest -b default --list-audio-input-devices
```

需要：Node、npm、curl、**已装 Even Hub Simulator**。  
https://github.com/BxNxM/even-dev  
https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/simulator.md

官方 caveat：渲染非像素级一致；无真实 BLE/权限/后台。notes 另写：模拟器 0.7.1 拒 >4 容器、image 大于 200×100（真机可以）。  
https://hub.evenrealities.com/docs/test/simulator  
https://github.com/nickustinov/even-g2-notes/blob/227c866718fbb68907ef723592c8a4d90630e823/docs/simulator.md

---

## 11. speakerRole 真机分布（跑通后填）

文档：`Self` / `Other` / `Unknown` 是 **App 音频算法**，不是固件身份。手机麦 / 旧 Host → `Unknown`。  
https://www.npmjs.com/package/@evenrealities/even_hub_sdk  
https://hub.evenrealities.com/docs/build/device-apis

本表在戴镜跑 `server/live.py` 后按 `live_*.jsonl` 的 `speakerRole` 统计填写。**不要编造。**

| 值 | 帧数 / 段数 | 占比 | 何时出现（观察） |
| --- | --- | --- | --- |
| `self` / Self |  |  | 跑通后填 |
| `other` / Other |  |  | 跑通后填 |
| `unknown` / Unknown |  |  | 跑通后填 |
| 缺字段 / null |  |  | 跑通后填 |

`direction`（int16 或 null）同场记下，单位文档未换算。
