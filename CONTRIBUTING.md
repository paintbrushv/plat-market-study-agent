# Contributing

Thank you for helping improve the Market Study Agent. Please follow the steps below when proposing changes.

1. **Discuss first**: Open an issue describing the enhancement or bug. For data or compliance topics, tag `@market-study/data-stewards`.
2. **Create a branch**: Branch names should follow `feature/<slug>` or `fix/<slug>` conventions.
3. **Set up tooling**: Run `make setup` to initialize a local environment and install pre-commit hooks.
4. **Write tests**: Add or update unit tests under `tests/` for any functional change.
5. **Respect guardrails**: Never commit paid data, secrets, or personal information. Confirm `.gitignore` covers any new artifacts you introduce.
6. **Run checks**: Execute `make lint` and `make test` before opening a pull request.
7. **Submit PR**: Reference the issue ID, summarize changes, and list validation steps. CODEOWNERS will review for governance compliance.

By contributing, you agree to the code of conduct implied by our data sharing agreements and security policies.
