"""共享的合成非交互式 shell 兼容用例，不执行业务命令。"""


def compatibility_cases():
    return {
        "pipeline": "printf '%s\\n' abc | tr a-z A-Z",
        "conditional": "false && printf bad; true || printf bad; printf ok",
        "substitution": "value=$(printf value); printf '%s' \"$value\"",
        "heredoc": "cat <<'END'\npayload\nEND\nprintf after",
        "stdin_empty": "cat\nprintf after",
        "stdin_long_script": "cat\n" + "# padding\n" * 4000 + "printf after",
        "read_long_script": "read value; printf '<%s>' \"$value\"\n" + "# padding\n" * 4000 + "printf after",
        "explicit_stdin": "printf input | cat; printf after",
        "set_e": "set -e; false; printf bad",
        "trap_exit": "trap 'printf exit >&2' EXIT; printf out; exit 7",
        "exec": "exec sh -c 'printf done; exit 9'",
        "cd": "cd /; pwd",
        "explicit_bash": "bash -c '[[ 3 -eq 3 ]] && printf bash'",
        "missing_command": "aiops_synthetic_missing_command",
        "syntax_error": "if then",
        "trailing_backslash": "printf '%s' value" + "\\",
        "empty_output": "true",
        "internal_variable_cleared": "printf '<%s>' \"${_aiops_script-unset}\"",
    }
