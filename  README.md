# resp-sync

同步启动 **Acconeer A111 雷达** 与 **Vernier Go Direct 呼吸带 (GDX-RB)** 的实验框架。

## 依赖

- Python 3.9+
- 你的原始脚本：
  - `a111_bpm_iq_ok.py`（雷达）
  - `gdx_getting_started_usb_0807.py`（GDX，需在 `godirect-examples-main` 下运行）
- 库：
  ```bash
  pip install pyyaml
