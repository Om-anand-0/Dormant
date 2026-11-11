# Dormant hook for fish. Install in ~/.config/fish/config.fish:
#   dormant hook fish | source
# Runs on every directory change; walks up to / looking for .dormant/state
# using built-ins only. `dormant wake` starts only when a marker is found.

function __dormant_hook --on-variable PWD
    test "$DORMANT_DISABLE" = 1; and return 0
    set -l dir $PWD
    set -l root
    while true
        if test -f "$dir/.dormant/state"
            set root "$dir"
            test -z "$root"; and set root /
            break
        end
        # Quote $dir: at / it is empty, and `string` with no argument reads stdin.
        string match -q -- '*/*' "$dir"; or break
        set dir (string replace -r -- '/[^/]*$' '' "$dir")
    end
    if test -z "$root"
        set -g __dormant_last_root
        return 0
    end
    # One attempt per visit: no retry until the user leaves this root.
    test "$root" = "$__dormant_last_root"; and return 0
    set -g __dormant_last_root $root
    command dormant wake --quiet "$root"
end

__dormant_hook
