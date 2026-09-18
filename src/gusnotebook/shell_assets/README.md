# Terminal colors

`colors.zsh` adds highlighting after user configuration. `context.sh` (Bash/zsh)
and `context.fish` replace the shell prompt with `> ` and emit the environment,
user, and current directory to a fixed terminal header using OSC 777. Context
updates at each prompt and travels in scrollback when a browser reattaches.
These scripts are sourced by temporary startup files; they do not edit user
dotfiles. `NO_COLOR` disables our colors. Fish keeps its built-in highlighting,
and agent processes render their own prompts and ANSI colors.

The bundled zsh-syntax-highlighting runtime is from
https://github.com/zsh-users/zsh-syntax-highlighting, release **0.8.0**, commit
`db085e4661f6aafd24e5acb5b2e17e4dd5dddf3e`.
Only the entry script and main/brackets highlighters are included, unchanged.
Its BSD license is in `zsh-syntax-highlighting/COPYING.md`.
