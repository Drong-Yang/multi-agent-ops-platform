export const VOICE_TOKEN_KEY: string;
export const FUSION_API_BASE: string;
export function voiceHeaders(storage: Pick<Storage, 'getItem'>): { Authorization: string };
export function initializeVoiceHandoff(options: {
  location: { hash: string; pathname?: string; search?: string };
  history: Pick<History, 'replaceState'>;
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
  fetchImpl: typeof fetch;
  apiBase?: string;
}): Promise<boolean>;

declare const fusionVoice: {
  VOICE_TOKEN_KEY: typeof VOICE_TOKEN_KEY;
  FUSION_API_BASE: typeof FUSION_API_BASE;
  voiceHeaders: typeof voiceHeaders;
  initializeVoiceHandoff: typeof initializeVoiceHandoff;
};
export default fusionVoice;
