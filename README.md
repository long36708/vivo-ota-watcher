# vivo-ota-watcher

使用 **GitHub Actions 每日定时** 查询指定机型的 Vivo OTA 升级包，检测是否有新版本发布，结果写回仓库并开 Issue 通知。

纯 Python 实现（基于 [VIVO-OTA-Tracker-python](../origin-vivo-tracker/VIVO-OTA-Tracker-python)）：vivo 升级服务的 `jvq_param` 加密为固定的 **AES-128-CBC**（密钥/IV 自 `libvivoseckey.so` 逆向提取），无需 unidbg / JDK / 模拟器，Linux CI 上十几秒即可完成全部机型查询。

## 原理

```
机型参数拼接 → AES-128-CBC 加密 + 协议头封装 + base64url → jvq_param
  → POST https://sysupgrade.vivo.com.cn/vgc/v2/getVgcAndPatch.do?   （OTA 查询）
  → 解密响应，提取 version / pkName / pkLen / h5Url / pk
  → 取 pk URL 的 query 再次加密 → POST /pk/redirPost.do             （换取下载直链）
```

- 加密/协议封装/请求/解析全部在 [`vivo_ota_tracker.py`](vivo_ota_tracker.py)（与上游 VIVO-OTA-Tracker-python 保持一致，便于同步）。
- 编排（多机型、结果落盘、diff、通知）在 [`run_check.py`](run_check.py)。

## 目录结构

```
vivo-ota-watcher/
├── .github/workflows/daily.yml   # 每日定时任务
├── models.json                   # 监控机型列表
├── vivo_ota_tracker.py           # 查询引擎（加密/请求/解析，单机型 CLI 亦可独立使用）
├── run_check.py                  # 编排入口（多机型批处理 + 结果比较 + 通知产物）
└── results/                      # 每次检查结果（results/<model_sw_ver>.json）
```

## 本地运行

```bash
pip install -r requirements.txt

# 查询 models.json 中全部机型（机型间间隔 10s）
python run_check.py

# 调试：打印原始请求/响应
python run_check.py --verbose

# 缩短机型间隔（本地快速测试）
python run_check.py --interval 3

# 单机型独立查询（引擎自带 CLI）
python vivo_ota_tracker.py -t phone -m PD2419 -d V2419A -v 15.0.33.7.W10 -a 15 --isfull true
```

每次运行会更新 `results/<model_sw_ver>.json`，并与上次成功结果比较（比较 `patch.version` 与 `ext.isFull`，两者都未变即视为同一版本，包名/签名/直链变化不算新版本，既不发通知也不追加 `history`；此时 `result` 直接沿用上次内容，只有 `checked_at` 与 `history[].last_seen` 会刷新）：

- `success`：服务器返回了更新包，`data` 中原样保留服务器结构：`patch`（版本、包名、`pk` 重定向链接、`pkSha256`、`pkLen`、`h5Url`…）、`ext`（`isFull`、`storage`、`timeStamp`…），另加二次请求换来的 `download_url`（`redirPost` 不下发签名地址时回退为 `https://sysupdxdl.vivo.com.cn/upgrade/oem/files/<pkName>`）
- `no_update`：服务器正常响应但没有可用版本（常见于基线版本不在官方升级路线内，retcode 210），记录 `retcode` / `message`
- `error`：请求/解析失败（原因记录在结果文件里）

结果文件末尾还有 `history` 数组，记录该机型的**升级轨迹**（只记录 `success` 结果）：

```json
"history": [
  {
    "version": "16.1.12.28.W10.V000L1",
    "pkName": "20260717075431f8eb01ea1b4affdeb9912664db0b2741.zip",
    "pkSha256": "90967a06...f6a97613",
    "pkLen": "11653209780",
    "download_url": "https://sysupdxdl.vivo.com.cn/upgrade/oem/files/....zip?sign=...&t=...",
    "h5Url": "https://sysdesc.vivo.com.cn/upgrade/h5/2025/10/2025102923283323282148-2/index.html",
    "isFull": 1,
    "ggBugDate": "2026-06-01",
    "first_seen": "2026-09-07T00:52:26+08:00",
    "last_seen": "2026-09-07T00:52:45+08:00"
  }
]
```

与最后一条的 `version` + `isFull` 相同则只刷新 `last_seen`（并补齐缺失字段），不同才追加新条目，最多保留最近 50 条（`HISTORY_LIMIT`）。这样 `results/` 里的 `result` 仍是"最新一次"，历史版本不会被覆盖丢失。

## GitHub Actions 自动运行

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) 每天 **北京时间 09:00 / 19:00 / 20:30 / 22:00** 自动执行，也支持 `workflow_dispatch` 手动触发：

| 北京时间 | cron (UTC) |
|---|---|
| 09:00 | `0 1 * * *` |
| 19:00 | `0 11 * * *` |
| 20:30 | `30 12 * * *` |
| 22:00 | `0 14 * * *` |

1. 安装 Python 依赖后运行 `python run_check.py`。
2. 将 `results/` 的变更以 `chore: update OTA check results <date>` 提交回仓库；**全部机型都是 `no_update` 时跳过提交**（此时文件内容只有 `checked_at` 变化，避免每天 4 次运行刷无意义的 commit）。
3. 发现新版本时：创建/更新标题为 **「vivo OTA 新版本汇总」** 的 Issue（含版本、包大小、changelog、下载直链）。通知按 `机型:版本` 指纹去重——同一批版本只通知一次，**同一天内后续出现的新版本仍会通知**。
4. `concurrency` 防止两次运行重叠。

## 自定义监控机型

编辑 `models.json` 的 `models` 数组（`model_sw_ver` 必须唯一，作为结果文件名）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 否 | 展示名 |
| `device_type` | 否 | `phone`（默认）或 `tablet`，两者请求参数集不同 |
| `model_sw_ver` | ✅ | 机型软件代码，如 `PD2419` |
| `device_model` | ✅ | 公开型号，如 `V2419A`（规律：`PD2419` → `V2419A`） |
| `sw_version` | ✅ | 该机型当前已装版本基线，如 `15.1.15.5.W10`，用于查询更高版本 |
| `android_ver` | 否 | 安卓大版本（int），仅 tablet 分支使用 |
| `snp` | 否 | 序列号占位，仅 tablet 分支使用，默认 `A0000000000000A` |
| `is_full` | 否 | `true` 整包 / `false` 差分包，默认 `true` |

机型代码查询：<https://khwang9883.github.io/MobileModels/brands/vivo_cn.html>

## 注意事项

- **基线版本很关键**：`sw_version` 必须是该机型真实存在的版本，且在官方开放升级路线内，否则服务器返回 `No update`（retcode 210）。
- 版本号含 `.W` 时引擎会自动补 `.V000L1` 等派生字段，无需手写完整版本串。
- 请求过于频繁可能被限流，脚本默认机型间间隔 10 秒。
- 若 Vivo 调整接口或加密，需要同步更新 `vivo_ota_tracker.py`（上游仓库更新后可直接覆盖）。
- 首次运行（无历史结果）查到可用更新时即视为新版本，会触发一次通知。
