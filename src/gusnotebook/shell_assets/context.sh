# Bash/zsh prompt context, consumed by the terminal's fixed header.
# Percent-escape delimiters and replace control characters before emitting OSC.
_gusnb_context_field() {
  local value="${1//[[:cntrl:]]/?}"
  value="${value//\%/%25}"
  printf '%s' "${value//;/%3B}"
}

_gusnb_prompt() {
  local previous_status=$?
  local environment="${CONDA_DEFAULT_ENV-}"
  if [[ -n ${VIRTUAL_ENV-} ]]; then environment="${VIRTUAL_ENV##*/}"; fi
  printf '\033]777;gusnotebook;'
  _gusnb_context_field "$environment"
  printf ';'
  _gusnb_context_field "${USER:-${LOGNAME:-unknown}}"
  printf ';'
  _gusnb_context_field "$PWD"
  printf '\007'
  if [[ -n ${ZSH_VERSION-} ]]; then
    PROMPT='%F{blue}>%f '
    [[ -n ${NO_COLOR-} ]] && PROMPT='> '
    RPROMPT=''
  else
    PS1='\[\e[34m\]>\[\e[0m\] '
    [[ -n ${NO_COLOR-} ]] && PS1='> '
  fi
  return "$previous_status"
}

if [[ -n ${ZSH_VERSION-} ]]; then
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd _gusnb_prompt
else
  # Preserve existing prompt hooks, including Bash's array form.
  case $(declare -p PROMPT_COMMAND 2>/dev/null) in
    'declare -a '*) PROMPT_COMMAND+=(_gusnb_prompt) ;;
    *) PROMPT_COMMAND="${PROMPT_COMMAND-}"$'\n_gusnb_prompt' ;;
  esac
fi
