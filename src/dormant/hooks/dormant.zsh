# shellcheck shell=bash
# (zsh-only file; the bash dialect is a lint approximation and parses cleanly.)
# Dormant hook for zsh. Install in ~/.zshrc:  eval "$(dormant hook zsh)"
# Runs on every directory change; walks up to / looking for .dormant/state
# using built-ins only. `dormant wake` starts only when a marker is found.

__dormant_chpwd() {
    [[ ${DORMANT_DISABLE-} == 1 ]] && return 0
    local dir=$PWD root=
    while :; do
        if [[ -f "$dir/.dormant/state" ]]; then
            root=${dir:-/}
            break
        fi
        case $dir in
            */*) dir=${dir%/*} ;;
            *) break ;;
        esac
    done
    if [[ -z $root ]]; then
        __dormant_last_root=
        return 0
    fi
    # One attempt per visit: no retry until the user leaves this root.
    [[ $root == "${__dormant_last_root-}" ]] && return 0
    typeset -g __dormant_last_root=$root
    command dormant wake --quiet "$root"
}

typeset -ga chpwd_functions
if [[ " ${chpwd_functions[*]-} " != *' __dormant_chpwd '* ]]; then
    chpwd_functions+=(__dormant_chpwd)
fi
__dormant_chpwd
