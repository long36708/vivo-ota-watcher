#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vivo OTA Tracker - 源码编译运行模式 (run_source.py)

设计目标：
  之前的 run_check.py 依赖一个打包好的 unidbg-android-0.9.10-SNAPSHOT.jar，
  但该 jar 内 Linux 的 libunicorn_java.so / libunicorn.so 是坏的
  (缺 13 个 TCG helper 符号，unidbg 私有符号，系统库无法提供)，
  导致 CI (Linux) 始终报 `undefined symbol: helper_div_i32`。

  本脚本改为对齐官方 README 的源码编译方式：
    git clone https://github.com/zhkl0228/unidbg
    ./mvnw clean install -DskipTests -Dgpg.skip=true   # 框架编译出正确的 native
    ./mvnw exec:java -pl unidbg-android \
        -Dexec.mainClass=com.vivo.ota.VivoOtaTracker \
        -DDEVICE_TYPE=... -DMODEL_SW_VER=... ...

  注意：unidbg 源码使用 JDK 8 编译/运行（与 JDK9+ 的 java.lang.Module 冲突），
  CI 中必须 setup-java@8。

用法：
  python run_source.py build           # clone + maven 编译 unidbg (首次/缓存失效)
  python run_source.py run <model>     # 运行单个机型 (model 为 models.json 的 key)
  python run_source.py run-all         # 运行 models.json 中所有机型
  python run_source.py setup           # 仅把 java/so 同步进 unidbg 源码目录
"""

import json
import os
import shutil
import subprocess
import sys
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
UNIDBG_DIR = os.path.join(ROOT, "unidbg")
JAVA_SRC = os.path.join(ROOT, "src", "main", "java", "com", "vivo", "ota", "VivoOtaTracker.java")
SO_SRC = os.path.join(ROOT, "libs", "libvivoseckey.so")
MODELS_JSON = os.path.join(ROOT, "models.json")

UNIDBG_REPO = "https://github.com/zhkl0228/unidbg.git"
# 使用最新稳定 tag；0.9.10-SNAPSHOT 无 tag，V0.9.9 是最近的稳定版，API 兼容 0.9.x
UNIDBG_TAG = "V0.9.9"

# 同步进 unidbg 源码的目标位置
TARGET_JAVA = lambda: os.path.join(UNIDBG_DIR, "unidbg-android", "src", "main", "java", "com", "vivo", "ota", "VivoOtaTracker.java")
TARGET_SO = lambda: os.path.join(UNIDBG_DIR, "unidbg-android", "libs", "libvivoseckey.so")


def log(msg):
    print(f"[run_source] {msg}", flush=True)


def run(cmd, cwd=None, env=None, check=True):
    log("CMD: " + " ".join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str))
    if check and r.returncode != 0:
        log(f"[ERROR] command failed (exit {r.returncode}): {cmd if isinstance(cmd, list) else ''}")
        sys.exit(r.returncode)
    return r


def sync_sources():
    """把 VivoOtaTracker.java 和 libvivoseckey.so 同步进 unidbg 源码目录。"""
    if not os.path.isfile(JAVA_SRC):
        log(f"[ERROR] 找不到 Java 源文件: {JAVA_SRC}")
        sys.exit(1)
    if not os.path.isfile(SO_SRC):
        log(f"[ERROR] 找不到 native 库: {SO_SRC}")
        sys.exit(1)
    os.makedirs(os.path.dirname(TARGET_JAVA()), exist_ok=True)
    os.makedirs(os.path.dirname(TARGET_SO()), exist_ok=True)
    shutil.copyfile(JAVA_SRC, TARGET_JAVA())
    shutil.copyfile(SO_SRC, TARGET_SO())
    log(f"已同步 VivoOtaTracker.java -> {TARGET_JAVA()}")
    log(f"已同步 libvivoseckey.so   -> {TARGET_SO()}")


def build():
    """clone（或更新）unidbg 源码并 maven 编译。"""
    if not os.path.isdir(UNIDBG_DIR):
        log(f"clone unidbg ({UNIDBG_TAG}) ...")
        run(["git", "clone", "--depth", "1", "--branch", UNIDBG_TAG, UNIDBG_REPO, UNIDBG_DIR])
    else:
        log("unidbg 目录已存在，跳过 clone")

    sync_sources()

    mvnw = os.path.join(UNIDBG_DIR, "mvnw")
    if not os.path.isfile(mvnw):
        log("[ERROR] 未找到 mvnw，clone 可能不完整")
        sys.exit(1)

    log("maven 编译 unidbg (install, skip tests) ...")
    run([mvnw, "clean", "install", "-DskipTests", "-Dgpg.skip=true", "-q"], cwd=UNIDBG_DIR)
    log("unidbg 编译完成")


def run_model(key, model):
    """运行单个机型。返回 (rc, stdout)。"""
    sync_sources_if_needed()

    mvnw = os.path.join(UNIDBG_DIR, "mvnw")
    if not os.path.isfile(mvnw):
        log("[ERROR] 未编译 unidbg，请先执行 build")
        sys.exit(1)

    dt = model.get("deviceType", "phone")
    props = [
        ("DEVICE_TYPE", dt),
        ("MODEL_SW_VER", model.get("modelSwVer", "")),
        ("DEVICE_MODEL", model.get("deviceModel", "")),
        ("SW_VERSION", model.get("swVersion", "")),
        ("ANDROID_VER", str(model.get("androidVer", 16))),
        ("SNP", model.get("snp", "A0000000000000A")),
        ("IS_FULL", str(model.get("isFull", True)).lower()),
    ]
    if "imei" in model:
        props.append(("IMEI", model["imei"]))

    cmd = [mvnw, "exec:java", "-pl", "unidbg-android",
           "-Dexec.mainClass=com.vivo.ota.VivoOtaTracker"]
    for k, v in props:
        if v != "":
            cmd.append(f"-D{k}={v}")

    log(f"运行机型 {key} ...")
    r = run(cmd, cwd=UNIDBG_DIR, check=False)
    return r.returncode, r.stdout


def sync_sources_if_needed():
    if os.path.isfile(TARGET_JAVA()) and os.path.isfile(TARGET_SO()):
        # 简单同步：始终复制最新
        sync_sources()
    else:
        sync_sources()


def parse_output(text):
    """从 Java 文本输出中解析关键信息。"""
    info = {"version": None, "filename": None, "size": None, "download_url": None}
    m = re.search(r"Version:\s*(\S+)", text)
    if m: info["version"] = m.group(1)
    m = re.search(r"Filename:\s*(\S+)", text)
    if m: info["filename"] = m.group(1)
    m = re.search(r"Size:\s*(\S+)", text)
    if m: info["size"] = m.group(1)
    m = re.search(r"Download URL:\s*(\S+)", text)
    if m: info["download_url"] = m.group(1)
    return info


def run_all():
    with open(MODELS_JSON, "r", encoding="utf-8") as f:
        models = json.load(f)
    results = {}
    for key, model in models.items():
        rc, _ = run_model(key, model)
        # stdout 由 maven 混合输出，真实结果在 run_model 内部已打印到控制台
        results[key] = rc == 0
    log("全部机型运行结束: " + json.dumps(results, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    action = sys.argv[1]
    if action == "build":
        build()
    elif action == "setup":
        sync_sources()
    elif action == "run":
        if len(sys.argv) < 3:
            log("用法: run_source.py run <model_key>")
            sys.exit(1)
        key = sys.argv[2]
        with open(MODELS_JSON, "r", encoding="utf-8") as f:
            models = json.load(f)
        if key not in models:
            log(f"[ERROR] models.json 中无此 key: {key}")
            sys.exit(1)
        run_model(key, models[key])
    elif action == "run-all":
        run_all()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
