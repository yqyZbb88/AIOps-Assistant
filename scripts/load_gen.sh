#!/usr/bin/env bash
# 在独立 Linux 测试主机产生真实 CPU 负载；可单独复制本文件运行。
# bash load_gen.sh       全部可用核心，5 分钟
# bash load_gen.sh 8     全部可用核心，8 分钟
# bash load_gen.sh 6 4   4 个负载进程，6 分钟
# bash load_gen.sh 0     显式不限时，Ctrl+C 结束
# 当前仓库默认告警：CPU >80%，for 30s，使用 2 分钟 rate 窗口。

load_fail() { printf '错误：%s\n' "$*" >&2; exit 2; }
load_integer() {
  local value=$1 minimum=$2 maximum=$3 label=$4
  [[ $value =~ ^[0-9]{1,9}$ ]] || load_fail "$label 必须是整数"
  LOAD_INTEGER=$((10#$value))
  (( LOAD_INTEGER >= minimum && LOAD_INTEGER <= maximum )) || load_fail "$label 必须在 $minimum～$maximum 之间"
}
load_clock() {
  local uptime rest seconds fraction
  read -r uptime rest < /proc/uptime || load_fail '无法读取系统运行时间'
  seconds=${uptime%%.*}; fraction=${uptime#*.}00; fraction=${fraction:0:3}
  LOAD_NOW=$((10#$seconds * 1000 + 10#$fraction))
}
load_sleep() {
  local ms=$1 delay
  (( ms > 0 )) || return 0
  printf -v delay '%d.%03d' "$((ms / 1000))" "$((ms % 1000))"
  sleep "$delay"
}
load_parent() {
  local stat state rest
  { read -r stat < "/proc/$1/stat"; } 2>/dev/null || return 1
  stat=${stat##*) }
  read -r state LOAD_PARENT rest <<< "$stat"
  [[ $state != Z ]]
}
load_worker() {
  local percent=$1 deadline=$2 supervisor=$3 start finish busy_until iteration
  trap 'exit 143' TERM
  while :; do
    load_parent "$BASHPID" || break
    [[ $LOAD_PARENT == "$supervisor" ]] || break
    load_clock
    (( deadline == 0 || LOAD_NOW < deadline )) || break
    start=$LOAD_NOW; busy_until=$((start + percent))
    if (( deadline > 0 && busy_until > deadline )); then busy_until=$deadline; fi
    # 不依赖 seq/head/yes；循环内持续消耗 CPU，并定期检查退出条件。
    while (( LOAD_NOW < busy_until )); do
      for ((iteration=0; iteration<200; iteration++)); do :; done
      load_clock
    done
    finish=$((start + 100))
    if (( deadline > 0 && finish > deadline )); then finish=$deadline; fi
    load_sleep "$((finish - LOAD_NOW))"
  done
}
load_cleanup() {
  local result=$? pid
  trap - EXIT INT TERM
  for pid in "${LOAD_PIDS[@]}"; do
    if load_parent "$pid" && [[ $LOAD_PARENT == "$BASHPID" ]]; then kill -TERM "$pid" 2>/dev/null || :; fi
  done
  sleep 0.1
  for pid in "${LOAD_PIDS[@]}"; do
    if load_parent "$pid" && [[ $LOAD_PARENT == "$BASHPID" ]]; then kill -KILL "$pid" 2>/dev/null || :; fi
    wait "$pid" 2>/dev/null || :
  done
  printf '\n已结束负载，已清理本次启动的进程。\n'
  exit "$result"
}
load_sample() {
  local -a fields
  local index
  read -r -a fields < /proc/stat || load_fail '无法读取 CPU 采样'
  [[ ${fields[0]:-} == cpu && ${#fields[@]} -ge 5 ]] || load_fail 'CPU 采样格式无效'
  LOAD_IDLE=${fields[4]}
  LOAD_TOTAL=0
  # guest/guest_nice 已包含在 user/nice，不能重复相加。idle 与项目 PromQL 定义一致。
  for ((index=1; index<=8 && index<${#fields[@]}; index++)); do
    LOAD_TOTAL=$((LOAD_TOTAL + fields[index]))
  done
}
load_help() {
  cat <<'HELP'
用法：bash load_gen.sh [分钟数] [进程数]
  不填参数：全部可用核心满载 5 分钟（最多 256 个进程）
  bash load_gen.sh 8       全核运行 8 分钟
  bash load_gen.sh 6 4     4 个进程运行 6 分钟
  bash load_gen.sh 0       不限时，Ctrl+C 结束
分钟数范围 0～1440；进程数最多 nproc 的两倍，且不超过 256。
可选参数：
  --duration 秒数  1～86400，与位置参数分钟数不能混用
  --workers 数量  指定进程数，与第二个位置参数不能混用
  --percent 数值  每个进程目标占空比 1～100，默认 100
  --dry-run       显示设置，不产生负载
  --help          查看帮助
HELP
}
load_main() {
  set -euo pipefail
  [[ ${BASH_VERSINFO[0]} -ge 4 && -r /proc/stat && -r /proc/uptime ]] || load_fail '需要 Linux 和 Bash 4+'
  local available maximum workers percent=100 duration=300 dry_run=0 duration_set=0 workers_set=0 option
  local -a positional=()
  command -v nproc >/dev/null && command -v sleep >/dev/null && command -v nice >/dev/null || load_fail '需要 nproc、sleep 和 nice'
  available=$(nproc)
  load_integer "$available" 1 1000000 'CPU 数'
  available=$LOAD_INTEGER; maximum=$((available * 2))
  (( maximum <= 256 )) || maximum=256
  workers=$available
  (( workers <= 256 )) || workers=256
  while (( $# )); do
    option=$1; shift
    case $option in
      --help|-h) load_help; return 0 ;;
      --dry-run) dry_run=1 ;;
      --duration|--workers|--percent)
        (( $# )) || load_fail "$option 缺少值"
        case $option in
          --duration) load_integer "$1" 1 86400 '秒数'; duration=$LOAD_INTEGER; duration_set=1 ;;
          --workers) load_integer "$1" 1 "$maximum" '进程数'; workers=$LOAD_INTEGER; workers_set=1 ;;
          --percent) load_integer "$1" 1 100 '占空比'; percent=$LOAD_INTEGER ;;
        esac
        shift
        ;;
      --*) load_fail "未知参数：$option" ;;
      *) positional+=("$option") ;;
    esac
  done
  (( ${#positional[@]} <= 2 )) || load_fail '最多填写分钟数和进程数两个位置参数'
  if (( ${#positional[@]} >= 1 )); then
    (( duration_set == 0 )) || load_fail '分钟数和 --duration 不能混用'
    load_integer "${positional[0]}" 0 1440 '分钟数'; duration=$((LOAD_INTEGER * 60))
  fi
  if (( ${#positional[@]} == 2 )); then
    (( workers_set == 0 )) || load_fail '进程数位置参数和 --workers 不能混用'
    load_integer "${positional[1]}" 1 "$maximum" '进程数'; workers=$LOAD_INTEGER
  fi
  if (( duration == 0 )); then
    printf '设置：%s 个负载进程，每进程目标 %s%%，不限时；Ctrl+C 结束。\n' "$workers" "$percent"
  else
    printf '设置：%s 个负载进程，每进程目标 %s%%，运行 %s 秒；Ctrl+C 结束。\n' "$workers" "$percent" "$duration"
  fi
  printf '采样来自 /proc/stat，显示整机 CPU 使用率；请在被监控的 Linux 测试机运行。\n'
  if (( dry_run )); then printf '预检通过，未启动负载进程。\n'; return 0; fi
  local script parent_pid=$BASHPID start deadline=0 previous_idle previous_total delta total usage elapsed pid status pause
  script=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/${BASH_SOURCE[0]##*/}
  LOAD_PIDS=()
  trap load_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  load_clock; start=$LOAD_NOW
  (( duration == 0 )) || deadline=$((start + duration * 1000))
  load_sample; previous_idle=$LOAD_IDLE; previous_total=$LOAD_TOTAL
  for ((pid=0; pid<workers; pid++)); do
    nice -n 10 bash -c 'source "$1"; load_worker "$2" "$3" "$4"' _ "$script" "$percent" "$deadline" "$parent_pid" &
    LOAD_PIDS+=("$!")
  done
  printf 'CPU 负载已启动：%s 个进程。\n' "${#LOAD_PIDS[@]}"
  while :; do
    load_clock
    (( deadline == 0 || LOAD_NOW < deadline )) || break
    pause=1000
    if (( deadline > 0 && deadline - LOAD_NOW < pause )); then pause=$((deadline - LOAD_NOW)); fi
    load_sleep "$pause"
    load_sample
    delta=$((LOAD_IDLE - previous_idle)); total=$((LOAD_TOTAL - previous_total)); usage='—'
    if (( total > 0 && delta >= 0 && delta <= total )); then usage=$((100 * (total - delta) / total)); fi
    load_clock; elapsed=$(((LOAD_NOW - start) / 1000))
    if [[ -t 1 ]]; then printf '\rCPU 使用率：%3s%%  已运行：%ss  （Ctrl+C 结束）' "$usage" "$elapsed"
    else printf 'CPU 使用率：%s%%  已运行：%ss\n' "$usage" "$elapsed"; fi
    previous_idle=$LOAD_IDLE; previous_total=$LOAD_TOTAL
    for pid in "${LOAD_PIDS[@]}"; do
      if ! load_parent "$pid" || [[ $LOAD_PARENT != "$BASHPID" ]]; then
        if wait "$pid"; then status=0; else status=$?; fi
        load_clock
        (( status == 0 && deadline > 0 && LOAD_NOW >= deadline )) || load_fail '负载进程提前退出'
      fi
    done
  done
  printf '\n已达到设定时长，自动结束。\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then load_main "$@"; fi
