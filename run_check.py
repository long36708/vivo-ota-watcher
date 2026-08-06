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


def detect_platform():
    """返回 (arch_key, [需要抽取的 native 文件名, ...])。

    arch_key 对应 jar 内 natives/<arch_key>/ 目录名。

    需要两个文件：
      - unicorn_java : JNI 桥接层，System.loadLibrary("unicorn_java") 的目标
      - unicorn      : 核心引擎，被前者动态链接依赖
    """
    import platform
    system = platform.system().lower()   # linux / windows / darwin
    machine = platform.machine().lower()

    table = {
        ("linux", "x86_64"):   ("linux_64",    ["libunicorn_java.so", "libunicorn.so"]),
        ("linux", "amd64"):    ("linux_64",    ["libunicorn_java.so", "libunicorn.so"]),
        ("linux", "aarch64"):  ("linux_arm64", ["libunicorn_java.so", "libunicorn.so"]),
        ("linux", "arm64"):    ("linux_arm64", ["libunicorn_java.so", "libunicorn.so"]),
        ("windows", "amd64"):  ("windows_64",  ["unicorn_java.dll", "unicorn.dll"]),
        ("windows", "x86_64"): ("windows_64",  ["unicorn_java.dll", "unicorn.dll"]),
        ("darwin", "x86_64"):  ("osx_64",      ["libunicorn_java.dylib", "libunicorn.dylib"]),
        ("darwin", "arm64"):   ("osx_arm64",   ["libunicorn_java.dylib", "libunicorn.dylib"]),
    }
    hit = table.get((system, machine))
    if hit is None:
        log(f"[WARN] 当前平台 {system}/{machine} 未在支持表中，unicorn native 可能无法加载。")
        return None, []
    return hit


# libunicorn_java.so 引用但 jar 内 libunicorn.so 未提供的 TCG 符号。
# 只要缺其一，System.loadLibrary("unicorn_java") 就会报
# "undefined symbol: helper_div_i32"。
_TCG_HELPERS = [
    "helper_div_i32", "helper_div_i64", "helper_divu_i32", "helper_divu_i64",
    "helper_rem_i32", "helper_rem_i64", "helper_remu_i32", "helper_remu_i64",
    "helper_mulsh_i64", "helper_muluh_i64",
    "helper_sar_i64", "helper_shl_i64", "helper_shr_i64",
]


def _elf_dynsyms(path_or_bytes):
    """解析 ELF64 的 .dynsym，返回 {符号名: st_shndx}。非 ELF 返回 None。"""
    import struct
    if isinstance(path_or_bytes, bytes):
        data = path_or_bytes
    else:
        try:
            with open(path_or_bytes, "rb") as f:
                data = f.read()
        except OSError:
            return None
    if len(data) < 0x40 or data[:4] != b"\x7fELF" or data[4] != 2:
        return None
    try:
        e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
        e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
        e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
        secs = []
        for i in range(e_shnum):
            f = struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + i * e_shentsize)
            secs.append(dict(type=f[1], offset=f[4], size=f[5],
                             link=f[6], entsize=f[9]))
        out = {}
        for s in secs:
            if s["type"] != 11 or not s["entsize"]:   # SHT_DYNSYM
                continue
            strtab = secs[s["link"]]
            for i in range(s["size"] // s["entsize"]):
                off = s["offset"] + i * s["entsize"]
                st_name, _i, _o, st_shndx = struct.unpack_from("<IBBH", data, off)
                b = data[strtab["offset"] + st_name:]
                nm = b[:b.index(b"\0")].decode(errors="replace")
                if nm:
                    out[nm] = st_shndx
        return out
    except Exception:
        return None


def find_system_unicorn():
    """在系统库路径中查找提供 TCG helper 符号的 libunicorn.so。

    仅 Linux 需要：jar 内 natives/linux_64/ 的两个 .so 版本不匹配，
    libunicorn_java.so 是纯 JNI 薄壳，其引用的 13 个 TCG helper 符号
    在同目录 libunicorn.so 中并不存在，必须由系统 unicorn 提供。
    （Windows 的 unicorn_java.dll 为自包含构建，不受影响。）
    """
    import glob
    cands = []
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
              "/usr/lib64", "/usr/lib", "/usr/local/lib"):
        cands.extend(glob.glob(os.path.join(d, "libunicorn.so*")))

    for p in cands:
        syms = _elf_dynsyms(p)
        if not syms:
            continue
        if all(syms.get(h, 0) != 0 for h in _TCG_HELPERS):
            return p
    return None


def extract_unicorn(work_dir):
    """准备 unicorn native 到 work_dir，返回成功就绪的文件数。

    背景一（找不到文件）：unicorn.Unicorn.<clinit> 调用
    NativeLoader.loadLibrary("unicorn_java")，流程是「先 System.loadLibrary，
    失败再从 jar 抽取」。抽取路径由 scijava MxSysInfo 推导：Linux 下它读
    /lib/libc.so.6 并用正则 `.*/libc-(\\d+)\\.(\\d+)\\..*` 解析 glibc 版本，
    拼成 natives/linux-x86_64-cxx*-glibc* 这类目录名——与 jar 内实际的
    natives/linux_64/ 对不上，且 glibc 2.34+ 已不用 libc-2.xx.so 命名，
    正则直接失配。故两步皆失败 → "no unicorn_java in java.library.path"。
    对策：自己抽取到 java.library.path，让第一步直接命中。

    背景二（符号缺失）：抽取成功后 Linux 仍报
    "undefined symbol: helper_div_i32"。经 ELF 分析，jar 内
    linux_64/libunicorn_java.so 仅 1.8MB 且 NEEDED 只有 librt/libc，
    是纯 JNI 薄壳，13 个 TCG helper 符号全部未定义；而同目录
    libunicorn.so 虽有 5236 个符号却恰好不含这批 helper——上游打包的
    两个 .so 版本不匹配。对策：优先用系统安装的 libunicorn.so 覆盖。

    优先级：libs/ 手动放置 > 系统 unicorn(仅Linux) > jar 内嵌。
    """
    import zipfile

    arch_key, fnames = detect_platform()
    if not arch_key:
        return 0

    is_linux = sys.platform.startswith("linux")
    sys_unicorn = find_system_unicorn() if is_linux else None
    if is_linux:
        if sys_unicorn:
            log(f"[INFO] 使用系统 unicorn: {sys_unicorn}")
        else:
            log("[WARN] 未找到含 TCG helper 的系统 libunicorn.so，"
                "将回退 jar 内嵌（Linux 下大概率报 undefined symbol）。"
                "请安装: sudo apt-get install -y libunicorn2")

    ready = 0
    with zipfile.ZipFile(JAR_PATH) as z:
        names = set(z.namelist())
        for fname in fnames:
            dst = os.path.join(work_dir, fname)
            override = os.path.join(LIBS_DIR, fname)

            if os.path.exists(override):
                # 1) libs/ 手动覆盖优先
                shutil.copyfile(override, dst)
                log(f"[INFO] 使用 libs/ 下的 {fname}")
            elif sys_unicorn and fname == "libunicorn.so":
                # 2) 系统 unicorn 替换掉版本不匹配的 jar 内嵌版
                shutil.copyfile(sys_unicorn, dst)
            else:
                # 3) 从 jar 内抽取
                member = f"natives/{arch_key}/{fname}"
                if member not in names:
                    avail = sorted(n for n in names
                                   if n.startswith(f"natives/{arch_key}/"))
                    log(f"[WARN] jar 内缺少 {member}，该目录现有: {avail}")
                    continue
                with z.open(member) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)

            if os.name != "nt":
                os.chmod(dst, 0o755)
            ready += 1

    if is_linux:
        _verify_symbols(work_dir)
    return ready


def _verify_symbols(work_dir):
    """加载前自检 TCG 符号，把失败原因提前暴露成可读信息。"""
    bridge = os.path.join(work_dir, "libunicorn_java.so")
    core = os.path.join(work_dir, "libunicorn.so")
    bs, cs = _elf_dynsyms(bridge), _elf_dynsyms(core)
    if not bs or not cs:
        return
    need = [h for h in _TCG_HELPERS if bs.get(h, 0) == 0]
    missing = [h for h in need if cs.get(h, 0) == 0]
    if missing:
        log(f"[ERROR] libunicorn.so 缺少 {len(missing)}/{len(need)} 个 TCG 符号，"
            f"例如 {missing[:3]}。java 侧将报 undefined symbol。")
    elif need:
        log(f"[INFO] TCG 符号自检通过（{len(need)} 个由 libunicorn.so 提供）。")


def prepare_work_dir(model):
    """准备 unidbg 工作目录，返回 (work_dir, jar_copy, lib_path)。

    - 拷贝 jar。
    - 拷贝 libs/（含 libvivoseckey.so，供 unidbg 在 ARM 模拟环境中加载）。
    - 抽取平台对应的 unicorn native 到 work_dir 根目录。
    - java.library.path 指向 work_dir。
    """
    work_dir = tempfile.mkdtemp(prefix="vivo_ota_")

    jar_copy = os.path.join(work_dir, JAR_NAME)
    shutil.copyfile(JAR_PATH, jar_copy)

    dst_libs = os.path.join(work_dir, "libs")
    if os.path.isdir(LIBS_DIR):
        shutil.copytree(LIBS_DIR, dst_libs)

    if extract_unicorn(work_dir) == 0:
        log("[WARN] 未能准备 unicorn native，java 侧大概率报 ExceptionInInitializerError。")

    return work_dir, jar_copy, work_dir


def build_env(work_dir):
    """构造子进程环境变量。

    把 work_dir 加入 LD_LIBRARY_PATH(Linux) / DYLD_LIBRARY_PATH(macOS)。
    注意 linux_64/libunicorn_java.so 的 NEEDED 里其实**没有** libunicorn.so，
    它期望 TCG 符号已在全局符号表中；设置该变量是为了让 libunicorn.so
    能被解析到，并兼容其他平台确有 DT_NEEDED 依赖的情形。
    Windows 下 dll 同目录即可找到，无需处理。
    """
    env = os.environ.copy()
    keys = []
    if sys.platform.startswith("linux"):
        keys = ["LD_LIBRARY_PATH"]
        # libunicorn_java.so 的 TCG 符号需在**全局**符号表中可见，
        # 而 System.loadLibrary 走 RTLD_LOCAL 不会导出符号。
        # 用 LD_PRELOAD 让 libunicorn.so 以全局方式先于 JVM 装载。
        core = os.path.join(work_dir, "libunicorn.so")
        if os.path.exists(core):
            env["LD_PRELOAD"] = os.pathsep.join(
                filter(None, [core, env.get("LD_PRELOAD", "")]))
    elif sys.platform == "darwin":
        keys = ["DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"]
    for k in keys:
        env[k] = os.pathsep.join(filter(None, [work_dir, env.get(k, "")]))
    return env


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


def summarize_java_error(stderr, limit=4000):
    """整理 java stderr。

    直接截前 N 字符常会把 `Caused by:` 根因切掉（这正是
    ExceptionInInitializerError 难排查的原因），所以这里把
    根因行单独提出来放最前面。
    """
    if not stderr:
        return "(stderr 为空)"
    stderr = stderr.strip()

    # 过滤 SLF4J 噪音
    lines = [ln for ln in stderr.splitlines()
             if not ln.startswith("SLF4J")]

    # 提取关键根因行
    key = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if (s.startswith("Caused by:")
                or "UnsatisfiedLinkError" in s
                or "NoClassDefFoundError" in s
                or "ExceptionInInitializerError" in s
                or "java.library.path" in s):
            key.append(ln)
            # 带上紧随其后的第一行调用栈，便于定位
            if i + 1 < len(lines):
                key.append(lines[i + 1])

    out = []
    if key:
        out.append(">>> 关键错误 <<<")
        out.extend(dict.fromkeys(key))  # 去重且保序
        out.append(">>> 完整 stderr <<<")
    out.extend(lines)

    text = "\n".join(out)
    return text[:limit] + ("\n...(已截断)" if len(text) > limit else "")


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
            env=build_env(work_dir),
        )
    except subprocess.TimeoutExpired:
        log(f"  查询超时: {name}")
        return "error", {"reason": "timeout", "detail": "java process timeout (300s)"}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if proc.returncode != 0:
        detail = summarize_java_error(proc.stderr)
        log(f"  Java 进程异常退出 (code={proc.returncode}):\n{detail}")
        return "error", {"reason": "nonzero_exit", "code": proc.returncode,
                         "stderr": detail}

    result = parse_ota_result(proc.stdout)
    if result is None:
        log(f"  未解析到结果标记。\n--- stdout ---\n{proc.stdout[:2000]}\n"
            f"--- stderr ---\n{summarize_java_error(proc.stderr)}")
        return "error", {"reason": "no_result_marker",
                         "stdout": proc.stdout[:2000],
                         "stderr": summarize_java_error(proc.stderr)}
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

    # 前置校验 unicorn native，避免每个机型都跑到 java 才失败
    arch_key, uni_names = detect_platform()
    if arch_key is None:
        log("当前平台不受支持，无法加载 unicorn native。")
        sys.exit(1)
    import zipfile
    with zipfile.ZipFile(JAR_PATH) as z:
        names = set(z.namelist())
    missing = [n for n in uni_names
               if f"natives/{arch_key}/{n}" not in names
               and not os.path.exists(os.path.join(LIBS_DIR, n))]
    if missing:
        log(f"jar 内 natives/{arch_key}/ 缺少 {missing}，且 libs/ 下也没有。")
        sys.exit(1)
    log(f"[INFO] 平台 {arch_key}，unicorn native: {', '.join(uni_names)}")

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
