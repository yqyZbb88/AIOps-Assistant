#!/usr/bin/env bash
# load_mem.sh —— 在被监控的 Linux 主机（如 CentOS 虚拟机）上模拟高内存占用
#
# 用途：制造真实的高内存使用率，触发 Prometheus 告警 HighMemoryUsage
#       （阈值：内存>90% 持续 3 分钟），从而验证整条链路：
#       Prometheus 规则评估 → Alertmanager → 运维机器人 /alert。
#       内存占用口径 = (1 - MemAvailable/MemTotal) * 100，与告警规则一致。
#
# 用法：
#   bash load_mem.sh             # 默认把内存占用拉到 ~93% 并保持 5 分钟
#   bash load_mem.sh 3           # 保持 3 分钟
#   bash load_mem.sh 3 95        # 拉高到 ~95% 并保持 3 分钟
#   bash load_mem.sh 5 93 200    # 保持 5 分钟、拉到 ~93%、至少保留 200MB 空闲
#
# 依赖：python3（分配匿名内存并保持占用）。
#   没有 python3 时：CentOS 8/9 -> sudo dnf install -y python3
#                   CentOS 7   -> sudo yum install -y python3
#   或改用 stress-ng（同样需安装）：stress-ng --vm 1 --vm-bytes 90% --vm-hang 0 --timeout 300
#
# 安全提示：
# - 内存越小越危险：小内存(如 1~2G)虚拟机拉到 >90% 有触发 OOM 的风险，
#   可能把本机 sshd/node_exporter 杀掉。运行前先 free -h 确认内存量，
#   Ctrl+C 可随时终止并释放全部占用。
# - 若虚拟机内存实在太小，可把告警阈值调低一档再压测（改 alerts.yml 的 expr）。
set -u

MINUTES="${1:-5}"
TARGET_PCT="${2:-93}"
MIN_FREE_MB="${3:-150}"

command -v python3 >/dev/null 2>&1 || {
    echo "错误：未找到 python3。请先安装 python3 或改用 stress-ng："
    echo "  stress-ng --vm 1 --vm-bytes 90% --vm-hang 0 --timeout $((MINUTES * 60))"
    exit 1
}

echo "将在 ${MINUTES} 分钟内把内存占用拉高到 ~${TARGET_PCT}%（保留至少 ${MIN_FREE_MB}MB 空闲），按 Ctrl+C 立即释放。"
echo "（内存占用 = 100 - MemAvailable/MemTotal，与 HighMemoryUsage 告警口径一致）"
echo ""
echo "当前内存："
free -h
echo ""

python3 - "$MINUTES" "$TARGET_PCT" "$MIN_FREE_MB" <<'PY'
import mmap
import sys
import time


def meminfo() -> dict:
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, rest = line.split(":", 1)
            d[key] = int(rest.split()[0]) * 1024  # kB -> bytes
    return d


def usage_pct(total: int, avail: int) -> float:
    return 100 * (1 - avail / total)


minutes = float(sys.argv[1])
target_pct = float(sys.argv[2])
min_free_mb = float(sys.argv[3])

page = 4096
step = 64 * 1024 * 1024        # 每次分配 64MB
allocated = 0                  # 已分配字节
chunks = []
end_time = time.monotonic() + minutes * 60
last_print = 0.0

try:
    # 阶段一：逐步分配匿名内存（每块都逐页触碰，强制计入 RSS），直到达标或兜底线
    while True:
        now = time.monotonic()
        m = meminfo()
        total = m["MemTotal"]
        avail = m["MemAvailable"]
        usage = usage_pct(total, avail)
        if now - last_print >= 1:
            print(f"\r内存占用: {usage:6.1f}%   已分配: {allocated // (1024 * 1024):4d}MB   ", end="", flush=True)
            last_print = now
        if usage >= target_pct or avail <= min_free_mb * 1024 * 1024 or now >= end_time:
            break
        buf = mmap.mmap(-1, step)  # 匿名映射，等价于 malloc
        for off in range(0, step, page):
            buf[off] = 1            # 触碰每一页，强制从“共享零页”变私有，占用真实内存
        chunks.append(buf)
        allocated += step

    # 阶段二：保持占用直到设定时长，让 Prometheus 规则“持续 3 分钟”成立
    while time.monotonic() < end_time:
        m = meminfo()
        usage = usage_pct(m["MemTotal"], m["MemAvailable"])
        remain = int(end_time - time.monotonic())
        print(
            f"\r内存占用: {usage:6.1f}%   保持中: {remain:3d}s   "
            f"已分配: {allocated // (1024 * 1024):4d}MB   (Ctrl+C 立即释放)",
            end="",
            flush=True,
        )
        time.sleep(1)
    print("\n已达设定时长，开始释放。")
finally:
    for c in chunks:
        c.close()
    chunks.clear()
    print("已释放全部占用的内存。")
PY
