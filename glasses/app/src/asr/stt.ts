// Phase 3 / M1: no local STT. PCM goes to server/live.py over WebSocket.
//
// Official template still calls sendPcm from onEvenHubEvent. We attach
// speakerRole + direction on every chunk (required uplink fields).

export interface SttSnapshot {
  finalText: string
  interimText: string
  finished: boolean
}

export interface SttClient {
  sendPcm(chunk: Uint8Array): void
  close(): void
}

export interface UplinkMeta {
  speakerRole: string
  direction: number | null
}

export interface LiveUplink {
  sendPcm(chunk: Uint8Array, meta: UplinkMeta): boolean
  close(): void
  get sentChunks(): number
}

function u8ToB64(u8: Uint8Array): string {
  let s = ''
  const step = 0x8000
  for (let i = 0; i < u8.length; i += step) {
    s += String.fromCharCode(...u8.subarray(i, i + step))
  }
  return btoa(s)
}

export function roleLabel(raw: unknown): string {
  const s = String(raw ?? '').trim().toLowerCase()
  if (s === 'self') return 'self'
  if (s === 'other') return 'other'
  return 'unknown'
}

export function startLiveUplink(
  sendJson: (obj: Record<string, unknown>) => boolean,
  onError?: (err: unknown) => void,
): LiveUplink {
  let sent = 0
  let closed = false
  return {
    sendPcm(chunk: Uint8Array, meta: UplinkMeta): boolean {
      if (closed || !chunk.length) return false
      try {
        const ok = sendJson({
          type: 'pcm',
          pcm_b64: u8ToB64(chunk),
          speakerRole: roleLabel(meta.speakerRole),
          direction: meta.direction,
        })
        if (ok) sent += 1
        return ok
      } catch (err) {
        onError?.(err)
        return false
      }
    },
    close() {
      closed = true
    },
    get sentChunks() {
      return sent
    },
  }
}

export function startSttStream(
  _apiKey: string,
  _onSnapshot: (snap: SttSnapshot) => void,
  _onError?: (err: unknown) => void,
): SttClient {
  throw new Error(
    'Local STT is not used. Phase 3 sends audioPcm over WebSocket via startLiveUplink.',
  )
}
