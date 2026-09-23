import {
  waitForEvenAppBridge,
  TextContainerProperty,
  CreateStartUpPageContainer,
  TextContainerUpgrade,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk'

export const MAIN_ID = 1
export const MAIN_NAME = 'main'
export const DEFAULT_CLEAR_PLACEHOLDER = '・'
export const LS_CLEAR = 'hud-router-clear-placeholder'

let bridge: EvenAppBridge | null = null
let pageCreateCalled = false

export function getBridge(): EvenAppBridge | null {
  return bridge
}

export function clearPlaceholder(): string {
  try {
    const saved = (localStorage.getItem(LS_CLEAR) || '').trim()
    if (saved) return saved
  } catch {
    /* ignore */
  }
  return DEFAULT_CLEAR_PLACEHOLDER
}

/** 最大画布一块文本容器。之后只走 textContainerUpgrade。 */
export async function initPage(): Promise<{ bridge: EvenAppBridge; result: unknown }> {
  bridge = await waitForEvenAppBridge()
  const text = new TextContainerProperty({
    xPosition: 0,
    yPosition: 0,
    width: 576,
    height: 288,
    borderWidth: 0,
    borderColor: 5,
    paddingLength: 4,
    containerID: MAIN_ID,
    containerName: MAIN_NAME,
    content: '等待服务器…',
    textColor: 4,
    isEventCapture: 1,
  })
  const result = await bridge.createStartUpPageContainer(
    new CreateStartUpPageContainer({
      containerTotalNum: 1,
      textObject: [text],
    }),
  )
  pageCreateCalled = true
  return { bridge, result }
}

export async function showText(content: string): Promise<boolean> {
  if (!bridge) throw new Error('EvenAppBridge 未就绪')
  if (!pageCreateCalled) {
    throw new Error('createStartUpPageContainer 尚未调用')
  }
  const ok = await bridge.textContainerUpgrade(
    new TextContainerUpgrade({
      containerID: MAIN_ID,
      containerName: MAIN_NAME,
      content,
    }),
  )
  if (ok === false) {
    throw new Error('textContainerUpgrade 返回 false')
  }
  return true
}

/**
 * 文档没有 hide/clear API；空串 "" 项目经验不能清屏。
 * 用可配置单字占位符走 textContainerUpgrade（默认 「・」）。
 */
export async function clearDisplay(placeholder?: string): Promise<boolean> {
  const ch = (placeholder && placeholder.length > 0 ? placeholder : clearPlaceholder())
  if (ch === '') {
    return showText(DEFAULT_CLEAR_PLACEHOLDER)
  }
  return showText(ch)
}
