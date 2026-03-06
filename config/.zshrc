# shellcheck shell=bash
# Zsh configuration for Claude Code devcontainer

# Add Claude Code to PATH
export PATH="$HOME/.local/bin:$PATH"

# Oh My Zsh ($ZSH env var is set by devenv)
ZSH_THEME="robbyrussell"
plugins=(git)
source "$ZSH/oh-my-zsh.sh"

# History settings
export HISTFILE=/commandhistory/.zsh_history
export HISTSIZE=200000
export SAVEHIST=200000
setopt SHARE_HISTORY
setopt HIST_IGNORE_DUPS
setopt HIST_IGNORE_ALL_DUPS    # Remove older duplicate entries
setopt HIST_REDUCE_BLANKS      # Remove extra blanks from commands
setopt HIST_VERIFY             # Show command before executing from history

# Directory navigation
setopt AUTO_CD                 # cd by typing directory name
setopt AUTO_PUSHD              # Push directories onto stack
setopt PUSHD_IGNORE_DUPS       # Don't push duplicates
setopt PUSHD_SILENT            # Don't print stack after pushd/popd

# Completion
setopt COMPLETE_IN_WORD        # Complete from both ends of word
setopt ALWAYS_TO_END           # Move cursor to end after completion

# Aliases
alias sg=ast-grep
alias claude-yolo='claude --dangerously-skip-permissions'
alias ll='ls -lah --color=auto'
alias la='ls -A --color=auto'
alias l='ls -CF --color=auto'
alias grep='grep --color=auto'

# fzf configuration - use fd for faster file finding
export FZF_DEFAULT_COMMAND='fd --type f --hidden --follow --exclude .git'
export FZF_CTRL_T_COMMAND="$FZF_DEFAULT_COMMAND"
export FZF_ALT_C_COMMAND='fd --type d --hidden --follow --exclude .git'
export FZF_DEFAULT_OPTS='--height 40% --layout=reverse --border --info=inline'

# Use fd for ** completion (e.g., vim **)
_fzf_compgen_path() {
  fd --hidden --follow --exclude .git . "$1"
}
_fzf_compgen_dir() {
  fd --type d --hidden --follow --exclude .git . "$1"
}

# Source fzf shell integration (built-in since fzf 0.48+)
eval "$(fzf --zsh)"

# Nix dev environment activation
# Usage: dev [path]
#   dev              - activate baked env from current directory
#   dev /some/path   - activate baked env from specified directory
dev() {
  local env_file="${1:-.}/.nix-dev-env.sh"
  if [[ ! -f "$env_file" ]]; then
    echo "No baked dev environment found."
    echo "Run 'devc bake' on the host first."
    return 1
  fi
  echo "Entering dev environment (Ctrl-D or 'exit' to leave)..."
  bash --rcfile <(echo "source '$env_file'; export PS1='(nix-dev) \w \$ '")
}
