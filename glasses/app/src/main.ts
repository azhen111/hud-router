import { OsEventTypeList } from '@evenrealities/even_hub_sdk'
import { initPage, showText, clearDisplay, getBridge } from './display'
import {
  createWsClient,
  defaultWsUrl,
  loadSavedUrl,
  persistUrl,
} from './ws'
import {
  mountUi,
  bindConnect,
  setButtons,
  setStatus,
  setConnState,
  setRecvCount,
  setLatestRaw,
  setLatestText,
  showError,
  wsUrlInput,
} from './ui'

// Template STT stub is unused this milestone (display path only).
// Keep src/asr/stt.ts in tree for the official ASR scaffold.

mountUi()

const urlInput = wsUrlInput()
urlInput.value = loadSavedUrl()
urlInput.placeholder = defaultWsUrl()
urlInput.addEventListener('change', () => persistUrl(urlInput.value))

let recvCount = 0

const ws = createWsClient({
  onState(state) {
    setConnState(state)
    if (state === 'connected') {
      setStatus('ok', 'WS connected · tap to disconnect · double-tap to exit')
      setButtons(true)
    } else if (state === 'disconnected') {
      setStatus('paused', 'WS idle · tap to connect · double-tap to exit')
      setButtons(false)
    } else if (state.startsWith('reconnect') || state === 'connecting') {
      setStatus('connecting', state)
      setButtons(true)
    } else if (state === 'invalid url' || state === 'error') {
      setStatus('error', state)
      setButtons(false)
    }
  },
  onMessage(raw) {
    recvCount += 1
    setRecvCount(recvCount)
    setLatestRaw(raw)
    void handlePush(raw)
  },
  onError(err) {
    showError(err)
  },
})

function doConnect() {
  let v = urlInput.value.trim()
  if (!v) {
    v = defaultWsUrl()
    urlInput.value = v
  }
  try {
    ws.connect(v)
  } catch (err) {
    showError(err)
    setConnState('invalid url')
    setStatus('error', 'invalid url')
    setButtons(false)
  }
}

function doDisconnect() {
  ws.disconnect()
}

bindConnect(doConnect, doDisconnect)

async function handlePush(raw: string) {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (err) {
    showError(new Error('JSON 解析失败: ' + String(err) + '\n原始: ' + raw))
    return
  }
  if (parsed && typeof parsed === 'object') {
    const obj = parsed as { text?: unknown; clear?: unknown }
    if (obj.clear === true) {
      try {
        await clearDisplay()
        setLatestText('(clear)')
      } catch (err) {
        showError(err)
      }
      return
    }
    if (typeof obj.text === 'string') {
      try {
        await showText(obj.text)
        setLatestText(obj.text)
      } catch (err) {
        showError(err)
      }
      return
    }
  }
  showError(new Error('期望 {"text":"..."} 或 {"clear":true}，收到: ' + raw))
}

setStatus('connecting', '等待 EvenAppBridge…')

const { result } = await initPage()
if (result !== 0 && result !== 'success') {
  setStatus('error', 'createStartUpPageContainer failed: ' + String(result))
  showError(new Error('createStartUpPageContainer failed: ' + String(result)))
} else {
  setStatus('paused', '启动页已创建 · tap to connect · double-tap to exit')
}

function eventTypeOf(envelope?: { eventType?: OsEventTypeList }): OsEventTypeList | null {
  if (!envelope) return null
  return envelope.eventType ?? OsEventTypeList.CLICK_EVENT
}

let cleanedUp = false
function cleanup() {
  if (cleanedUp) return
  cleanedUp = true
  ws.disconnect()
  unsubscribe()
}

const bridge = getBridge()
if (!bridge) {
  throw new Error('bridge missing after initPage')
}

const unsubscribe = bridge.onEvenHubEvent(event => {
  const sysType = eventTypeOf(event.sysEvent)
  const textType = eventTypeOf(event.textEvent)

  if (sysType === OsEventTypeList.DOUBLE_CLICK_EVENT || textType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
    void bridge.shutDownPageContainer(1)
    return
  }

  if (sysType === OsEventTypeList.CLICK_EVENT || textType === OsEventTypeList.CLICK_EVENT) {
    if (ws.wantConnected) doDisconnect()
    else doConnect()
    return
  }

  if (sysType === OsEventTypeList.SYSTEM_EXIT_EVENT || sysType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
    cleanup()
  }
})

window.addEventListener('beforeunload', cleanup)
window.addEventListener('error', ev => showError(ev.error || ev.message || ev))
window.addEventListener('unhandledrejection', ev => showError(ev.reason || ev))
