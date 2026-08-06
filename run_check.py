#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vivo-ota-watcher
每日定时查询 Vivo 指定机型的 OTA 升级包。

基于 VIVO-OTA-Tracker 的 unidbg 模拟方案，去 GUI、跨平台（Linux 优先）。

用法:
    python run_check.py                # 查询 models.json 中所有机型
    python run_check.py --model PD2408 # 仅查询指定机型
    python run_check.py --no-commit    # 不写回结果文件（仅打印）
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta

# ----------------------------------------------------------------------------
# 路径配置
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JAR_NAME = "unidbg-android-0.9.10-SNAPSHOT.jar"
JAR_PATH = os.path.join(BASE_DIR, JAR_NAME)
LIBS_DIR = os.path.join(BASE_DIR, "libs")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
MODELS_FILE = os.path.join(BASE_DIR, "models.json")

# 结果标记（与 VivoOtaTracker.java 输出一致）
RESULT_START = "===VIVO_OTA_RESULT_START==="
RESULT_END = "===VIVO_OTA_RESULT_END==="


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def log(msg):
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_models():
    with open(MODELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("models", [])


def java_available():
    return shutil.which("java") is not None


def detect_unicorn():
    """
    检测 unidbg 所需的 unicorn native 库是否可用。

    说明：unidbg-android-*.jar 已内嵌各平台 natives，包括：
        natives/linux_64/libunicorn_java.so        (Linux x64)
        natives/windows_64/unicorn_java.dll         (Windows x64)
        natives/osx_64/libunicorn_java.dylib        (macOS x64)
        natives/linux_arm64/libunicorn_java.so      (Linux arm64)
        ...
    unidbg 运行时会根据当前 OS/ARCH 自动从 jar 内抽取对应文件，
    因此 **无需外部提供 libunicorn_java.so**。

    仅当 jar 内嵌缺失、需在 libs/ 下手动放置时才需要外部文件。
    本函数负责：① 校验运行平台受支持；② 若 libs/ 下有手动覆盖的
    libunicorn_java.so，则将其目录加入 java.library.path。
    """
    import platform
    system = platform.system().lower()  # linux / windows / darwin
    machine = platform.machine().lower()

    supported = {
        ("linux", "x86_64"): "linux_64",
        ("linux", "amd64"): "linux_64",
        ("windows", "amd64"): "windows_64",
        ("windows", "x86_64"): "windows_64",
        ("darwin", "x86_64"): "osx_64",
        ("darwin", "arm64"): "osx_arm64",
    }
    arch_key = supported.get((system, machine))
    if arch_key is None:
        log(f"[WARN] 当前平台 {system}/{machine} 未经验证，unidbg 可能缺少对应 native。")

    # 是否由 libs/ 手动覆盖 unicorn native
    override = None
    if os.path.isdir(LIBS_DIR):
        for fn in os.listdir(LIBS_DIR):
            if fn.startswith("libunicorn_java") or fn.startswith("unicorn_java"):
                override = os.path.join(LIBS_DIR, fn)
                break
    return arch_key, override


def prepare_work_dir(model):
    """准备 unidbg 工作目录，返回 (work_dir, jar_copy)。

    - 拷贝 jar（内嵌 unicorn native，自动按平台加载）。
    - 拷贝 libs/（含 libvivoseckey.so，unidbg 在 ARM 模拟环境中加载）。
    - 若 libs/ 下有手动覆盖的 unicorn native，则加入 java.library.path。
    """
    arch_key, unicorn_override = detect_unicorn()
    work_dir = tempfile.mkdtemp(prefix="vivo_ota_")

    # 拷贝 jar
    jar_copy = os.path.join(work_dir, JAR_NAME)
    shutil.copyfile(JAR_PATH, jar_copy)

    # 拷贝整个 libs/ 目录（含 libvivoseckey.so）
    dst_libs = os.path.join(work_dir, "libs")
    if os.path.isdir(LIBS_DIR):
        shutil.copytree(LIBS_DIR, dst_libs)

    # java.library.path 指向 work_dir（unidbg 抽取 natives 缓存目录）
    # 若手动覆盖了 unicorn native，则额外把其所在目录加入最前。
    lib_path = work_dir
    if unicorn_override:
        log(f"[INFO] 检测到手动覆盖的 unicorn native: {os.path.basename(unicorn_override)}")
        lib_path = os.pathsep.join([os.path.dirname(unicorn_override), work_dir])
    return work_dir, jar_copy, lib_path


def build_java_command(work_dir, jar_copy, model, lib_path):
    """构造 java 命令。参数对齐 VivoOtaTracker.java 的 System.getProperty 默认值。"""
    sw = model.get("model_sw_ver", "")
    device_type = "tablet" if sw.startswith("DPD") else "phone"
    cmd = [
        "java",
        "-Djava.awt.headless=true",
        "-Djava.net.preferIPv4Stack=true",
        f"-DDEVICE_TYPE={device_type}",
        f"-DMODEL_SW_VER={sw}",
        f"-DDEVICE_MODEL={model.get('device_model', sw)}",
        f"-DSW_VERSION={model.get('sw_version', '')}",
        f"-DANDROID_VER={model.get('android_ver', '')}",
        f"-DSNP={model.get('snp', 'A0000000000000A')}",
        f"-DIMEI={model.get('imei', '')}",
        f"-DIS_FULL={model.get('is_full', 'true')}",
        f"-DMODE={model.get('mode', 'NORMAL')}",
        "-Dhttps.protocols=TLSv1.2",
        f"-Djava.library.path={lib_path}",
        "-jar", jar_copy,
    ]
    return cmd


def parse_ota_result(output):
    """从 java stdout 中提取 ===VIVO_OTA_RESULT_START=== ... END=== 之间的 JSON。"""
    start = output.find(RESULT_START)
    end = output.find(RESULT_END)
    if start == -1 or end == -1 or end <= start:
        return None
    raw = output[start + len(RESULT_START):end].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def run_single(model):
    """运行单个机型的查询，返回 (status, payload)。

    status:
        "success" -> payload 为解析后的 OTA 结果 dict
        "error"   -> payload 为错误描述 dict（含 reason）
    每个机型独立进程、独立临时目录，互不牵连。
    """
    name = model.get("name", model.get("model_sw_ver", "unknown"))
    sw = model.get("model_sw_ver", "unknown")
    log(f"开始查询机型: {name} ({sw})")
    work_dir, jar_copy, lib_path = prepare_work_dir(model)
    cmd = build_java_command(work_dir, jar_copy, model, lib_path)
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log(f"  查询超时: {name}")
        return "error", {"reason": "timeout", "detail": "java process timeout (300s)"}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if proc.returncode != 0:
        log(f"  Java 进程异常退出 (code={proc.returncode})，stderr 前 500 字符:\n{proc.stderr[:500]}")
        return "error", {"reason": "nonzero_exit", "code": proc.returncode,
                         "stderr": proc.stderr[:500]}

    result = parse_ota_result(proc.stdout)
    if result is None:
        log(f"  未解析到结果标记，stdout 前 500 字符:\n{proc.stdout[:500]}")
        return "error", {"reason": "no_result_marker", "stdout": proc.stdout[:500]}
    log(f"  查询成功: {name}")
    return "success", result


def result_path(model):
    sw = model.get("model_sw_ver", "unknown")
    return os.path.join(RESULTS_DIR, f"{sw}.json")


def load_prev(model):
    p = result_path(model)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_result(model, result):
    """result 形如 {"status": "success", "data": {...}} 或 {"status": "error", ...}。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    payload = {
        "model": model,
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "result": result,
    }
    with open(result_path(model), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _extract_data(record):
    """从结果文件 record 中取出真正用于比较的 OTA 数据 dict。"""
    if not isinstance(record, dict):
        return {}
    res = record.get("result", {})
    if not isinstance(res, dict):
        return {}
    if res.get("status") == "success":
        return res.get("data", {})
    if res.get("status") == "error":
        return {}  # 失败记录不参与新版本比较
    return res  # 兼容旧结构（直接是 OTA dict）


def is_new_version(prev, cur):
    """简单比较：prev 不存在、或关键字段变化，视为有新版本。"""
    if prev is None:
        return True
    prev_res = _extract_data(prev)
    cur_res = _extract_data({"result": cur})
    # 根据 VivoOtaTracker 输出字段自行调整比较键
    keys = ["version", "rom_version", "sw_version", "build_id", "ota_version"]
    for k in keys:
        if prev_res.get(k) != cur_res.get(k):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="vivo-ota-watcher")
    parser.add_argument("--model", help="仅查询指定 model_sw_ver")
    parser.add_argument("--no-commit", action="store_true", help="不写回结果文件")
    args = parser.parse_args()

    if not java_available():
        log("未找到 java，请先安装 JDK 8 或 11。")
        sys.exit(1)
    if not os.path.exists(JAR_PATH):
        log(f"缺少 jar: {JAR_PATH}")
        sys.exit(1)
    if not os.path.exists(os.path.join(LIBS_DIR, "libvivoseckey.so")):
        log(f"缺少 native 库: {LIBS_DIR}/libvivoseckey.so")
        sys.exit(1)

    models = load_models()
    if args.model:
        models = [m for m in models if m.get("model_sw_ver") == args.model]
        if not models:
            log(f"models.json 中未找到 model_sw_ver={args.model}")
            sys.exit(1)

    found_new = []
    failed = []
    for model in models:
        status, cur = run_single(model)
        sw = model.get("model_sw_ver", "unknown")
        if status == "error":
            # 失败机型也留存记录，便于排查，且不影响其他机型
            failed.append((model, cur))
            if not args.no_commit:
                save_result(model, {"status": "error", **cur})
            log(f"  [失败] {model.get('name')} ({sw}): {cur.get('reason')}")
            continue
        prev = load_prev(model)
        if is_new_version(prev, {"result": cur}):
            found_new.append((model, prev, cur))
            log(f"  >>> 检测到新版本: {model.get('name')}")
        else:
            log(f"  无变化: {model.get('name')}")
        if not args.no_commit:
            save_result(model, {"status": "success", "data": cur})

    if found_new:
        log(f"共发现 {len(found_new)} 个机型有新版本。")
        # 供 GitHub Action 步骤读取
        with open(os.path.join(BASE_DIR, "new_versions.txt"), "w", encoding="utf-8") as f:
            for model, _, cur in found_new:
                f.write(f"{model.get('name')} ({model.get('model_sw_ver')})\n")

    if failed:
        log(f"共 {len(failed)} 个机型查询失败: " +
            ", ".join(m.get("model_sw_ver") for m, _ in failed))

    # 退出码: 0=全部正常且无新版本; 2=有新版本; 3=有新版本且有失败; 4=全部失败/无成功
    if found_new and failed:
        sys.exit(3)
    if found_new:
        sys.exit(2)
    if failed:
        sys.exit(4)
    sys.exit(0)


if __name__ == "__main__":
    main()
