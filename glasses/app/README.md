# glasses/app — HUD 显示客户端

从官方 [evenhub-templates/asr](https://github.com/even-realities/evenhub-templates) 脚手架改来：保留 Vite / `app.json` / 双击退出（`shutDownPageContainer(1)`）。STT stub 仍在 `src/asr/stt.ts`，**本里程碑不开麦**。

```bash
npm install
npm run dev
npx evenhub-simulator http://localhost:5173
npx evenhub qr --url http://<LAN>:5173
```

Companion WebView：填 `ws://<LAN>:8766`，Connect。服务器推 `{"text":"..."}` → `textContainerUpgrade`；`{"clear":true}` → `clearDisplay()`（默认 `・`）。
