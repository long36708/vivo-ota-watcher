#!/usr/bin/env python3
"""Vivo OTA 每日检测编排脚本。

读取 models.json 中配置的机型，逐个调用 vivo_ota_tracker 查询 OTA 更新包，
结果写入 results/<model_sw_ver>.json，并与上次成功结果比较：

- 发现新版本时写 new_versions.txt 与 issue_body.md（均不入库），
  供 GitHub Actions 的结果提交 / Issue 通知步骤消费；
- 存在 $GITHUB_STEP_SUMMARY 时输出 Markdown 摘要表；
- 存在 $GITHUB_OUTPUT 时输出 has_new_versions 供 workflow 条件判断。

退出码：0=全部查询完成；1=部分机型失败；2=全部失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from vivo_ota_tracker import (
    DeviceConfig,
    UpdateInfo,
    build_request_params,
    join_params,
    parse_update_response,
    print_update_info,
    send_encrypted_request,
)

ROOT = Path(__file__).resolve().parent
MODELS_FILE = ROOT / "models.json"
RESULTS_DIR = ROOT / "results"
NEW_VERSIONS_FILE = ROOT / "new_versions.txt"
ISSUE_BODY_FILE = ROOT / "issue_body.md"

# vivo_ota_tracker 解析不到字段时使用的占位符
NOT_FOUND = "(Not found)"
DEFAULT_INTERVAL_SECONDS = 10
# 每个结果文件最多保留的历史版本条数
HISTORY_LIMIT = 50

STATUS_LABELS = {
    "success": "✅ 有更新",
    "no_update": "➖ 无更新",
    "error": "❌ 失败",
}


def build_device_config(entry: dict) -> DeviceConfig:
    """把 models.json 的一个机型条目转换为引擎的 DeviceConfig。"""
    return DeviceConfig(
        device_type=entry.get("device_type") or "phone",
        model_sw_ver=entry["model_sw_ver"],
        device_model=entry["device_model"],
        sw_version=entry["sw_version"],
        android_ver=int(entry.get("android_ver") or 13),
        snp=entry.get("snp") or "A0000000000000A",
        is_full=str(entry.get("is_full", "true")).lower() in ("1", "true", "yes"),
    )


def query_with_detail(config: DeviceConfig) -> tuple[Optional[UpdateInfo], Optional[str]]:
    """查询单个机型，返回 (info, error_reason)，二者恰有一个非 None。

    不走引擎的 query_ota_update，以便把失败原因带回结果文件。
    """
    try:
        raw_params = join_params(build_request_params(config))
    except Exception as exc:
        return None, f"构造请求参数失败: {exc}"
    if config.verbose:
        print(f"  Raw Request Params: {raw_params}")
    try:
        response = send_encrypted_request(raw_params)
    except Exception as exc:
        return None, f"请求失败: {exc}"
    if config.verbose:
        print(f"  Raw Update Response: {response}")
    if response.startswith("[Error]"):
        return None, response
    return parse_update_response(response), None


def clean(value: Optional[str]) -> Optional[str]:
    """去掉引擎解析失败时的 (Not found) 占位符。"""
    if value in (None, "", NOT_FOUND):
        return None
    return value


def drop_none(mapping: dict) -> dict:
    """丢弃值为 None 的字段，避免落盘大量 null。"""
    return {k: v for k, v in mapping.items() if v is not None}


def patch_of(data: dict) -> dict:
    """取结果中的 patch 对象（不存在时返回空字典）。"""
    patch = data.get("patch")
    return patch if isinstance(patch, dict) else {}


def result_version(data: dict) -> Optional[str]:
    return patch_of(data).get("version")


def result_filename(data: dict) -> Optional[str]:
    return patch_of(data).get("pkName")


def result_size_mb(data: dict) -> int:
    """从 pkLen（字节）换算 MB。"""
    pk_len = patch_of(data).get("pkLen")
    try:
        return int(pk_len) // 1048576
    except (TypeError, ValueError):
        return 0


def result_changelog(data: dict) -> Optional[str]:
    return patch_of(data).get("h5Url")


def classify(info: Optional[UpdateInfo], reason: Optional[str]) -> dict:
    """把查询结果归类为 success / no_update / error 三种状态。

    success 时原样保留服务器返回的 patch / ext 结构，
    仅把二次请求换来的 download_url 放在与 patch 同级的顶层。
    """
    if reason is not None:
        return {"status": "error", "data": {"reason": reason}}

    if not info.patch:
        # 服务器正常响应但没有可用版本（如基线不在升级路线内 retcode 210）
        return {
            "status": "no_update",
            "data": drop_none({
                "retcode": info.retcode,
                "message": info.message,
                "detail": (info.raw_response or "")[:400] or None,
            }),
        }

    return {
        "status": "success",
        "data": drop_none({
            "retcode": info.retcode,
            "patch": info.patch,
            "ext": info.ext,
            "download_url": clean(info.download_url),
        }),
    }


def check_model(entry: dict, verbose: bool) -> dict:
    """查询单个机型并返回 result 字典。"""
    config = build_device_config(entry)
    config.verbose = verbose
    print(f"  Device: {config.device_type} | {config.device_model} / {config.model_sw_ver}")
    print(f"  Base Version: {config.sw_version}")
    info, reason = query_with_detail(config)
    result = classify(info, reason)
    if result["status"] == "success":
        print_update_info(info)
    else:
        data = result["data"]
        detail = data.get("reason") or data.get("message") or data.get("detail") or ""
        print(f"  [{result['status']}] {detail}")
    return result


def result_path(entry: dict) -> Path:
    return RESULTS_DIR / f"{entry['model_sw_ver']}.json"


def load_previous(entry: dict) -> Optional[dict]:
    path = result_path(entry)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  [!] 无法读取上次结果 {path.name}: {exc}")
        return None


def history_entry(result: dict, checked_at: str) -> Optional[dict]:
    """为本次成功结果构造一条升级轨迹记录；无可用版本时返回 None。"""
    if result["status"] != "success":
        return None
    data = result["data"]
    ext = data.get("ext")
    ext = ext if isinstance(ext, dict) else {}
    record = drop_none({
        "version": result_version(data),
        "pkName": result_filename(data),
        "pkSha256": patch_of(data).get("pkSha256"),
        "pkLen": patch_of(data).get("pkLen"),
        "download_url": data.get("download_url"),
        "h5Url": result_changelog(data),
        "isFull": ext.get("isFull"),
        "ggBugDate": ext.get("ggBugDate"),
    })
    if not record.get("version"):
        return None
    record["first_seen"] = checked_at
    record["last_seen"] = checked_at
    return record


def merge_history(previous: Optional[dict], result: dict, checked_at: str) -> list:
    """把本次结果并入历史轨迹。

    - 只记录 success 结果（no_update / error 不入历史，避免每天刷噪音）；
    - 与最后一条版本+包名相同则只刷新 last_seen，不新增条目；
    - 超过 HISTORY_LIMIT 时丢弃最早的条目。
    """
    history: list = []
    if previous:
        old = previous.get("history")
        if isinstance(old, list):
            history = [item for item in old if isinstance(item, dict)]

    record = history_entry(result, checked_at)
    if record is None:
        return history

    last = history[-1] if history else None
    same_as_last = (
        last is not None
        and last.get("version") == record["version"]
        and last.get("pkName") == record["pkName"]
    )
    if same_as_last:
        last["last_seen"] = checked_at
        # 历史条目缺字段时（如新增了字段）用本次结果补齐
        for key, value in record.items():
            if key in ("first_seen", "last_seen"):
                continue
            if last.get(key) is None and value is not None:
                last[key] = value
        return history

    history.append(record)
    return history[-HISTORY_LIMIT:]


def save_result(entry: dict, result: dict, checked_at: str, history: list) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    payload = {"model": entry, "checked_at": checked_at, "result": result}
    if history:
        payload["history"] = history
    path = result_path(entry)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  Saved: {path.relative_to(ROOT)}")


def is_new_version(previous: Optional[dict], current: dict) -> bool:
    """与上次结果比较，判定是否发现新版本。

    - 本次查询失败不参与判定；本次无更新不算新版本；
    - 上次无成功结果（首次运行 / no_update / error）而本次查到更新，视为新版本；
    - 否则比较 version 与 filename，任一变化即为新版本。
    """
    if current["status"] != "success":
        return False
    if previous is None:
        return True
    prev_result = previous.get("result") or {}
    if prev_result.get("status") != "success":
        return True
    prev_data = prev_result.get("data") or {}
    current_data = current["data"]
    return (
        result_version(current_data) != result_version(prev_data)
        or result_filename(current_data) != result_filename(prev_data)
    )


def build_issue_body(new_items: list[tuple[dict, dict]], checked_at: str) -> str:
    lines = ["## 📢 检测到新的 OTA 版本", "", f"报告日期：{checked_at}", ""]
    for entry, data in new_items:
        name = entry.get("name") or entry["model_sw_ver"]
        filename = result_filename(data)
        lines += [
            f"### {name}（{entry['model_sw_ver']} / {entry['device_model']}）",
            "",
            f"- **新版本**：`{result_version(data)}`",
        ]
        if filename:
            lines.append(f"- **升级包**：`{filename}`（{result_size_mb(data)} MB）")
        changelog = result_changelog(data)
        if changelog:
            lines.append(f"- **更新日志**：<{changelog}>")
        if data.get("download_url"):
            lines.append(f"- **下载直链**：<{data['download_url']}>")
        lines.append("")
    return "\n".join(lines)


def write_new_version_files(new_items: list[tuple[dict, dict]], checked_at: str) -> None:
    """每次运行都覆写，避免残留过期的通知内容。"""
    lines = [
        f"{entry.get('name') or entry['model_sw_ver']} ({entry['model_sw_ver']}): "
        f"新版本 {result_version(data)}"
        for entry, data in new_items
    ]
    NEW_VERSIONS_FILE.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )
    body = build_issue_body(new_items, checked_at) if new_items else ""
    ISSUE_BODY_FILE.write_text(body, encoding="utf-8")


def write_step_summary(rows: list[dict], new_count: int, checked_at: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## vivo OTA 检查结果",
        "",
        f"检查时间：{checked_at}",
        "",
        "| 机型 | 型号 | 状态 | 最新版本 | 大小 (MB) |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        status = STATUS_LABELS.get(row["status"], row["status"])
        if row["is_new"]:
            status += " 🆕"
        lines.append(
            f"| {row['name']} | {row['model_sw_ver']} | {status} "
            f"| {row['version'] or '-'} | {row['size_mb'] or '-'} |"
        )
    if new_count:
        lines += ["", f"发现 **{new_count}** 个新版本，详情见通知 Issue。"]
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def set_github_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def load_models() -> list[dict]:
    try:
        raw: Any = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"读取 {MODELS_FILE} 失败: {exc}")
    models = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(models, list) or not models:
        sys.exit(f'{MODELS_FILE} 中未找到机型配置（期望 {"models": [...]} 结构）')
    required = ("model_sw_ver", "device_model", "sw_version")
    for index, entry in enumerate(models):
        missing = [key for key in required if not entry.get(key)]
        if missing:
            sys.exit(f"{MODELS_FILE} 第 {index + 1} 个机型缺少字段: {', '.join(missing)}")
    return models


def main() -> int:
    parser = argparse.ArgumentParser(description="Vivo OTA 每日检测")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
        help=f"机型间请求间隔秒数（默认 {DEFAULT_INTERVAL_SECONDS}，防限流）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印原始请求/响应")
    args = parser.parse_args()

    models = load_models()
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")

    summary_rows: list[dict] = []
    new_items: list[tuple[dict, dict]] = []
    failure_count = 0

    for index, entry in enumerate(models):
        if index > 0 and args.interval > 0:
            time.sleep(args.interval)
        name = entry.get("name") or entry["model_sw_ver"]
        print(f"\n[{index + 1}/{len(models)}] {name}")
        try:
            result = check_model(entry, verbose=args.verbose)
        except Exception as exc:  # 单个机型异常不中断整批
            result = {"status": "error", "data": {"reason": f"未预期异常: {exc}"}}
        if result["status"] == "error":
            failure_count += 1

        previous = load_previous(entry)
        new_version = is_new_version(previous, result)
        history = merge_history(previous, result, checked_at)
        save_result(entry, result, checked_at, history)
        if new_version:
            new_items.append((entry, result["data"]))

        data = result["data"]
        summary_rows.append({
            "name": name,
            "model_sw_ver": entry["model_sw_ver"],
            "status": result["status"],
            "version": result_version(data),
            "size_mb": result_size_mb(data),
            "is_new": new_version,
        })

    write_new_version_files(new_items, checked_at)
    write_step_summary(summary_rows, len(new_items), checked_at)
    set_github_output("has_new_versions", "true" if new_items else "false")

    print(
        f"\n=== Done: {len(models)} models, "
        f"{failure_count} failures, {len(new_items)} new versions ==="
    )
    if failure_count == len(models):
        return 2
    if failure_count:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
