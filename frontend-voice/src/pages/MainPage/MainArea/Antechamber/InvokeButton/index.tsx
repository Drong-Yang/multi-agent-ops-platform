/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import Loading from './loading';
import style from './index.module.less';
import CallButtonSVG from '@/assets/img/CallWrapper.svg';
import PhoneSVG from '@/assets/img/Phone.svg';

interface IInvokeButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  loading?: boolean;
}

function InvokeButton(props: IInvokeButtonProps) {
  const { loading, disabled, className, ...rest } = props;

  return (
    <button type="button" className={`${style.wrapper} ${className}`} disabled={disabled || loading} {...rest}>
      <div className={style.btn}>
        <img src={CallButtonSVG} alt="" />
        {loading ? (
          <Loading className={style.icon} />
        ) : (
          <img src={PhoneSVG} className={style.icon} alt="" />
        )}
      </div>
      <div className={style.text}>{loading ? '连接中' : '通话'}</div>
    </button>
  );
}

export default InvokeButton;
