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
let captureEl: HTMLDivElement
let pcmEl: HTMLDivElement
let roleEl: HTMLDivElement
let dirEl: HTMLDivElement
let listEl: HTMLDivElement

export function mountUi() {
  const app = document.querySelector<HTMLDivElement>('#app')!
  app.innerHTML = `
    <main class="panel">
      <header>
        <h1>HUD Live</h1>
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
        <div class="k">采集</div><div id="captureState">paused</div>
        <div class="k">已发 PCM</div><div id="pcmCount">0</div>
        <div class="k">speakerRole</div><div id="lastRole">（无）</div>
        <div class="k">direction</div><div id="lastDirection">（无）</div>
        <div class="k">已收消息</div><div id="recvCount">0</div>
        <div class="k">最近一条</div><div id="latestMsg" class="mono">（无）</div>
        <div class="k">镜片</div><div id="latestText" class="mono">（无）</div>
      </section>
      <section class="listbox">
        <h2>手机详解（会话后可回看）</h2>
        <div id="detailList" class="detaillist">（还没有详解）</div>
      </section>
      <section class="errbox">
        <h2>最近错误（全文）</h2>
        <div id="errorText" class="mono">（无）</div>
      </section>
      <footer>Temple tap = pause/resume capture · double-tap = exit · Connect 先连服务器。</footer>
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
  captureEl = app.querySelector('#captureState')!
  pcmEl = app.querySelector('#pcmCount')!
  roleEl = app.querySelector('#lastRole')!
  dirEl = app.querySelector('#lastDirection')!
  listEl = app.querySelector('#detailList')!
  renderDetailList(loadDetailItems())
  injectStyles()
}

export type DetailItem = {
  ts: string
  question: string
  detail: string
}

const LS_DETAILS = 'hud-router-detail-list'
const DETAIL_CAP = 50

export function loadDetailItems(): DetailItem[] {
  try {
    const raw = localStorage.getItem(LS_DETAILS) || '[]'
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (x): x is DetailItem =>
        !!x && typeof x.ts === 'string' && typeof x.detail === 'string',
    )
  } catch {
    return []
  }
}

export function appendDetail(question: string, detail: string, ts?: string): DetailItem[] {
  const item: DetailItem = {
    ts: ts || new Date().toISOString(),
    question: question || '',
    detail,
  }
  const next = [item, ...loadDetailItems()].slice(0, DETAIL_CAP)
  try {
    localStorage.setItem(LS_DETAILS, JSON.stringify(next))
  } catch {
    /* ignore quota */
  }
  renderDetailList(next)
  return next
}

export function renderDetailList(items: DetailItem[]): void {
  if (!listEl) return
  if (!items.length) {
    listEl.textContent = '（还没有详解）'
    return
  }
  listEl.innerHTML = items
    .map(it => {
      const q = escapeHtml(it.question || '（无原问）')
      const d = escapeHtml(it.detail)
      const t = escapeHtml(it.ts.replace('T', ' ').replace('Z', ''))
      return `<article class="ditem"><time>${t}</time><div class="q">${q}</div><div class="d">${d}</div></article>`
    })
    .join('')
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
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

export function setCaptureState(text: string) {
  captureEl.textContent = text
}

export function setPcmCount(n: number) {
  pcmEl.textContent = String(n)
}

export function setLastMeta(role: string, direction: number | null) {
  roleEl.textContent = role || '（无）'
  dirEl.textContent = direction === null || direction === undefined ? 'null' : String(direction)
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
    .errbox, .listbox { background: #2E2E2E; border: 1px solid #3E3E3E; border-radius: 12px; padding: 16px; }
    .errbox h2, .listbox h2 { font-size: 12px; color: #7B7B7B; margin: 0 0 8px; }
    .detaillist { max-height: 42vh; overflow: auto; display: flex; flex-direction: column; gap: 10px; }
    .ditem { border-top: 1px solid #3E3E3E; padding-top: 8px; }
    .ditem:first-child { border-top: 0; padding-top: 0; }
    .ditem time { font-size: 11px; color: #7B7B7B; }
    .ditem .q { font-size: 13px; color: #A7A7A7; margin: 2px 0 4px; white-space: pre-wrap; }
    .ditem .d { font-size: 14px; white-space: pre-wrap; word-break: break-word; }
    #errorText { color: #FF453A; min-height: 3em; }
    footer { font-size: 12px; color: #7B7B7B; text-align: center; }
  `
  const style = document.createElement('style')
  style.textContent = css
  document.head.appendChild(style)
}
