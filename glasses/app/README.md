# glasses/app — HUD Live 客户端

官方 [evenhub-templates/asr](https://github.com/even-realities/evenhub-templates) 脚手架：Vite / `app.json` / 双击 `shutDownPageContainer(1)`。

Phase 3 / M1：`src/asr/stt.ts` 把 `audioPcm` + `speakerRole` + `direction` 经 **同一条** WebSocket 发给 `server/live.py`（不做本地 STT）。下行 `{"text":"..."}` → `textContainerUpgrade`。

```bash
npm install
npm run dev
npx evenhub-simulator http://localhost:5173
npx evenhub qr --url http://<LAN>:5173
```

1. 电脑先跑 `python server/live.py`（需 `DEEPGRAM_API_KEY` + `OPENAI_API_KEY` + `ROUTER_MODEL`）。
2. Companion WebView：填 `ws://<LAN>:8766`，Connect。
3. 镜腿单击 = 暂停/恢复采集；双击退出。
4. 错误和状态只画在手机页（不只 console）。

`app.json` `network.whitelist` 必须包含 live 服务器的完整 origin（无通配符）。开发 QR 是否跳过白名单：**文档未说明**。
