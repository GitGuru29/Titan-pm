# Fish shell completions for tpac / titanpkg (ArchTitan OS)
for cmd in tpac titanpkg
    complete -c $cmd -f

    # Arch pacman-style flags
    complete -c $cmd -o Syu -d "Synchronize repositories and upgrade all Titan subsystems"
    complete -c $cmd -o S   -d "Install or reinstall Titan packages"
    complete -c $cmd -o Sy  -d "Synchronize Titan repository databases"
    complete -c $cmd -o Su  -d "Upgrade installed Titan subsystems"
    complete -c $cmd -o Ss  -d "Search repositories for matching packages"
    complete -c $cmd -o Si  -d "Display repository package information"
    complete -c $cmd -o Sl  -d "List all available packages in repositories"
    complete -c $cmd -o Sc  -d "Clean old cached packages"
    complete -c $cmd -o Scc -d "Clean all cached packages and indexes"
    complete -c $cmd -o R   -d "Remove installed packages"
    complete -c $cmd -o Rs  -d "Remove packages and orphaned dependencies"
    complete -c $cmd -o Rns -d "Remove packages, orphans, and skip .pacsave"
    complete -c $cmd -o Q   -d "List installed packages"
    complete -c $cmd -o Qu  -d "List upgradable packages"
    complete -c $cmd -o Qi  -d "Display installed package information"
    complete -c $cmd -o Ql  -d "List files owned by an installed package"
    complete -c $cmd -o Qo  -d "Query which Titan package owns a file"
    complete -c $cmd -o Qk  -d "Verify installed files against SHA-256 hashes"
    complete -c $cmd -o Qdt -d "List orphaned dependency packages"
    complete -c $cmd -s V -l version -d "Show version"

    # Global options
    complete -c $cmd -l root       -r -d "Operate on a sandbox root"
    complete -c $cmd -l config     -r -d "Override path to titanpkg.json"
    complete -c $cmd -s y -l noconfirm -d "Assume yes for all prompts"
    complete -c $cmd -l needed        -d "Do not reinstall up-to-date targets"
    complete -c $cmd -s p -l dry-run  -d "Print resolved plan and exit"
    complete -c $cmd -l offline       -d "Use cached indexes and packages only"
    complete -c $cmd -l json          -d "Emit machine-readable JSON output"
    complete -c $cmd -s v -l verbose  -d "Increase output verbosity"

    # Subcommands
    complete -c $cmd -n "__fish_use_subcommand" -a update     -d "Refresh repository indexes (-Sy)"
    complete -c $cmd -n "__fish_use_subcommand" -a upgrade    -d "Upgrade installed packages (-Su / -Syu)"
    complete -c $cmd -n "__fish_use_subcommand" -a install    -d "Install packages (-S)"
    complete -c $cmd -n "__fish_use_subcommand" -a reinstall  -d "Reinstall installed packages"
    complete -c $cmd -n "__fish_use_subcommand" -a remove     -d "Remove installed packages (-R)"
    complete -c $cmd -n "__fish_use_subcommand" -a autoremove -d "Remove orphaned dependencies (-Qdt)"
    complete -c $cmd -n "__fish_use_subcommand" -a list       -d "List packages (-Q / -Sl / -Qu)"
    complete -c $cmd -n "__fish_use_subcommand" -a search     -d "Search repositories (-Ss)"
    complete -c $cmd -n "__fish_use_subcommand" -a info       -d "Show package metadata (-Si / -Qi)"
    complete -c $cmd -n "__fish_use_subcommand" -a files      -d "List package files (-Ql)"
    complete -c $cmd -n "__fish_use_subcommand" -a owns       -d "Query file owner (-Qo)"
    complete -c $cmd -n "__fish_use_subcommand" -a verify     -d "Verify installed files (-Qk)"
    complete -c $cmd -n "__fish_use_subcommand" -a rollback   -d "Roll back a package to an older version"
    complete -c $cmd -n "__fish_use_subcommand" -a clean      -d "Prune cached packages (-Sc)"
    complete -c $cmd -n "__fish_use_subcommand" -a doctor     -d "Run security and config diagnostics"
    complete -c $cmd -n "__fish_use_subcommand" -a key        -d "Manage trusted repository GPG keys"
    complete -c $cmd -n "__fish_use_subcommand" -a self-check -d "Verify titanpkg modules"
end

