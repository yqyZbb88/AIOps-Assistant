#!/usr/bin/env bash
# 独立 Linux 测试主机的告警压测；可单独复制本文件运行。
# 默认运行 5 分钟，目标使用率约 90%；Ctrl+C 提前结束。

STRESS_MIB=1048576
STRESS_GIB=1073741824

stress_fail() {
  printf '已停止：%s\n' "$*" >&2
  exit 2
}

stress_uint() {
  local value=$1 minimum=$2 maximum=$3 label=$4
  [[ $value =~ ^[0-9]{1,9}$ ]] || stress_fail "$label 必须是整数"
  value=$((10#$value))
  (( value >= minimum && value <= maximum )) || stress_fail "$label 必须在 $minimum～$maximum 之间"
  STRESS_NUMBER=$value
}

stress_clock() {
  local uptime rest seconds fraction
  read -r uptime rest < /proc/uptime || stress_fail '无法读取系统运行时间'
  seconds=${uptime%%.*}
  fraction=${uptime#*.}00
  fraction=${fraction:0:3}
  [[ $seconds =~ ^[0-9]+$ && $fraction =~ ^[0-9]{3}$ ]] || stress_fail '系统运行时间格式无效'
  STRESS_NOW=$((10#$seconds * 1000 + 10#$fraction))
}

stress_pause_ms() {
  local milliseconds=$1 delay
  (( milliseconds > 0 )) || return 0
  printf -v delay '%d.%03d' "$((milliseconds / 1000))" "$((milliseconds % 1000))"
  sleep "$delay"
}

stress_memory_state() {
  # 参数仅供离线读数测试使用；运行入口始终使用真实 /proc 和标准 cgroup 挂载。
  local meminfo=${1:-/proc/meminfo} groups=${2:-/proc/self/cgroup} mount=${3:-/sys/fs/cgroup}
  local key value unit hierarchy controllers relative root current limit_name used_name raw used limit
  STRESS_MEMORY_TOTAL=0
  STRESS_MEMORY_AVAILABLE=0
  [[ -r $meminfo && -r $groups ]] || stress_fail '无法读取内存或 cgroup 信息'
  while read -r key value unit; do
    case $key in
      MemTotal:|MemAvailable:)
        [[ $value =~ ^[0-9]{1,15}$ && $unit == kB ]] || stress_fail '内存读数无效'
        if [[ $key == MemTotal: ]]; then
          STRESS_MEMORY_TOTAL=$((value * 1024))
        else
          STRESS_MEMORY_AVAILABLE=$((value * 1024))
        fi
        ;;
    esac
  done < "$meminfo"
  (( STRESS_MEMORY_TOTAL > 0 && STRESS_MEMORY_AVAILABLE > 0 )) || stress_fail '缺少可靠的 MemAvailable/MemTotal'
  while IFS=: read -r hierarchy controllers relative; do
    if [[ -z $controllers ]]; then
      root=$mount; limit_name=memory.max; used_name=memory.current
    elif [[ ,$controllers, == *,memory,* ]]; then
      root=$mount/memory; limit_name=memory.limit_in_bytes; used_name=memory.usage_in_bytes
    else
      continue
    fi
    root=$(realpath -m -- "$root") || stress_fail '无法解析 cgroup 根目录'
    current=$(realpath -m -- "$root/${relative#/}") || stress_fail '无法解析 cgroup 目录'
    # 某些容器的 /proc/self/cgroup 带 ../；仍检查当前命名空间挂载根限额。
    [[ $current == "$root" || $current == "$root/"* ]] || current=$root
    while :; do
      if [[ -e $current/$limit_name ]]; then
        raw=$(< "$current/$limit_name") || stress_fail '无法读取 cgroup 内存限额'
        if [[ $raw != max ]]; then
          [[ $raw =~ ^[0-9]{1,19}$ ]] || stress_fail 'cgroup 内存限额无效'
          used=$(< "$current/$used_name") || stress_fail '无法读取 cgroup 已用内存'
          [[ $used =~ ^[0-9]{1,19}$ ]] || stress_fail 'cgroup 已用内存无效'
          limit=$raw
          (( limit > 0 && used >= 0 )) || stress_fail 'cgroup 内存读数溢出或无效'
          (( limit >= STRESS_MEMORY_TOTAL )) || STRESS_MEMORY_TOTAL=$limit
          value=$((limit - used))
          (( value >= 0 )) || value=0
          (( value >= STRESS_MEMORY_AVAILABLE )) || STRESS_MEMORY_AVAILABLE=$value
        fi
      fi
      [[ $current != "$root" ]] || break
      current=${current%/*}
    done
  done < "$groups"
}

stress_require_memory() {
  local request=${1:-0} reserve
  stress_memory_state
  reserve=$((STRESS_MEMORY_TOTAL / 50))
  (( reserve >= 32 * STRESS_MIB )) || reserve=$((32 * STRESS_MIB))
  (( STRESS_MEMORY_AVAILABLE - request >= reserve )) || stress_fail "内存不足，至少保留 $((reserve / STRESS_MIB)) MiB"
}

stress_require_disk() {
  local directory=$1 request=${2:-0} result total free reserve
  result=$(LC_ALL=C df -B1 --output=size,avail -- "$directory") || stress_fail '无法读取磁盘余量'
  result=${result#*$'\n'}
  read -r total free <<< "$result"
  [[ $total =~ ^[0-9]{1,18}$ && $free =~ ^[0-9]{1,18}$ ]] || stress_fail '磁盘余量格式无效'
  STRESS_DISK_TOTAL=$total
  STRESS_DISK_AVAILABLE=$free
  reserve=$((total / 100))
  (( reserve >= 8 * STRESS_MIB )) || reserve=$((8 * STRESS_MIB))
  (( free - request >= reserve )) || stress_fail "磁盘不足，至少保留 $((reserve / STRESS_MIB)) MiB"
}

stress_child_alive() {
  local pid=$1 stat state parent rest
  [[ -r /proc/$pid/stat ]] || return 1
  { read -r stat < "/proc/$pid/stat"; } 2>/dev/null || return 1
  stat=${stat##*) }
  read -r state parent rest <<< "$stat"
  [[ $parent == "$BASHPID" && $state != Z ]]
}

stress_cleanup() {
  local result=$? pid
  trap - EXIT INT TERM
  for pid in "${STRESS_CHILDREN[@]}"; do
    if stress_child_alive "$pid"; then kill -TERM "$pid" 2>/dev/null || :; fi
  done
  if (( ${#STRESS_CHILDREN[@]} > 0 )); then
    sleep 0.1
  fi
  for pid in "${STRESS_CHILDREN[@]}"; do
    # 核对仍是自己的子进程，避免对已经退出并复用的 PID 发信号。
    if stress_child_alive "$pid"; then kill -KILL "$pid" 2>/dev/null || :; fi
    wait "$pid" 2>/dev/null || :
  done
  if [[ -n $STRESS_TEMP_FILE ]]; then
    if [[ $STRESS_TEMP_FILE == "$STRESS_DIRECTORY/aiops-safe-stress-"* ]]; then
      rm -f -- "$STRESS_TEMP_FILE" || result=2
    else
      printf '临时文件路径不符合预期，保留文件供检查\n' >&2
      result=2
    fi
  fi
  printf '负载结束，已清理本次子进程和临时文件（退出码 %s）\n' "$result"
  exit "$result"
}

stress_running() {
  (( STRESS_DEADLINE == 0 || STRESS_NOW < STRESS_DEADLINE ))
}

stress_report() {
  (( STRESS_NOW >= STRESS_NEXT_REPORT )) || return 0
  local elapsed=$(((STRESS_NOW - STRESS_START) / 1000)) usage
  if [[ $STRESS_MODE == memory ]]; then
    usage=$((100 * (STRESS_MEMORY_TOTAL - STRESS_MEMORY_AVAILABLE) / STRESS_MEMORY_TOTAL))
    printf '内存使用率：%s%%  本次占用：%s MiB  已运行：%ss\n' "$usage" "$STRESS_SIZE_MIB" "$elapsed"
  else
    stress_require_disk "$STRESS_DIRECTORY"
    usage=$((100 * (STRESS_DISK_TOTAL - STRESS_DISK_AVAILABLE) / STRESS_DISK_TOTAL))
    printf '磁盘使用率：%s%%  本次文件：%s MiB  已运行：%ss\n' "$usage" "$STRESS_SIZE_MIB" "$elapsed"
  fi
  STRESS_NEXT_REPORT=$((STRESS_NOW + 1000))
}

stress_memory() {
  local block allocated=0 chunk
  local -a blocks=()
  while (( allocated < STRESS_SIZE_MIB )); do
    stress_clock
    stress_running || break
    chunk=$((STRESS_SIZE_MIB - allocated))
    (( chunk <= 8 )) || chunk=8
    stress_require_memory "$(((chunk + 16) * STRESS_MIB))"
    printf -v block '%*s' "$((chunk * STRESS_MIB))" x
    blocks+=("$block")
    allocated=$((allocated + chunk))
  done
  STRESS_SIZE_MIB=$allocated
  printf '已在 Bash 内存中保存 %s MiB 数据；开始保持目标负载\n' "$allocated"
  while :; do
    stress_clock
    stress_running || break
    stress_require_memory
    stress_report
    stress_pause_ms 250
  done
}

stress_help() {
  if [[ $STRESS_MODE == memory ]]; then
    printf '用法：bash load_mem.sh [分钟数=5] [目标内存使用率=90]\n'
  else
    printf '用法：bash load_disk.sh [分钟数=5] [目标磁盘使用率=90] [目录=/var/tmp]\n'
  fi
  printf '  分钟数 0～1440，0 显式不限时；目标使用率 1～95%%。\n'
  printf '  --duration 秒数  1～86400，与分钟数位置参数不能混用\n'
  printf '  --target-percent 目标使用率（不能与第二个位置参数混用）\n'
  printf '  --mib 容量       改为指定 MiB，不自动追求目标使用率\n'
  printf '  --dry-run        只显示计划，不施加负载\n'
  if [[ $STRESS_MODE == disk ]]; then printf '  --directory 路径；--rate-mib 1～32 限速（不填则快速预分配）\n'; fi
}

stress_main() {
  set -euo pipefail
  STRESS_MODE=$1
  shift
  [[ ${BASH_VERSINFO[0]} -ge 4 && -r /proc/uptime && -r /proc/self/cgroup ]] || stress_fail '需要 Linux 和 Bash 4+'
  [[ $STRESS_MODE == memory || $STRESS_MODE == disk ]] || stress_fail '未知负载类型'
  STRESS_DURATION=300 STRESS_TARGET=90 STRESS_RATE_MIB=8 STRESS_RATE_SET=0
  STRESS_DIRECTORY=/var/tmp STRESS_TEMP_FILE='' STRESS_DRY_RUN=0 STRESS_EXPLICIT_SIZE=0
  STRESS_SIZE_MIB=0 STRESS_CHILDREN=()
  local option maximum tool duration_set=0 target_set=0 directory_set=0 needed fs
  local -a positional=()
  while (( $# > 0 )); do
    option=$1; shift
    case $option in
      --help|-h) stress_help; return 0 ;;
      --dry-run) STRESS_DRY_RUN=1; continue ;;
      --duration|--target-percent|--mib|--directory|--rate-mib) (( $# > 0 )) || stress_fail "$option 缺少值" ;;
      --*) stress_fail "未知参数：$option" ;;
      *) positional+=("$option"); continue ;;
    esac
    case $option in
      --duration) stress_uint "$1" 1 86400 "$option"; STRESS_DURATION=$STRESS_NUMBER; duration_set=1 ;;
      --target-percent) stress_uint "$1" 1 95 "$option"; STRESS_TARGET=$STRESS_NUMBER; target_set=1 ;;
      --mib)
        stress_uint "$1" 1 1048576 "$option"; STRESS_SIZE_MIB=$STRESS_NUMBER; STRESS_EXPLICIT_SIZE=1
        ;;
      --directory) [[ $STRESS_MODE == disk ]] || stress_fail '--directory 仅用于磁盘'; STRESS_DIRECTORY=$1; directory_set=1 ;;
      --rate-mib) [[ $STRESS_MODE == disk ]] || stress_fail '--rate-mib 仅用于磁盘'; stress_uint "$1" 1 32 "$option"; STRESS_RATE_MIB=$STRESS_NUMBER; STRESS_RATE_SET=1 ;;
    esac
    shift
  done
  maximum=2; [[ $STRESS_MODE != disk ]] || maximum=3
  (( ${#positional[@]} <= maximum )) || stress_fail '位置参数过多'
  if (( ${#positional[@]} >= 1 )); then
    (( duration_set == 0 )) || stress_fail '分钟数和 --duration 不能混用'
    stress_uint "${positional[0]}" 0 1440 '分钟数'; STRESS_DURATION=$((STRESS_NUMBER * 60))
  fi
  if (( ${#positional[@]} >= 2 )); then
    (( target_set == 0 && STRESS_EXPLICIT_SIZE == 0 )) || stress_fail '目标使用率参数不能混用'
    stress_uint "${positional[1]}" 1 95 '目标使用率'; STRESS_TARGET=$STRESS_NUMBER; target_set=1
  fi
  if (( ${#positional[@]} == 3 )); then
    (( directory_set == 0 )) || stress_fail '目录参数不能混用'
    STRESS_DIRECTORY=${positional[2]}
  fi
  (( STRESS_EXPLICIT_SIZE == 0 || target_set == 0 )) || stress_fail '--mib 和目标使用率不能混用'
  for tool in realpath sleep; do command -v "$tool" >/dev/null || stress_fail "缺少工具：$tool"; done
  if [[ $STRESS_MODE == memory ]]; then
    stress_memory_state
    if (( STRESS_EXPLICIT_SIZE == 0 )); then
      needed=$((STRESS_MEMORY_AVAILABLE - STRESS_MEMORY_TOTAL * (100 - STRESS_TARGET) / 100))
      (( needed >= 0 )) || needed=0
      STRESS_SIZE_MIB=$(((needed + STRESS_MIB - 1) / STRESS_MIB))
    fi
    stress_require_memory "$((STRESS_SIZE_MIB * STRESS_MIB + 32 * STRESS_MIB))"
  else
    for tool in df dd mktemp rm stat; do command -v "$tool" >/dev/null || stress_fail "缺少工具：$tool"; done
    STRESS_DIRECTORY=$(realpath -e -- "$STRESS_DIRECTORY") || stress_fail '目录不存在'
    [[ -d $STRESS_DIRECTORY && -w $STRESS_DIRECTORY ]] || stress_fail '目标必须是已有可写目录'
    stress_require_disk "$STRESS_DIRECTORY"
    if (( STRESS_EXPLICIT_SIZE == 0 )); then
      fs=$(stat -f -c %T -- "$STRESS_DIRECTORY") || stress_fail '无法读取文件系统类型'
      [[ $fs != tmpfs && $fs != overlayfs && $fs != overlay ]] || stress_fail '默认磁盘告警不统计 tmpfs/overlay，请选择实际 Linux 磁盘上的目录'
      needed=$((STRESS_DISK_AVAILABLE - STRESS_DISK_TOTAL * (100 - STRESS_TARGET) / 100))
      (( needed >= 0 )) || needed=0
      STRESS_SIZE_MIB=$(((needed + STRESS_MIB - 1) / STRESS_MIB))
    else
      # 小容量测试可在 tmpfs 运行，但需预算其真实内存占用。
      stress_require_memory "$((STRESS_SIZE_MIB * STRESS_MIB + 32 * STRESS_MIB))"
    fi
    stress_require_disk "$STRESS_DIRECTORY" "$((STRESS_SIZE_MIB * STRESS_MIB))"
  fi
  printf '模式=%s，目标=%s%%，计划占用=%s MiB，时限=%ss（0 表示不限时）\n' "$STRESS_MODE" "$STRESS_TARGET" "$STRESS_SIZE_MIB" "$STRESS_DURATION"
  if (( STRESS_DRY_RUN )); then printf '预检通过，未产生负载\n'; return 0; fi
  trap stress_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  stress_clock
  STRESS_START=$STRESS_NOW STRESS_NEXT_REPORT=$STRESS_NOW STRESS_DEADLINE=0
  (( STRESS_DURATION == 0 )) || STRESS_DEADLINE=$((STRESS_NOW + STRESS_DURATION * 1000))
  "stress_$STRESS_MODE"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then stress_main memory "$@"; fi
