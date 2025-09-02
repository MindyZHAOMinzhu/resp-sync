#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, math, time, warnings
from collections import deque
from datetime import datetime

warnings.filterwarnings("ignore", category=RuntimeWarning, module="acconeer.exptool")

from acconeer.exptool.a111 import Client, IQServiceConfig
from acconeer.exptool.a111.algo.sleep_breathing import _processor as sb

def safe_float(x):
    if isinstance(x, bool):
        return None
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
    raw = res.get("init_progress") if isinstance(res, dict) else None
    ip = safe_float(raw)
    if ip is None:
        return 1.0
    return ip/100.0 if ip > 1.0 else ip

def main():
    ap = argparse.ArgumentParser("A111 IQ sleep-breathing (stable 1Hz logging)")
    ap.add_argument("--host", required=True)
    ap.add_argument("--r0", type=float, default=0.40)
    ap.add_argument("--r1", type=float, default=0.60)
    ap.add_argument("--rate", type=float, default=12.0)
    ap.add_argument("--n-dft", type=int, default=15, help="DFT长度（整数）")
    ap.add_argument("--f-low", type=float, default=0.12)  # 抬高低频截止，默认 0.12 Hz ≈ 7.2 bpm
    ap.add_argument("--f-high", type=float, default=0.70)
    ap.add_argument("--snr-min", type=float, default=10.0)
    ap.add_argument("--hold-last-for", type=float, default=5.0, help="秒；短时失锁则保留上次值")

    # 新增稳健性参数
    ap.add_argument("--smooth-window", type=int, default=5)
    ap.add_argument("--smooth", choices=["mean","median"], default="median")
    ap.add_argument("--prominence-min", type=float, default=1.6, help="主峰与次峰能量比阈值")
    ap.add_argument("--max-step-bpm", type=float, default=6.0, help="相邻有效 BPM 最大跳变（绝对值）")
    ap.add_argument("--max-ratio", type=float, default=1.5, help="相邻有效 BPM 最大倍数变化")
    ap.add_argument("--debug", action="store_true")
    
    ap.add_argument("--out", type=str, default=None,
                help="可选：指定输出 CSV 文件名（追加模式）")


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
    pc.n_dft = int(args.n_dft)
    pc.t_freq_est = 0.2
    pc.D = 124
    pc.f_low, pc.f_high = args.f_low, args.f_high
    pc.lambda_p, pc.lambda_05 = 40.0, 1.0
    proc = sb.Processor(cfg, pc, sess)

    print("time_hms,unix_s,bpm,note")
    
    csv_fh = None
    if args.out_csv:
        csv_fh = open(args.out_csv, "a", buffering=1)
        csv_fh.write("time_hms,unix_s,bpm,note\n")
    
    q = deque(maxlen=args.smooth_window)
    last_good_bpm = None
    last_good_t = 0.0

    t0 = time.time()
    next_tick = math.floor(t0) + 1
    debug_keys_printed = False

    try:
        while True:
            info, data = client.get_next()
            res = proc.process(data, info)

            if args.debug and (not debug_keys_printed) and isinstance(res, dict):
                print("[DEBUG] keys:", sorted(res.keys()))
                debug_keys_printed = True

            # 估计量
            ip = norm_init_progress(res)
            raw_bpm = bpm_from_res(res)

            # ----- 自定义 SNR（峰值/噪声中位数） -----
            ps = res.get("power_spectrum") if isinstance(res, dict) else None
            snr = None
            try:
                import numpy as np
                if ps is not None:
                    ps = np.asarray(ps, dtype=float)
                    if np.all(np.isfinite(ps)) and ps.size > 8 and np.any(ps > 0):
                        peak_i = int(np.nanargmax(ps))
                        lo = max(0, peak_i-2)
                        hi = min(ps.size, peak_i+3)
                        mask = np.ones(ps.shape[0], dtype=bool)
                        mask[lo:hi] = False
                        noise = ps[mask]
                        noise = noise[noise > 0]
                        if noise.size > 0:
                            snr = 10.0 * math.log10(ps[peak_i] / np.median(noise))
            except Exception:
                snr = None

            if snr is None:
                snr = safe_float(isinstance(res, dict) and res.get("snr"))

            now = time.time()
            bpm_lo, bpm_hi = 60.0*args.f_low, 60.0*args.f_high

            # 基础有效性
            valid = (
                (ip is None or ip >= 0.99) and
                (snr is None or snr >= args.snr_min) and
                (raw_bpm is not None and bpm_lo <= raw_bpm <= bpm_hi)
            )

            # 峰显著性门
            try:
                if valid and ps is not None and 'np' in globals():
                    idx = np.argpartition(ps, -3)[-3:]
                    tops = np.sort(ps[idx])
                    if tops.size >= 2 and tops[-2] > 0:
                        if (tops[-1] / tops[-2]) < args.prominence_min:
                            valid = False
            except Exception:
                pass

            # 突变抑制门
            if valid and (last_good_bpm is not None):
                r = raw_bpm / max(1e-6, last_good_bpm)
                if (abs(raw_bpm - last_good_bpm) > args.max_step_bpm) or (r > args.max_ratio) or (r < 1/args.max_ratio):
                    valid = False

            # 更新滤波值
            if valid:
                q.append(raw_bpm)
                if args.smooth == "median":
                    last_good_bpm = sorted(q)[len(q)//2]
                else:
                    last_good_bpm = sum(q)/len(q)
                last_good_t = now

            # 每秒对齐输出（处理可能的漏秒）
            while now >= next_tick:
                hms = datetime.now().strftime("%H:%M:%S")
                unix_s = int(next_tick)

                note = []
                held = (last_good_bpm is not None) and ((now - last_good_t) <= args.hold_last_for)
                if held:
                    out_bpm = f"{last_good_bpm:.2f}"
                    note.append("held=1")
                else:
                    out_bpm = ""

                if snr is not None: note.append(f"snr={snr:.2f}")
                if ip  is not None: note.append(f"init={ip:.2f}")

                if raw_bpm is not None and not valid and (bpm_lo <= raw_bpm <= bpm_hi):
                    note.append(f"raw={raw_bpm:.2f}")

                if args.debug:
                    fe = safe_float(isinstance(res, dict) and res.get("f_est"))
                    fd = safe_float(isinstance(res, dict) and res.get("f_dft_est"))
                    if fe is not None: note.append(f"f_est={fe*60:.2f}")
                    if fd is not None: note.append(f"f_dft={fd*60:.2f}")

                print(f"{hms},{unix_s},{out_bpm},{'/'.join(note)}")
                if csv_fh:
                    csv_fh.write(f"{hms},{unix_s},{out_bpm},{'/'.join(note)}\n")
                next_tick += 1

    except KeyboardInterrupt:
        pass
    finally:
        try: client.stop_session()
        except: pass
        client.disconnect()
        # <-- 在这里关闭文件
        if csv_fh:
            csv_fh.close()

if __name__ == "__main__":
    main()
