# Integration guidance authoring

Packaged `powercontext-project-context` resources are generated from this directory.
Change `base/` for common instructions and `hosts/<host>/` for genuine host differences.
Host templates inherit named blocks; do not edit generated copies under `integrations/`.
`resources.json` lists the complete packaged resource set, including linked references.
Generated `.powercontext-guidance.json` files contain the current `resources` inventory and `schema_version: 1`.
All declared resources (including unlinked auxiliary files) and local Markdown references must exist before
installation writes. Managed content without its manifest, invalid manifests, and incomplete legacy reference
trees are rejected; existing files never repair an incomplete incoming package.

Run `uv run python scripts/integration_guidance_skills.py --write` (or `make integration-guidance`),
then `uv run python scripts/integration_guidance_skills.py --check-generated`.
The latter runs in `make check`, reports drift without writing, and never runs model evaluation.
Running the generator again with unchanged inputs preserves file bytes and mtimes.

`src/powercontext/cli/guidance.py` owns distribution paths, guidance refresh and the permitted
path variables. Supported capabilities come from `integrations/capabilities.toml`; capability removal,
missing resolver scripts and unknown template variables fail generation. The portable agent-plugin
and MiniMax MCP packages use the Server MCP toolset as a content contract, not a native-host qualification.
DSH provides runtime Skills and is intentionally outside this file-generation surface.

The managed range starts at `<!-- POWERCONTEXT-GUIDANCE:START -->` and ends at
`<!-- POWERCONTEXT-GUIDANCE:END -->`. For `SKILL.md`, the start marker is the YAML comment
`# POWERCONTEXT-GUIDANCE:START` immediately after the opening `---`, so frontmatter remains valid
and the discovery description is updated with the body. Text outside the range is preserved byte-for-byte.
Duplicate, incomplete or reversed markers fail before writing any generated file.

The installation helpers for WorkBuddy/OpenCode and the staged Hermes/ZCode upgrades preserve these
ranges and additional local Skill files.
An existing document without markers is rejected rather than overwritten: identify the package-owned
range and add the matching markers, leaving personal notes outside. Host-managed package installation
remains subject to that host's behavior; generation tests do not certify native discovery or execution.

Reinstalling an older, unmarked package is allowed only where its existing Markdown is identical.
An incompatible downgrade fails before replacing installed guidance; staged upgrades retain additional
user files even when the incoming package predates managed-refresh metadata.

An upgrade that drops a previously owned resource requires an explicit migration and currently fails before
writing. This preserves edits and notes in retired resources and avoids silently reviving them as user files.
Unlisted Markdown with ownership markers is also rejected rather than classified as a personal file.
User-added unmarked files continue to be preserved. These installation checks do not validate arbitrary
external URLs or provide content authenticity for an untrusted package.

Git attributes keep packaged guidance resources in LF form, including on `core.autocrlf=true` checkouts.
This checkout policy does not normalize local notes outside the managed region during refresh.

The shared embedding-cost note is an intentional content addition. Claude Code's resolver accepts the
host-provided plugin root, verifies its helper, and falls back to the installed Skill's plugin root when
the environment variable is unavailable. Other existing host instructions are retained through template inheritance.

After committing a Claude Code Skill change, refresh its pinned evaluation copy with
`uv run python evaluation/skills/skill-up/sync_skill.py --revision HEAD` and run that script with `--check`.
The offline evaluation checks do not replace a new authenticated model run for the new pin.
