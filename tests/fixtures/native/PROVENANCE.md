Codex 0.154.0 session_meta captured read-only from an existing native child
on 2026-10-12. Parent/child UUIDs, role/path/nickname were replaced. Only metadata
shape is recorded; turn_context and content-leak canary are synthetic. The
0.160.1 `subagent.other=guardian` record was also observed; it has no explicit
parent and is intentionally NOT attributed. New schemas require new evidence.

Herdr 0.9.0 process-info and Codex 0.160.1 --help captured read-only on this host;
these describe the process observation option and read-only launch flags. No
model was called, account queried, or input sent. The launch flags do not prove
the permission of an already-running process; dispatch checks that process anew.
