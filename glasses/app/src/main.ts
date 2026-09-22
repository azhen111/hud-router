import {
  AudioInputSource,
  OsEventTypeList,
} from '@evenrealities/even_hub_sdk'
import { initPage, showText, clearDisplay, getBridge } from './display'
import {
  createWsClient,
  defaultWsUrl,
  loadSavedUrl,
  persistUrl,
} from './ws'
import { startLiveUplink, roleLabel } from './asr/stt'
import {
  mountUi,
  bindConnect,
  setButtons,
  setStatus,
  setConnState,
  setRecvCount,
  setLatestRaw,
  setLatestText,
  setCaptureState,
  setPcmCount,
  setLastMeta,
  showError,
  wsUrlInput,
} from './ui'

mountUi()

const urlInput = wsUrlInput()
urlInput.value = loadSavedUrl()
urlInput.placeholder = defaultWsUrl()
urlInput.addEventListener('change', () => persistUrl(urlInput.value))

let recvCount = 0
let captureOn = false
let pageReady = false

const uplink = startLiveUplink(
  obj => ws.send(JSON.stringify(obj)),
  err => showError(err),
)

const ws = createWsClient({
  onState(state) {
    setConnState(state)
    if (state === 'connected') {
      setStatus('ok', 'WS connected · tap temple to pause capture · double-tap to exit')
      setButtons(true)
      void startCapture('WS connected')
    } else if (state === 'disconnected') {
      void stopCapture()
      setStatus('paused', 'WS idle · Connect 后开麦 · double-tap to exit')
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
  void stopCapture()
  ws.disconnect()
}

bindConnect(doConnect, doDisconnect)

async function startCapture(reason: string) {
  const bridge = getBridge()
  if (!bridge || !pageReady || !ws.ready) {
    setCaptureState('paused')
    return
  }
  try {
    const ok = await bridge.audioControl(true, AudioInputSource.Glasses)
    if (ok === false) {
      captureOn = false
      setCaptureState('failed')
      setStatus('error', 'audioControl(true) 返回 false（须先有启动页）')
      showError(new Error('audioControl(true) 返回 false · ' + reason))
      return
    }
    captureOn = true
    setCaptureState('live')
    setStatus('listening', 'Microphone live · tap to pause · double-tap to exit')
  } catch (err) {
    captureOn = false
    setCaptureState('error')
    showError(err)
  }
}

async function stopCapture() {
  captureOn = false
  setCaptureState('paused')
  const bridge = getBridge()
  if (!bridge) return
  try {
    await bridge.audioControl(false)
  } catch (err) {
    showError(err)
  }
}

async function toggleCapture() {
  if (!ws.ready) {
    showError(new Error('先点 Connect 连上 server/live.py，再点镜腿开麦'))
    setStatus('error', 'WS 未连接')
    return
  }
  if (captureOn) {
    await stopCapture()
    setStatus('paused', 'Paused · tap to resume · double-tap to exit')
  } else {
    await startCapture('temple tap')
  }
}

async function handlePush(raw: string) {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (err) {
    showError(new Error('JSON 解析失败: ' + String(err) + '\n原始: ' + raw))
    return
  }
  if (parsed && typeof parsed === 'object') {
    const obj = parsed as { text?: unknown; clear?: unknown; error?: unknown; status?: unknown }
    if (typeof obj.error === 'string') {
      showError(new Error(obj.error))
      setStatus('error', obj.error)
      return
    }
    if (typeof obj.status === 'string') {
      setStatus('ok', obj.status)
      return
    }
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
  showError(new Error('期望 {"text":"..."} / {"status":...} / {"error":...}，收到: ' + raw))
}

setStatus('connecting', '等待 EvenAppBridge…')

const { result } = await initPage()
if (result !== 0 && result !== 'success') {
  setStatus('error', 'createStartUpPageContainer failed: ' + String(result))
  showError(new Error('createStartUpPageContainer failed: ' + String(result)))
} else {
  pageReady = true
  setStatus('paused', '启动页已创建 · Connect 后开麦 · double-tap to exit')
}

function eventTypeOf(envelope?: { eventType?: OsEventTypeList }): OsEventTypeList | null {
  if (!envelope) return null
  return envelope.eventType ?? OsEventTypeList.CLICK_EVENT
}

let cleanedUp = false
function cleanup() {
  if (cleanedUp) return
  cleanedUp = true
  uplink.close()
  void stopCapture()
  ws.disconnect()
  unsubscribe()
}

const bridge = getBridge()
if (!bridge) {
  throw new Error('bridge missing after initPage')
}

const unsubscribe = bridge.onEvenHubEvent(event => {
  const audio = event.audioEvent
  if (audio?.audioPcm && audio.audioPcm.length) {
    const role = roleLabel(audio.speakerRole)
    const direction = audio.direction ?? null
    setLastMeta(role, direction)
    uplink.sendPcm(audio.audioPcm, { speakerRole: role, direction })
    setPcmCount(uplink.sentChunks)
  }

  const sysType = eventTypeOf(event.sysEvent)
  const textType = eventTypeOf(event.textEvent)

  if (sysType === OsEventTypeList.DOUBLE_CLICK_EVENT || textType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
    void bridge.shutDownPageContainer(1)
    return
  }

  if (sysType === OsEventTypeList.CLICK_EVENT || textType === OsEventTypeList.CLICK_EVENT) {
    void toggleCapture()
    return
  }

  if (sysType === OsEventTypeList.SYSTEM_EXIT_EVENT || sysType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
    cleanup()
  }
})

window.addEventListener('beforeunload', cleanup)
window.addEventListener('error', ev => showError(ev.error || ev.message || ev))
window.addEventListener('unhandledrejection', ev => showError(ev.reason || ev))
