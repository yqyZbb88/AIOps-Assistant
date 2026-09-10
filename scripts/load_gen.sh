#!/usr/bin/env bash
# load_gen.sh —— 在被监控的 Linux 主机（如 CentOS 虚拟机）上模拟高 CPU 负载
#
# 用途：制造真实的高 CPU 使用率，触发 Prometheus 告警 HighCPUUsage
#       （阈值：CPU>85% 持续 3 分钟），从而验证整条链路：
#       Prometheus 规则评估 → Alertmanager → 运维机器人 /alert。
#
# 用法：
#   bash load_gen.sh            # 全部核心满载 5 分钟（默认）
#   bash load_gen.sh 8          # 全部核心满载 8 分钟
#   bash load_gen.sh 6 4        # 占 4 个核，跑 6 分钟
#   bash load_gen.sh 0          # 不限时，按 Ctrl+C 结束
#
# 说明：
# - 高 CPU 告警需「>85% 持续 3 分钟」，建议运行时长 >= 5 分钟（默认），
#   触发后还剩约 2 分钟观察窗；若机器核少或共享配额跑不满 85%，
#   可超配核数：bash load_gen.sh 5 <核数x2>；
# - 内存告警 HighMemoryUsage 的压测见 scripts/load_mem.sh；
# - 若想触发 HighDiskUsage（磁盘>90%），不要对真实磁盘写满（有风险），
#   可挂一个一次性小文件系统再填满，本脚本不包含该操作。
set -u

MINUTES="${1:-5}"
CORES="${2:-$(nproc)}"

if [ "$MINUTES" = "0" ]; then
    echo "不限时运行（${CORES} 核满载），按 Ctrl+C 结束。"
else
    echo "将在 ${MINUTES} 分钟内用 ${CORES} 个核制造高 CPU 负载…"
fi

# 每个核启动一个死循环进程，吃满对应 CPU
pids=()
for _ in $(seq 1 "$CORES"); do
    ( while :; do :; done ) &
    pids+=("$!")
done
echo "已启动 ${#pids[@]} 个负载进程。"

cleanup() {
    kill "${pids[@]}" 2>/dev/null
    wait 2>/dev/null
    echo ""
    echo "已结束负载，负载进程已清理。"
    exit 0
}
trap cleanup INT TERM

# 从 /proc/stat 采样 CPU 使用率（Linux）
get_cpu() {
    read -r -a arr <<<"$(head -n1 /proc/stat)"
    # arr[0]="cpu"；arr[1..] 依次是 user nice system idle iowait irq softirq ...
    local idle=${arr[4]}
    local total=0 v
    for v in "${arr[@]:1}"; do
        total=$((total + v))
    done
    echo "$idle $total"
}

prev=$(get_cpu)
prev_idle=${prev% *}
prev_total=${prev#* }
elapsed=0

while :; do
    sleep 1
    cur=$(get_cpu)
    cur_idle=${cur% *}
    cur_total=${cur#* }
    idle_delta=$((cur_idle - prev_idle))
    total_delta=$((cur_total - prev_total))
    if [ "$total_delta" -gt 0 ]; then
        usage=$((100 * (total_delta - idle_delta) / total_delta))
    else
        usage=100
    fi
    printf "\rCPU 使用率: %3d%%   已运行: %ds    (按 Ctrl+C 结束)" "$usage" "$elapsed"

    prev_idle=$cur_idle
    prev_total=$cur_total
    elapsed=$((elapsed + 1))

    # 限时模式：到时自动结束
    if [ "$MINUTES" != "0" ] && [ "$elapsed" -ge $((MINUTES * 60)) ]; then
        echo ""
        echo "已达设定时长，自动结束。"
        cleanup
    fi
done
