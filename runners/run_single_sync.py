import os, sys, yaml, time, math, signal, subprocess
from datetime import datetime
from pathlib import Path

def load_cfg(path="configs/single.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def timestamped_dir(base: str, label_fmt: str):
    ts = datetime.now().strftime(label_fmt)
    out = Path(base) / ts
    ensure_dir(out)
    return out

def build_sleep_then_cmd(cmd: str, start_after_s: float):
    # 用 bash -lc 执行；sleep 到统一时刻后再 exec 原命令
    # 这样不动你的脚本，也能做到同步开跑
    return f"sleep {start_after_s:.3f}; exec {cmd}"

def launch_one(name: str, workdir: str, cmd: str, out_dir: Path, stdout_name: str, stderr_name: str, start_after_s: float):
    ensure_dir(out_dir)
    stdout_f = open(out_dir / stdout_name, "w")
    stderr_f = open(out_dir / stderr_name, "w")
    full_cmd = ["bash", "-lc", build_sleep_then_cmd(cmd, start_after_s)]
    print(f"[{name}] workdir={workdir}")
    print(f"[{name}] cmd={cmd}")
    print(f"[{name}] scheduled in {start_after_s:.3f}s")
    p = subprocess.Popen(
        full_cmd,
        cwd=workdir,
        stdout=stdout_f,
        stderr=stderr_f,
        preexec_fn=os.setsid  # 便于组信号控制
    )
    return p, stdout_f, stderr_f

def main():
    cfg = load_cfg()

    # 输出目录：data/raw/<session_ts>，同时放日志
    out_dir = timestamped_dir(cfg["session"]["out_dir"], cfg["session"]["label"])
    logs_dir = Path("data/logs")
    ensure_dir(logs_dir)

    start_after_s = float(cfg["sync"]["start_after_s"])

    # 先把两条命令都准备好，再“同时”sleep → exec
    procs = []
    files_to_close = []

    # RADAR
    radar = cfg["radar"]
    rp, rso, rse = launch_one(
        name="RADAR",
        workdir=radar["workdir"],
        cmd=radar["cmd"],
        out_dir=out_dir,
        stdout_name=radar["stdout_filename"],
        stderr_name=radar["stderr_filename"],
        start_after_s=start_after_s
    )
    procs.append(("RADAR", rp)); files_to_close += [rso, rse]

    # GDX
    gdx = cfg["gdx"]
    gp, gso, gse = launch_one(
        name="GDX",
        workdir=gdx["workdir"],
        cmd=gdx["cmd"],
        out_dir=out_dir,
        stdout_name=gdx["stdout_filename"],
        stderr_name=gdx["stderr_filename"],
        start_after_s=start_after_s
    )
    procs.append(("GDX", gp)); files_to_close += [gso, gse]

    print(f"\n[MASTER] Both processes scheduled. Output folder: {out_dir}")
    print("[MASTER] Press Ctrl+C to stop both.\n")

    max_dur = int(cfg.get("run", {}).get("max_duration_s", 0))
    t0 = time.time()

    try:
        while True:
            all_done = True
            for name, p in procs:
                ret = p.poll()
                if ret is None:
                    all_done = False
            if all_done:
                break
            if max_dur > 0 and (time.time() - t0) > max_dur:
                print("[MASTER] Reached max_duration_s, sending SIGINT to both...")
                for name, p in procs:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGINT)
                    except Exception as e:
                        print(f"[MASTER] SIGINT {name} error: {e}")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[MASTER] Ctrl+C detected, sending SIGINT to both...")
        for name, p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
            except Exception as e:
                print(f"[MASTER] SIGINT {name} error: {e}")
    finally:
        # 等子进程收尾
        for name, p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                # 若未退出，强制杀
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    pass
        for f in files_to_close:
            try: f.close()
            except: pass

        print(f"[MASTER] Done. Check outputs under: {out_dir}")

if __name__ == "__main__":
    main()
