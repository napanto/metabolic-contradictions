# Addendum04a — replace Codex built-in instructions before oracle inference

Antonio's request in INBOX_FROM_CLAUDE.md,13 November2026 at12:50 CET, refines amendment04 before any oracle output. Signed amendment04 remains in force: the46 human IDs,145 oracle items, seeds, pilot order, model/effort, fallback and analyses do not change. No oracle inference has run.

The oracle's built-in coding-agent instructions are replaced using `model_instructions_file`. That read-only file contains only the exact frozen `annotation/GUIDELINES.md` text (trailing whitespace trimmed at the file boundary), followed by one neutral line:

> Return only JSON that matches the supplied schema.

Its SHA-256 is `7cbbc1293dbaa0152b05ab2ba16cb4f06b7a803f6ad032b6714878edc819231e`. The stdin prompt contains only the one original public item JSON. Do not add coding instructions, repository context, research hypotheses, system predictions, other labels or other items. The JSON output schema remains the declared annotation schema.

Set `include_environment_context=false`, `include_environment_context_time=false`, `include_permissions_instructions=false`, `include_apps_instructions=false`, `include_collaboration_mode_instructions=false` and `personality="none"`. Supply no `developer_instructions`. Keep `--strict-config`, all previous tool/plugin/MCP restrictions, bubblewrap filesystem/PID/network isolation, ChatGPT-only authentication and egress policy. Do not touch API keys or gopass.

Record the instructions-file hash in every item's provenance, alongside the item/prompt hash and existing timing/version/validity fields. Identical-input retries require the same instructions, item and schema hashes, while each attempt uses a fresh process and empty external temporary CWD.

Before the remaining pilot, run only the first preselected pilot item, `pair-067`. Verify its effective CLI configuration and JSONL events, including absence of tool calls. If the CLI safely exposes assembled model input locally without credentials, preserve and inspect that actual artifact for Codex base instructions, environment context and tool definitions. Never log authentication headers/tokens. A locally reconstructed configuration is not evidence of the actual assembled request; clearly distinguish observed artifacts from configuration assertions. If direct assembled-input inspection is unavailable, record that fact instead of claiming verification of hidden content.

Any ChatGPT-backend server-side instructions not removable or inspectable client-side remain an explicit limitation. The judge is still a closed-weight frontier LLM run through an agent wrapper, from the same model family as the pipeline-building agent. It is not a raw API call or an independent human expert.

After the single-item verification, complete the remaining four preselected pilot items, report timing/validity and apply the existing feasibility rule; then continue the full 145-item run with at most two concurrent calls. No earlier labels are substituted. The human UI remains the 46-item subset. Final paper-style revision waits for human annotation as requested.

## Pre-inference client-tool clarification

The local `codex debug prompt-input` inspection showed that ordinary feature flags alone retained tools from the bundled GPT-6 Astra descriptor. Before any oracle output, additionally set `agents.enabled=false` and use the supported model-catalog override with SHA-256 `f3da60f2921bd6f212b298af43ef5d09a518320adcc5050a2eba9603c4abed1b`. Its only descriptor changes are `node_repl_disabled: false→true`, `apply_patch_tool_type: freeform→null`, `experimental_supported_tools: [send_user_message_async, clock]→[]`, and `tool_mode: code_mode_only→direct`. Model identity, reasoning choices, routing, context limits, sampling and all other descriptor values remain unchanged.

A no-inference local inspection then contains exactly two messages: replacement `model.base_instructions` and the single-item `user.text`, with no additional tool definitions or environment/collaboration instructions. The first pilot item's actual local assembled input will be archived and checked under the same setup. Backend server instructions remain outside this client-side observation. This clarification is signed as a new commit before inference; no prior history is rewritten.
