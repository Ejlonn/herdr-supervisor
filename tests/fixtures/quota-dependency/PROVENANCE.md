# Quota dependency fixtures

Captured read-only on 2026-10-22 with `herdr 0.9.5` for the `herdr-quota-dependency` check and bootstrap tests.

- `plugin-list-installed-quota-0.9.5.json`: the real `herdr plugin list --json` object for the installed `herdr-agent-quota` plugin (GitHub source with `resolved_commit`), reduced to that one plugin, with the home directory replaced by `/home/user`. No account, session, token or quota value is present.
- `plugin-list-empty-0.9.5.json`: the real output of the same command in a fresh, isolated `HOME` with no plugins installed.
- `plugin-list-help-0.9.5.txt`, `plugin-install-help-0.9.5.txt`, `plugin-link-help-0.9.5.txt`, `plugin-enable-help-0.9.5.txt`: the real `--help` output of those commands (usage portion; the trailing generic Herdr banner is omitted). They document the accepted options (`install --ref`, `link --disabled`, `enable <PLUGIN_ID>`) the operator instructions and `herdr-setup` rely on. The `enable` help was captured read-only on 2026-11-09 with `herdr 0.9.5` in an isolated HOME.

Nothing was installed, linked, enabled, configured or refreshed to produce them.
