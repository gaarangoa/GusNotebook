# Sourced after the user's startup files, only for GusNotebook shell terminals.
[[ -n ${NO_COLOR-} ]] && return 0

# Respect a highlighter already installed by the user's shell configuration.
if (( ! ${+functions[_zsh_highlight]} && ! ${+functions[_fast_highlight]} )); then
  typeset -ga ZSH_HIGHLIGHT_HIGHLIGHTERS=(main brackets)
  typeset -gA ZSH_HIGHLIGHT_STYLES
  : ${ZSH_HIGHLIGHT_STYLES[single-hyphen-option]:=fg=cyan}
  : ${ZSH_HIGHLIGHT_STYLES[double-hyphen-option]:=fg=cyan}
  : ${ZSH_HIGHLIGHT_STYLES[path]:=fg=cyan,underline}
  : ${ZSH_HIGHLIGHT_STYLES[commandseparator]:=fg=magenta}
  : ${ZSH_HIGHLIGHT_STYLES[comment]:=fg=blue}
  source "${${(%):-%N}:A:h}/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
fi
