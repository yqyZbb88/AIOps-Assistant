#!/usr/bin/env bash
# 默认仅检查参数和合成资源读数；--smoke 只用于资源受限的 Linux 测试容器。
set -euo pipefail
test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
script_dir=$(cd -- "$test_dir/../scripts" && pwd)
source "$script_dir/load_mem.sh"
source "$script_dir/load_disk.sh"
test_root=$(mktemp -d /tmp/aiops-stress-tests-XXXXXXXX)
test_count=0
test_pid=''
trap 'if [[ -n $test_pid ]]; then kill -TERM "$test_pid" 2>/dev/null || :; wait "$test_pid" 2>/dev/null || :; fi; rm -f -- "$test_root"/*; rmdir -- "$test_root"' EXIT

pass() { test_count=$((test_count + 1)); printf 'PASS %s: %s\n' "$test_count" "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
expect_failure() {
  local status
  if "$@" > "$test_root/output" 2>&1; then fail "意外成功：$*"; else status=$?; fi
  [[ $status == 2 ]] || fail "意外退出码 $status：$*"
  pass "拒绝 $*"
}

for entry in load_gen load_mem load_disk; do
  bash -n "$script_dir/$entry.sh"
done
pass '三个独立 Bash 文件语法'
for mode in gen mem disk; do
  bash "$script_dir/load_$mode.sh" --help > "$test_root/output"
  expect_failure bash "$script_dir/load_$mode.sh" --duration 0
  expect_failure bash "$script_dir/load_$mode.sh" --duration 86401
  expect_failure bash "$script_dir/load_$mode.sh" --duration
  expect_failure bash "$script_dir/load_$mode.sh" --not-an-option
done
available=$(nproc)
maximum=$((available * 2))
(( maximum <= 256 )) || maximum=256
expect_failure bash "$script_dir/load_gen.sh" --workers "$((maximum + 1))"
expect_failure bash "$script_dir/load_gen.sh" --workers 0
expect_failure bash "$script_dir/load_gen.sh" --percent 101
expect_failure bash "$script_dir/load_gen.sh" --percent 1.5
expect_failure bash "$script_dir/load_mem.sh" --mib 1048577
expect_failure bash "$script_dir/load_disk.sh" --mib 1048577
expect_failure bash "$script_dir/load_disk.sh" --rate-mib 33
expect_failure bash "$script_dir/load_disk.sh" --rate-mib 0
expect_failure bash "$script_dir/load_mem.sh" --workers 1
expect_failure bash "$script_dir/load_gen.sh" --mib 1
expect_failure bash "$script_dir/load_disk.sh" --mib 999999999999999999999999
bash "$script_dir/load_gen.sh" --workers 01 --percent 01 --duration 01 --dry-run > "$test_root/output"
pass '预检不产生负载，十进制前导零参数可用'
bash "$script_dir/load_gen.sh" --dry-run > "$test_root/output"
grep -q '目标 100%，运行 300 秒' "$test_root/output" || fail 'CPU 默认不是满载五分钟'
pass 'CPU 默认满载五分钟，只做预检'
bash "$script_dir/load_gen.sh" 8 1 --dry-run > "$test_root/output"
grep -q '运行 480 秒' "$test_root/output" || fail 'CPU 分钟数位置参数无效'
pass '参考脚本的分钟数与进程数位置参数'
bash "$script_dir/load_gen.sh" 0 1 --dry-run > "$test_root/output"
grep -q '不限时' "$test_root/output" || fail '显式不限时参数无效'
pass '显式 0 分钟不限时，只做预检'
expect_failure bash "$script_dir/load_gen.sh" -1
expect_failure bash "$script_dir/load_gen.sh" 1441
expect_failure bash "$script_dir/load_gen.sh" 5 --duration 1
expect_failure bash "$script_dir/load_gen.sh" 5 1 --workers 1
if stress_child_alive "$BASHPID"; then fail '误把自身识别为子进程'; fi
pass '清理前必须确认进程属于本次启动的子进程'

(
  stress_memory_state() { STRESS_MEMORY_TOTAL=$((1024 * STRESS_MIB)); STRESS_MEMORY_AVAILABLE=$((80 * STRESS_MIB)); }
  stress_require_memory "$((40 * STRESS_MIB))"
)
pass '内存余量充足可通过'
expect_failure bash -c 'source "$1"; stress_memory_state() { STRESS_MEMORY_TOTAL=$((1024 * STRESS_MIB)); STRESS_MEMORY_AVAILABLE=$((80 * STRESS_MIB)); }; stress_require_memory "$((50 * STRESS_MIB))"' _ "$script_dir/load_disk.sh"
expect_failure bash -c 'source "$1"; stress_memory_state() { STRESS_MEMORY_TOTAL=$((16 * STRESS_GIB)); STRESS_MEMORY_AVAILABLE=$((128 * STRESS_MIB)); }; stress_require_memory' _ "$script_dir/load_disk.sh"
expect_failure bash -c 'source "$1"; df() { printf "Size Avail\n5368709120 8388608\n"; }; stress_require_disk /tmp 1048576' _ "$script_dir/load_disk.sh"
expect_failure bash -c 'source "$1"; df() { printf "Size Avail\n107374182400 1073741824\n"; }; stress_require_disk /tmp 1048576' _ "$script_dir/load_disk.sh"

# 在临时树内合成 cgroup v1/v2 层级，验证祖先组的更小余量也能被识别。
for version in 1 2; do
  fixture=$test_root/cgroup-$version
  mkdir -p "$fixture/group/child"
  printf 'MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\n' > "$test_root/meminfo"
  if [[ $version == 2 ]]; then
    printf '0::/group/child\n' > "$test_root/cgroups"
    limit_name=memory.max; used_name=memory.current; mount=$fixture
  else
    printf '5:memory:/group/child\n' > "$test_root/cgroups"
    limit_name=memory.limit_in_bytes; used_name=memory.usage_in_bytes
    mkdir -p "$fixture/memory/group/child"
    mount=$fixture/memory
  fi
  printf '2147483648\n' > "$mount/group/child/$limit_name"
  printf '1073741824\n' > "$mount/group/child/$used_name"
  printf '3221225472\n' > "$mount/group/$limit_name"
  printf '3087007744\n' > "$mount/group/$used_name"
  stress_memory_state "$test_root/meminfo" "$test_root/cgroups" "$fixture"
  [[ $STRESS_MEMORY_TOTAL == 2147483648 && $STRESS_MEMORY_AVAILABLE == 134217728 ]] || fail "cgroup v$version 余量错误"
  pass "cgroup v$version 当前组与祖先限额"
  # 只清理测试脚本创建的固定目录树；不在产品脚本中递归删除目录。
  rm -f -- "$mount/group/child/$limit_name" "$mount/group/child/$used_name" "$mount/group/$limit_name" "$mount/group/$used_name"
  rmdir "$mount/group/child" "$mount/group"
  if [[ $version == 1 ]]; then rmdir "$fixture/memory" "$fixture/group/child" "$fixture/group"; fi
  rmdir "$fixture"
done
printf 'MemTotal: 8388608 kB\n' > "$test_root/meminfo"
expect_failure bash -c 'source "$1"; stress_memory_state "$2" "$3" "$4"' _ "$script_dir/load_disk.sh" "$test_root/meminfo" "$test_root/cgroups" "$test_root"

# 默认目标按当前余量计算；这里只使用合成读数，不分配内存或磁盘。
(
  source "$script_dir/load_mem.sh"
  stress_memory_state() { STRESS_MEMORY_TOTAL=$((1024 * STRESS_MIB)); STRESS_MEMORY_AVAILABLE=$((800 * STRESS_MIB)); }
  stress_main memory --dry-run > "$test_root/output"
  grep -q '目标=90%，计划占用=698 MiB，时限=300s' "$test_root/output" || fail '内存默认目标计算错误'
  stress_main memory 0 85 --dry-run > "$test_root/output"
  grep -q '目标=85%，计划占用=647 MiB，时限=0s' "$test_root/output" || fail '内存位置参数错误'
)
pass '内存默认目标与不限时位置参数'
(
  source "$script_dir/load_disk.sh"
  df() { printf 'Size Avail\n1073741824 838860800\n'; }
  stat() { printf 'ext2/ext3\n'; }
  stress_main disk --directory "$test_root" --dry-run > "$test_root/output"
  grep -q '目标=90%，计划占用=698 MiB，时限=300s' "$test_root/output" || fail '磁盘默认目标计算错误'
)
pass '磁盘默认目标计算'
for mode in mem disk; do
  expect_failure bash "$script_dir/load_$mode.sh" 1441
  expect_failure bash "$script_dir/load_$mode.sh" 5 96
  expect_failure bash "$script_dir/load_$mode.sh" 5 --duration 1
  expect_failure bash "$script_dir/load_$mode.sh" --mib 1 --target-percent 90
done

if [[ ${1:-} == --smoke ]]; then
  scratch=/scratch
  [[ -d $scratch ]] || fail '--smoke 需要测试容器的 /scratch 临时挂载'
  printf 'keep\n' > "$scratch/existing-data.txt"
  for mode in gen mem disk; do
    case $mode in
      gen) options=(--workers 1 --percent 10) ;;
      mem) options=(--mib 1) ;;
      disk) options=(--mib 1 --directory "$scratch") ;;
    esac
    bash "$script_dir/load_$mode.sh" "${options[@]}" --duration 1 > "$test_root/output"
    [[ $(< "$scratch/existing-data.txt") == keep ]] || fail '已有文件被修改'
    if compgen -G "$scratch/aiops-safe-stress-*.tmp" >/dev/null; then fail '临时文件未清理'; fi
    pass "$mode 小负载自动退出与文件清理"
    bash "$script_dir/load_$mode.sh" "${options[@]}" --duration 30 > "$test_root/output" 2>&1 &
    test_pid=$!
    for ((attempt=0; attempt<160; attempt++)); do
      if grep -qE 'CPU 负载已启动|已在 Bash 内存中保存|已写入' "$test_root/output"; then break; fi
      kill -0 "$test_pid" 2>/dev/null || fail "$mode 提前退出"
      sleep 0.05
    done
    (( attempt < 160 )) || fail "$mode 未进入工作状态"
    kill -TERM "$test_pid"
    if wait "$test_pid"; then status=0; else status=$?; fi
    test_pid=''
    [[ $status == 143 ]] || fail "$mode SIGTERM 退出码为 $status"
    if compgen -G "$scratch/aiops-safe-stress-*.tmp" >/dev/null; then fail '中断后临时文件未清理'; fi
    pass "$mode SIGTERM 提前退出"
  done
  expect_failure bash "$script_dir/load_mem.sh" --target-percent 95 --dry-run
  expect_failure bash "$script_dir/load_disk.sh" --directory /tmp --mib 64 --dry-run
  expect_failure bash "$script_dir/load_disk.sh" --directory "$scratch/no-such-directory" --mib 1 --dry-run

  bash "$script_dir/load_mem.sh" --mib 8 --duration 30 > "$test_root/output" 2>&1 &
  test_pid=$!
  for ((attempt=0; attempt<160; attempt++)); do
    if grep -q '已在 Bash 内存中保存 8 MiB' "$test_root/output"; then break; fi
    kill -0 "$test_pid" 2>/dev/null || fail '内存驻留测试提前退出'
    sleep 0.05
  done
  (( attempt < 160 )) || fail '内存驻留测试未就绪'
  resident=0
  while read -r key value unit; do
    [[ $key != VmRSS: ]] || resident=$value
  done < "/proc/$test_pid/status"
  (( resident >= 8192 )) || fail "8 MiB 数据没有产生预期的驻留内存：$resident KiB"
  kill -TERM "$test_pid"
  if wait "$test_pid"; then status=0; else status=$?; fi
  test_pid=''
  [[ $status == 143 ]] || fail '内存驻留测试未正常中断'
  pass 'Bash 数组中的 8 MiB 数据实际驻留内存'

  bash "$script_dir/load_gen.sh" --workers 1 --percent 10 --duration 30 > "$test_root/output" 2>&1 &
  test_pid=$!
  for ((attempt=0; attempt<160; attempt++)); do
    if grep -q 'CPU 负载已启动' "$test_root/output"; then break; fi
    sleep 0.05
  done
  (( attempt < 160 )) || fail '父进程终止测试未就绪'
  sleep 0.2
  read -r children < "/proc/$test_pid/task/$test_pid/children" || :
  [[ -n $children ]] || fail 'CPU 测试未启动子进程'
  kill -KILL "$test_pid"
  wait "$test_pid" 2>/dev/null || :
  test_pid=''
  for ((attempt=0; attempt<100; attempt++)); do
    running=0
    for pid in $children; do
      if [[ -r /proc/$pid/stat ]]; then
        { read -r stat < "/proc/$pid/stat"; } 2>/dev/null || continue
        stat=${stat##*) }
        [[ ${stat%% *} == Z ]] || running=1
      fi
    done
    (( running != 0 )) || break
    sleep 0.05
  done
  (( running == 0 )) || fail '父进程 SIGKILL 后仍有 CPU 子进程运行'
  pass '父进程 SIGKILL 后 CPU 子进程自行停止'
  # 复制、改名后仍能独立运行；CPU 用一个进程，测试容器额外限制 CPU 配额。
  for mode in gen mem disk; do
    cp "$script_dir/load_$mode.sh" "$test_root/copied.sh"
    case $mode in
      gen) options=(--workers 1) ;;
      mem) options=(--mib 1) ;;
      disk) options=(--mib 1 --directory "$scratch" --rate-mib 8) ;;
    esac
    bash "$test_root/copied.sh" "${options[@]}" --duration 1 > "$test_root/output"
    if compgen -G "$scratch/aiops-safe-stress-*.tmp" >/dev/null; then fail '独立脚本未清理临时文件'; fi
    pass "$mode 单文件复制运行（含满载 CPU 与限速磁盘路径）"
  done
  rm -f -- "$scratch/existing-data.txt"
fi
printf '完成：%s 项 Bash 检查通过\n' "$test_count"
