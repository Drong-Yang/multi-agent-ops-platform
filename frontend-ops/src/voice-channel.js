const voicePage = 'http://127.0.0.1:5179/'
const unavailableNotices = {
  mock_mode: '当前为演示模式。请运行 stop.cmd 停止当前服务，再用 start-voice.cmd 启动实时语音。',
  configuration: '实时语音配置尚未就绪。请按项目语音验收指南完成预检，再用 start-voice.cmd 启动。',
  voice_ui_unavailable: '语音页面尚未就绪。请构建语音前端并用 start-voice.cmd 启动后重试。'
}

export const isVoiceChannelReady = availability => availability?.ready === true && availability.reason === 'ready'

export function voiceChannelNotice(availability) {
  if (isVoiceChannelReady(availability)) return '语音页面已就绪，打开后可拨号。'
  return unavailableNotices[availability?.reason] || '语音状态暂未确认，可以重新检查。'
}

export async function openVoiceChannel(availability, { openWindow, handoff }) {
  if (!isVoiceChannelReady(availability)) return false
  // Open while the user gesture is active. Navigate only after the API checks readiness again.
  const tab = openWindow('about:blank', '_blank')
  if (!tab) throw new Error('浏览器拦截了弹窗，请允许工作台打开语音页面。')
  try {
    tab.opener = null
    const { code } = await handoff()
    if (tab.closed) return false
    if (typeof code !== 'string' || !code) throw new Error('未取得语音凭据，请重试。')
    tab.location.replace(`${voicePage}#handoff=${encodeURIComponent(code)}`)
    return true
  } catch (error) {
    tab.close()
    throw error
  }
}
