# shellcheck shell=bash
# Dormant hook for bash. Install in ~/.bashrc:  eval "$(dormant hook bash)"
# Runs before each prompt; when $PWD changed, walks up to / looking for
# .dormant/state using built-ins only. `dormant wake` starts only when found.

__dormant_check() {
    [ "${DORMANT_DISABLE-}" = 1 ] && return 0
    local dir=$PWD root=
    while :; do
        if [ -f "$dir/.dormant/state" ]; then
            root=${dir:-/}
            break
        fi
        case $dir in
            */*) dir=${dir%/*} ;;
            *) break ;;
        esac
    done
    if [ -z "$root" ]; then
        __dormant_last_root=
        return 0
    fi
    # One attempt per visit: no retry until the user leaves this root.
    [ "$root" = "${__dormant_last_root-}" ] && return 0
    __dormant_last_root=$root
    command dormant wake --quiet "$root"
}

__dormant_prompt() {
    local status=$?
    if [ "$PWD" != "${__dormant_last_pwd-}" ]; then
        __dormant_last_pwd=$PWD
        __dormant_check
    fi
    return "$status"
}

if [[ " ${PROMPT_COMMAND[*]-} " != *__dormant_prompt* ]]; then
    if [ -z "${PROMPT_COMMAND+set}" ]; then
        PROMPT_COMMAND=__dormant_prompt
    elif ((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 501)) && [[ ${PROMPT_COMMAND@a} == *a* ]]; then
        PROMPT_COMMAND+=(__dormant_prompt)  # bash 5.1+ array form
    else
        # String form. Newline, not ';': safe after a trailing ';' or '&' in the old value.
        # shellcheck disable=SC2128,SC2178
        PROMPT_COMMAND=${PROMPT_COMMAND}$'\n'__dormant_prompt
    fi
fi
