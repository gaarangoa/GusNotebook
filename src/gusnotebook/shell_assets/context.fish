# Keep activation from adding the environment back into the inline prompt.
set -gx VIRTUAL_ENV_DISABLE_PROMPT 1
set -gx CONDA_CHANGEPS1 false

function _gusnb_context_field
    set -l value (string replace -ar '[[:cntrl:]]' '?' -- "$argv[1]")
    set value (string replace -a '%' '%25' -- "$value")
    string replace -a ';' '%3B' -- "$value"
end

function fish_prompt
    set -l environment "$CONDA_DEFAULT_ENV"
    if set -q VIRTUAL_ENV; and test -n "$VIRTUAL_ENV"
        set environment (string replace -r '.*/' '' -- "$VIRTUAL_ENV")
    end
    printf '\e]777;gusnotebook;%s;%s;%s\a' (_gusnb_context_field "$environment") (_gusnb_context_field "$USER") (_gusnb_context_field "$PWD")
    if test -z "$NO_COLOR"
        set_color blue
    end
    printf '>'
    if test -z "$NO_COLOR"
        set_color normal
    end
    printf ' '
end

function fish_right_prompt
end
