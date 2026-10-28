# Startup-prompt fixtures — provenance and limits

Everything here is either a **retained native fragment** (copied from the preserved diagnostic evidence, never edited) or explicitly **synthetic**.

| File | Origin | What it proves / does not prove |
|---|---|---|
| `codex_update_banner_attempt2_visible.json` | `supervisor-native-diagnostic-trust-auto-successor-20261026/execution-evidence-attempt2/screen-seed-codex.json` (`visible_lines`, pane id replaced by `<pane>`), captured by step 06 **after the human had selected Skip** | Codex's post-handling update NOTICE box (`Update available! 0.163.0 -> 0.164.0`). It is not the interactive menu and does not prove the notice is still blocking. |
| `codex_update_menu_attempt2_topic.txt` | Herdr's pane topic for `diag2-codex` in `.../execution-evidence-attempt2/collect-summary.json` (`tokens.quota_topic`, truncated by the presenting plugin) | The text of the first option of Codex's startup update MENU. Only option 1 survives; the Skip option text, the surrounding frame and the frame position were never retained. |
| `codex_trust_option_attempt1_topic.txt` | first attempt `.../seed-session-amendment/execution-evidence/collect-summary.json` (`tokens.quota_topic`) | The first option of Codex's folder-trust dialog (agent status was `blocked`, `launch_pending: true`). A SEPARATE decision, never an update wait. |
| `herdr_agent_not_ready_attempt1_error_fragment.txt` | first attempt, same file (`quota_topic` of the original Codex pane) | Herdr `agent start` reports `agent_not_ready` ("agent ... is blocked dur[ing startup]") — the signal the sealed 04 step treated as terminal. Truncated. |
| `claude_synthetic_unknown.json` | **SYNTHETIC** | A Claude-looking prompt that deliberately contains Codex's option wording. No Claude startup screen was ever retained, so Claude has no recognition pattern: this must stay UNKNOWN. Not native evidence. |

Screens used by the tests are assembled from these fragments plus clearly synthetic filler lines. No test result here is native validation of a live Codex or Claude startup, of the Telegram card, or of an update's effect on the supervisor.
