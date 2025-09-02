
# -*- coding: utf-8 -*-
import argparse, math, time, warnings
from collections import deque
from datetime import datetime

warnings.filterwarnings("ignore", category=RuntimeWarning, module="acconeer.exptool")

from acconeer.exptool.a111 import Client, IQServiceConfig
from acconeer.exptool.a111.algo.sleep_breathing import _processor as sb

def safe_float(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except:
        return None

def bpm_from_res(res):
    """优先 f_est，其次 f_dft_est；再兜底可能的 *_hz / *_bpm。"""
    if not isinstance(res, dict):
        return None
    for k in ("f_est","f_dft_est","breathing_rate_hz","freq_hz","f_hat","resp_rate_hz"):
        v = safe_float(res.get(k))
        if v is not None:
            return v * 60.0
    for k in ("breathing_rate_bpm","respiratory_rate_bpm","bpm"):
        v = safe_float(res.get(k))
        if v is not None:
            return v
    return None

def norm_init_progress(res):
    ip = safe_float(isinstance(res, dict) and res.get("init_progress"))
    if ip is None:
        return 1.0
    # 你的输出是 9,19,...99，看起来是百分比；>1 则归一化到 0-1
    return ip/100.0 if ip > 1.0 else ip

def main():
    ap = argparse.ArgumentParser("A111 IQ sleep-breathing (stable 1Hz logging)")
    ap.add_argument("--host", required=True)
    ap.add_argument("--r0", type=float, default=0.40)
    ap.add_argument("--r1", type=float, default=0.60)
    ap.add_argument("--rate", type=float, default=12.0)
    ap.add_argument("--n-dft", type=float, default=15.0)
    ap.add_argument("--f-low", type=float, default=0.08)
    ap.add_argument("--f-high", type=float, default=0.70)
    ap.add_argument("--snr-min", type=float, default=10.0)
    ap.add_argument("--hold-last-for", type=float, default=5.0, help="秒；短时失锁则保留上次值")
    args = ap.parse_args()

    client = Client(protocol="exploration", link="socket", host=args.host)

    cfg = IQServiceConfig()
    cfg.range_interval = [args.r0, args.r1]
    cfg.update_rate = args.rate
    cfg.hw_accelerated_average_samples = 10
    try: cfg.sensor = [1]
    except: pass
    try: cfg.profile = cfg.Profile.PROFILE_2
    except: pass

    sess = client.setup_session(cfg)
    client.start_session()

    pc = sb.ProcessingConfiguration()
    pc.n_dft = args.n_dft
    pc.t_freq_est = 0.2
    pc.D = 124
    pc.f_low, pc.f_high = args.f_low, args.f_high
    pc.lambda_p, pc.lambda_05 = 40.0, 1.0
    proc = sb.Processor(cfg, pc, sess)

    print("time_hms,unix_s,bpm,note")

    # 平滑：最近 N 个有效 BPM 的中位数
    smooth_window = 5
    q = deque(maxlen=smooth_window)

    last_good_bpm = None
    last_good_t = 0.0

    t0 = time.time()
    next_tick = math.floor(t0) + 1
    first = True

    try:
        while True:
            info, data = client.get_next()
            res = proc.process(data, info)

            if first and isinstance(res, dict):
                print("[DEBUG] keys:", sorted(res.keys()))
                first = False

            snr = safe_float(isinstance(res, dict) and res.get("snr"))
            ip = norm_init_progress(res)
            raw_bpm = bpm_from_res(res)

            now = time.time()

            # 门限：预热完成 + SNR 合格 + 频带内
            valid = (
                (ip is None or ip >= 0.99) and
                (snr is None or snr >= args.snr_min) and
                (raw_bpm is not None and 60.0*args.f_low <= raw_bpm <= 60.0*args.f_high)
            )

            if valid:
                q.append(raw_bpm)
                last_good_bpm = sum(q)/len(q)  # 简单均值/也可改中位数：sorted(q)[len(q)//2]
                last_good_t = now

            # 每秒对齐输出
            if now >= next_tick:
                hms = datetime.now().strftime("%H:%M:%S")
                unix_s = int(next_tick)

                # 失锁短于 hold-last-for 秒 → 保留上次值；超时则空
                if last_good_bpm is not None and (now - last_good_t) <= args.hold_last_for:
                    out_bpm = f"{last_good_bpm:.2f}"
                    note = []
                else:
                    out_bpm = ""
                    note = []

                if snr is not None: note.append(f"snr={snr:.2f}")
                if ip is not None:  note.append(f"init={ip:.2f}")
                if raw_bpm is not None and not valid:
                    note.append(f"raw={raw_bpm:.2f}")  # 观察原始估计
                print(f"{hms},{unix_s},{out_bpm},{'/'.join(note)}")

                next_tick += 1
    except KeyboardInterrupt:
        pass
    finally:
        try: client.stop_session()
        except: pass
        client.disconnect()

if __name__ == "__main__":
    main()