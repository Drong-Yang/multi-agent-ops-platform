/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useDispatch } from 'react-redux';
import { isMobile } from '@/utils/utils';
import InvokeButton from '@/pages/MainPage/MainArea/Antechamber/InvokeButton';
import { useJoin, useRTC, useScene } from '@/lib/useCommon';
import AIChangeCard from '@/components/AiChangeCard';
import { updateFullScreen, updateShowSubtitle } from '@/store/slices/room';
import style from './index.module.less';

function Antechamber() {
  const dispatch = useDispatch();
  const [joining, dispatchJoin] = useJoin();
  const { isScreenMode, isAvatarScene } = useScene();
  const { AppId, RoomId, UserId, Token } = useRTC();
  const ready = Boolean(AppId && RoomId && UserId && Token);

  const handleJoinRoom = () => {
    if (!ready || joining) return;
    dispatch(updateFullScreen({ isFullScreen: !isMobile() && !isScreenMode && !isAvatarScene }));
    dispatch(updateShowSubtitle({ isShowSubtitle: !isAvatarScene }));
    dispatchJoin();
  };

  return (
    <div className={`${style.wrapper} ${isMobile() ? style.mobile : ''}`}>
      <AIChangeCard />
      <InvokeButton
        onClick={handleJoinRoom}
        disabled={!ready}
        loading={joining}
        className={style['invoke-btn']}
      />
    </div>
  );
}

export default Antechamber;
