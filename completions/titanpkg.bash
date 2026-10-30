# Bash completion for tpac and titanpkg
_titanpkg() {
    local cur prev words cword
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local flags="-Syu -S -Sy -Su -Ss -Si -Sl -Sc -Scc -R -Rs -Rns -Q -Qu -Qi -Ql -Qo -Qk -Qdt -V --version --root --config --noconfirm -y --needed --dry-run -p --offline --json --verbose -v"
    local commands="update upgrade install reinstall remove autoremove list search info files owns verify rollback clean doctor key self-check"

    if [[ "$prev" == "--root" || "$prev" == "--config" || "$prev" == "-Qo" || "$prev" == "owns" ]]; then
        COMPREPLY=( $(compgen -f -- "$cur") )
        return 0
    fi

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
        return 0
    fi

    COMPREPLY=( $(compgen -W "$commands $flags" -- "$cur") )
    return 0
}
complete -F _titanpkg tpac titanpkg

