"""Mac-side HTTP runner for qwen-server autonomous specs.

Accepts test-run requests from the Linux orchestrator over a loopback-only
HTTP endpoint, materializes a git worktree of a registered repo, applies the
LLM's patch files, and runs swift_test or xcodebuild_test inside it.
"""
