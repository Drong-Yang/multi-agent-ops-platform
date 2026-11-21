/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

export const Disclaimer = 'https://www.volcengine.com/docs/6348/68916';
export const ReversoContext = 'https://www.volcengine.com/docs/6348/68918';
export const UserAgreement = 'https://www.volcengine.com/docs/6348/128955';

/**
 * @note 请求的 API Proxy Server(对应此 Demo 中包含的 Node server) 地址。
 *       动态使用当前页面的主机名，支持局域网访问
 */
// Fusion backend gateway; deliberately never uses the old standalone port 3001.
export const FUSION_API_BASE = (process.env.REACT_APP_FUSION_API_BASE || 'http://127.0.0.1:8010').replace(/\/$/, '');
export const AIGC_PROXY_HOST = `${FUSION_API_BASE}/api/v1/voice/legacy`;

export interface IScene {
  icon: string;
  name: string;
  questions: string[];
  agentConfig: Record<string, any>;
  llmConfig: Record<string, any>;
  asrConfig: Record<string, any>;
  ttsConfig: Record<string, any>;
}
