# vivo-ota-watcher

基于 [VIVO-OTA-Tracker](https://github.com/) 的 unidbg 模拟方案，使用 **GitHub Actions 每日定时** 请求指定机型的 Vivo 升级包，检测是否有新版本发布，并将结果写回仓库 / 开启 Issue 通知。

## 原理

- 复用原 PC 版 `unidbg-android-*.jar` + `libs/libvivoseckey.so`，在 Linux（ubuntu-latest）上 **无头** 运行。
- unidbg 在 ARM 模拟环境中加载 `libvivoseckey.so` 完成 `jvq_param` 的加密/解密，对 `sysupgrade.vivo.com.cn` 发起真实 OTA 查询。
- 无需 Windows、无需手机、无需 GUI。

## 目录结构

```
vivo-ota-watcher/
├── .github/workflows/daily.yml   # 每日定时任务
├── models.json                   # 监控机型列表
├── run_check.py                  # CLI 入口（去 GUI，跨平台）
├── libs/libvivoseckey.so         # unidbg 加载的 native 库
├── unidbg-android-*.jar          # unidbg 主程序
└── results/                      # 历次结果（diff 出新版本）
```

## 本地运行（Linux / macOS / WSL）

```bash
# 1. 安装 JDK 8（或 11）
# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 运行全部机型
python run_check.py

# 或指定单个机型
python run_check.py --model PD2408
```

## GitHub Actions 自动运行

`daily.yml` 每天 **北京时间 09:00**（`cron: '0 1 * * *'`）自动：

1. 在 `ubuntu-latest` 上 `setup-java`（Temurin 8）。
2. 对 `models.json` 中每个机型调用 unidbg 查询。
3. 与 `results/` 中上一次结果比对，发现新版本则：
   - 更新 `results/<model>.json` 并 commit 回仓库；
   - （可选）开启 Issue 通知（需在仓库 Secrets 配置 `GH_TOKEN`）。
4. 支持 `workflow_dispatch` 手动触发测试。

## 自定义监控机型

编辑 `models.json`，字段含义：

| 字段 | 说明 |
|---|---|
| `name` | 展示名（仅用于结果展示） |
| `device_type` | 设备类型，通常 `phone` |
| `model_sw_ver` | 机型软件版本号，如 `PD2408` |
| `rom_version` | ROM 大版本，如 `14.0` |
| `region` | 区域，如 `CN` |

## 注意事项

- **unicorn native 由 jar 内嵌**：`unidbg-android-*.jar` 已包含 `natives/linux_64/libunicorn_java.so` 等各平台原生库，unidbg 运行时会按当前 OS/ARCH 自动抽取加载，**无需手动提供 libunicorn**。仅当 jar 缺失某平台时才需把 `libunicorn_java.so` 放进 `libs/` 目录手动覆盖。
- `libs/libvivoseckey.so` 是 ARM 版，由 unidbg 在 ARM 模拟环境中加载，Linux x64 runner 可直接使用。
- unidbg 首次初始化较慢（约 10–30 秒/机型），属正常。
- 若 Vivo 调整接口或加密，需同步更新 `unidbg-android-*.jar` 与 `libvivoseckey.so`。
- GitHub Actions 免费额度每月 2000 分钟，每日运行几分钟完全足够。
