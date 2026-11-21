/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import VERTC from '@volcengine/rtc';
import { Typography } from '@arco-design/web-react';
import { useSelector } from 'react-redux';
import { RootState } from '@/store';
import Operation from './components/Operation';
import CameraArea from '../MainArea/Room/CameraArea';
import { isMobile } from '@/utils/utils';
import { useScene } from '@/lib/useCommon';
import styles from './index.module.less';

function Menu() {
  const room = useSelector((state: RootState) => state.room);
  const isJoined = room?.isJoined;
  const { isVision, name } = useScene();
  const requestId = sessionStorage.getItem('RequestID');

  return (
    <div className={styles.wrapper}>
      {isJoined && isMobile() && isVision ? (
        <div className={styles['mobile-camera-wrapper']}>
          <CameraArea className={styles['mobile-camera']} />
        </div>
      ) : null}
      <div className={`${styles.box} ${styles.info}`}>
        <div className={styles.title}>{name}</div>
        <div className={styles.desc}>演示商品与售后规则</div>
        <div className={styles.desc}>{isJoined ? '说话即可打断播报' : '试问：SC-PB27 多重？'}</div>
      </div>
      {isJoined && <Operation />}
      {isJoined && (
        <div className={`${styles.box} ${styles.info}`} aria-label="语音输入状态">
          <div className={styles.title}>麦克风：{room.localUser.publishAudio ? '已开启' : '已静音'}</div>
          <meter min={0} max={255} value={room.localUser.audioPropertiesInfo?.linearVolume ?? 0} aria-label="麦克风输入音量" />
          {room.voiceError && <div role="alert" style={{ color: '#b42318' }}>{room.voiceError}</div>}
        </div>
      )}
      <details className={`${styles.box} ${styles.info} ${styles.diagnostics}`}>
        <summary className={styles.title}>通话诊断</summary>
        <div className={styles.desc}>RTC SDK {VERTC.getSdkVersion()}</div>
        {isJoined && (
          <>
            <div className={styles.desc}>音频上行：{room.localUser.audioStats?.sendKBitrate?.toFixed(1) ?? '—'} kbps</div>
            <div className={styles.desc}>
              房间 ID <Typography.Paragraph copyable ellipsis={{ rows: 1 }} className={styles.value}>{room.roomId || '—'}</Typography.Paragraph>
            </div>
            {requestId && <div className={styles.desc}>
              请求 ID <Typography.Paragraph copyable ellipsis={{ rows: 1 }} className={styles.value}>{requestId}</Typography.Paragraph>
            </div>}
          </>
        )}
      </details>
    </div>
  );
}

export default Menu;
