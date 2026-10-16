# Compass plugin for Claude Code

This folder is a Claude Code plugin: the `compass` MCP server plus twelve skills (playbooks). It ships inside the
`@beamreach/compass` npm package and is installed by `compass agent install`.

## Publishing it

Two public channels exist, and this folder is already in the shape both expect:

1. **Anthropic's Claude directory** (claude.ai/directory). Paid-plan developers submit either a remote MCP connector or a
   *plugin bundle* hosted as a GitHub repository (manifest in `.claude-plugin/plugin.json`, skills as `skills/<name>/SKILL.md`,
   MCP servers in the manifest). Submissions are validated and safety-scanned, then reviewed; there is no partner programme to
   join first. To submit: push this folder to a public repo (proposed `beamreach-ai/compass-plugin`), then submit that repo.
   Note the bundle's MCP entry runs `compass mcp` locally, so the npm package must be published first.
2. **github.com/anthropics/skills**: Anthropic's public skills repository (spec, template, examples). It accepts pull requests
   and highlights partner skills for specific products. Candidate: `skills/compass-install` as an example of an install playbook
   that drives an MCP server with plan-then-confirm. Keep it generic enough to read as an example, link to the product page.

Both require the `name` and `description` frontmatter fields every SKILL.md here already has.
