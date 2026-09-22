type Status = 'connecting' | 'listening' | 'paused' | 'error' | 'ok'

let statusEl: HTMLDivElement
let urlEl: HTMLInputElement
let btnConnect: HTMLButtonElement
let btnDisconnect: HTMLButtonElement
let connEl: HTMLDivElement
let recvEl: HTMLDivElement
let latestEl: HTMLDivElement
let errorEl: HTMLDivElement
let latestTextEl: HTMLDivElement

export function mountUi() {
  const app = document.querySelector<HTMLDivElement>('#app')!
  app.innerHTML = `
    <main class="panel">
      <header>
        <h1>HUD Display</h1>
        <div id="status" class="status status-connecting">Connecting…</div>
      </header>
      <label class="lbl" for="wsUrl">WebSocket URL（ws:// 或 wss://）</label>
      <input id="wsUrl" type="text" autocomplete="off" spellcheck="false" />
      <div class="row">
        <button id="btnConnect" type="button">Connect</button>
        <button id="btnDisconnect" type="button" class="danger" disabled>Disconnect</button>
      </div>
      <section class="kv">
        <div class="k">连接</div><div id="connState">disconnected</div>
        <div class="k">已收消息</div><div id="recvCount">0</div>
        <div class="k">最近一条</div><div id="latestMsg" class="mono">（无）</div>
        <div class="k">镜片</div><div id="latestText" class="mono">（无）</div>
      </section>
      <section class="errbox">
        <h2>最近错误（全文）</h2>
        <div id="errorText" class="mono">（无）</div>
      </section>
      <footer>Tap temple to connect/disconnect · double-tap to exit.</footer>
    </main>
  `
  statusEl = app.querySelector('#status')!
  urlEl = app.querySelector('#wsUrl')!
  btnConnect = app.querySelector('#btnConnect')!
  btnDisconnect = app.querySelector('#btnDisconnect')!
  connEl = app.querySelector('#connState')!
  recvEl = app.querySelector('#recvCount')!
  latestEl = app.querySelector('#latestMsg')!
  latestTextEl = app.querySelector('#latestText')!
  errorEl = app.querySelector('#errorText')!
  injectStyles()
}

export function wsUrlInput(): HTMLInputElement {
  return urlEl
}

export function bindConnect(onConnect: () => void, onDisconnect: () => void) {
  btnConnect.addEventListener('click', onConnect)
  btnDisconnect.addEventListener('click', onDisconnect)
}

export function setButtons(connectedWanted: boolean) {
  btnConnect.disabled = connectedWanted
  btnDisconnect.disabled = !connectedWanted
}

export function setStatus(kind: Status, text: string) {
  if (!statusEl) return
  statusEl.className = `status status-${kind}`
  statusEl.textContent = text
}

export function setConnState(text: string) {
  connEl.textContent = text
}

export function setRecvCount(n: number) {
  recvEl.textContent = String(n)
}

export function setLatestRaw(text: string) {
  latestEl.textContent = text
}

export function setLatestText(text: string) {
  latestTextEl.textContent = text
}

export function showError(err: unknown) {
  errorEl.textContent = formatError(err)
  console.error(errorEl.textContent)
}

export function formatError(err: unknown): string {
  if (err == null) return '（无）'
  if (typeof err === 'string') return err
  const e = err as { name?: string; message?: string; stack?: string }
  const name = e.name || 'Error'
  const msg = e.message || String(err)
  const stack = e.stack ? '\n' + e.stack : ''
  return name + ': ' + msg + stack
}

function injectStyles() {
  const css = `
    :root { color-scheme: dark; }
    html, body { margin: 0; height: 100%; background: #232323; color: #E5E5E5;
      font: 16px/1.4 -apple-system, BlinkMacSystemFont, 'Helvetica Neue', system-ui, sans-serif;
      touch-action: manipulation; -webkit-text-size-adjust: 100%;
      overscroll-behavior: none; }
    #app { display: flex; height: 100%; }
    .panel { display: flex; flex-direction: column; gap: 12px;
      width: 100%; max-width: 640px; margin: 0 auto; padding: 24px; box-sizing: border-box; }
    header { display: flex; align-items: center; justify-content: space-between; }
    h1 { font-size: 18px; font-weight: 600; margin: 0; letter-spacing: 0.02em; }
    .lbl { font-size: 12px; color: #7B7B7B; }
    input[type="text"] { width: 100%; box-sizing: border-box; padding: 10px;
      border: 1px solid #3E3E3E; border-radius: 8px; background: #2E2E2E; color: #E5E5E5; font-size: 15px; }
    input[type="text"]::placeholder { color: #666; opacity: 1; }
    .row { display: flex; gap: 8px; }
    button { flex: 1; padding: 10px; border: 0; border-radius: 8px; background: #2a4a7a; color: #fff; font-size: 15px; }
    button:disabled { opacity: 0.45; }
    button.danger { background: #6a2a2a; }
    .status { font-size: 12px; padding: 4px 10px; border-radius: 999px;
      border: 1px solid transparent; letter-spacing: 0.04em; text-transform: uppercase; }
    .status-connecting { color: #A7A7A7; border-color: #3E3E3E; }
    .status-listening, .status-ok { color: #3CFA44; border-color: #3CFA44; background: rgba(60,250,68,0.08); }
    .status-paused { color: #E5E5E5; border-color: #7B7B7B; background: rgba(229,229,229,0.06); }
    .status-error { color: #FF453A; border-color: #FF453A; background: rgba(255,69,58,0.08); }
    .kv { display: grid; grid-template-columns: 7em 1fr; gap: 6px 8px; font-size: 14px;
      background: #2E2E2E; border: 1px solid #3E3E3E; border-radius: 12px; padding: 16px; }
    .k { color: #7B7B7B; }
    .mono { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; }
    .errbox { background: #2E2E2E; border: 1px solid #3E3E3E; border-radius: 12px; padding: 16px; }
    .errbox h2 { font-size: 12px; color: #7B7B7B; margin: 0 0 8px; }
    #errorText { color: #FF453A; min-height: 3em; }
    footer { font-size: 12px; color: #7B7B7B; text-align: center; }
  `
  const style = document.createElement('style')
  style.textContent = css
  document.head.appendChild(style)
}
