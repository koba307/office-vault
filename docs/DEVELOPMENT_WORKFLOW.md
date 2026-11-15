# DEVELOPMENT WORKFLOW INSTRUCTIONS: STRICT PHASE-BY-PHASE IMPLEMENTATION WITH MANDATORY GITHUB BACKUPS

## 0. ЯЗЫК ОБЩЕНИЯ

Все объяснения, отчёты о выполненных фазах, комментарии к изменениям, описание планов и любые ответы пользователю должны быть на русском языке.

Код, имена переменных, идентификаторы, commit messages, названия файлов и технические термины могут оставаться на английском языке, если это соответствует принятым в проекте соглашениям.

## 1. CORE DEVELOPMENT PRINCIPLE

You must develop the project strictly in sequential, independently verifiable stages.

NEVER attempt to implement the entire project at once.

Divide the complete project specification into logical development phases. Each phase must have a clearly defined objective, implementation scope, expected result, and acceptance criteria.

Your primary responsibility is to ensure that every completed development phase is fully implemented, tested, documented, and safely preserved in GitHub before starting the next phase.

GitHub must serve as the central version-controlled repository and the permanent source of truth for the project's development history.

---

## 2. INITIAL PROJECT ANALYSIS AND DEVELOPMENT ROADMAP

Before writing or modifying any code:

1. Thoroughly analyze the complete project specification.
2. Inspect all existing source code and project files.
3. Identify the current architecture, technologies, dependencies, and integrations.
4. Identify all required modules, components, services, and functionality.
5. Determine dependencies between individual components.
6. Identify potential technical risks and implementation constraints.
7. Divide the entire project into logically ordered development phases.

Create a detailed development roadmap in:

`DEVELOPMENT_ROADMAP.md`

The roadmap must include:

- Phase number and name.
- Phase objectives.
- Features and components to implement.
- Files and modules expected to be created or modified.
- Dependencies on previous phases.
- Implementation requirements.
- Testing requirements.
- Acceptance criteria.
- Expected deliverables.
- GitHub backup and verification requirements.

Also create:

- `PROJECT_STATUS.md` — current development status.
- `CHANGELOG.md` — chronological implementation history.
- `ARCHITECTURE.md` — current project architecture.
- `DEVELOPMENT_LOG.md` — detailed development activity log.

Do not begin full implementation until the roadmap has been prepared.

---

## 3. STRICT SEQUENTIAL DEVELOPMENT

Implement exactly one development phase at a time.

The mandatory workflow for EVERY phase is:

1. Analyze the requirements of the current phase.
2. Define its implementation plan.
3. Inspect dependencies and existing code.
4. Implement all functionality included in the current phase.
5. Integrate new components with existing modules.
6. Perform code review and detect potential issues.
7. Run automated and manual tests.
8. Fix all discovered errors.
9. Verify that previously implemented functionality still works.
10. Update technical documentation.
11. Update project status and changelog.
12. Save all completed changes to GitHub.
13. Verify that the GitHub repository contains the correct and complete version.
14. Only after successful verification, proceed to the next phase.

Do not skip any of these steps.

Do not start implementing the next phase while the current phase has unresolved critical errors, incomplete functionality, failed acceptance criteria, or an unverified GitHub backup.

---

## 4. MANDATORY GITHUB VERSION CONTROL

GitHub backup is mandatory after EVERY completed development phase.

Do not accumulate changes across several phases before uploading them.

### 4.1. Repository preparation

If the project already has a GitHub repository:

- Inspect its structure and history.
- Preserve existing branches, tags, and commits.
- Verify the correct repository and remote.
- Never overwrite unrelated repositories or branches.

If no repository exists:

- Create a new private GitHub repository.
- Configure Git.
- Connect the local project.
- Verify authentication and push permissions.

Use a private repository whenever the project contains proprietary source code, internal configurations, or sensitive project information.

### 4.2. Required backup procedure after each phase

After successfully completing and testing a phase:

**STEP 1 — Review changes**

Inspect all modified, created, renamed, and deleted files.

Ensure that no unrelated files or accidental changes are included.

**STEP 2 — Validate project integrity**

Run the required tests, build checks, static analysis, and other relevant validation procedures.

**STEP 3 — Update documentation**

Update:

- `PROJECT_STATUS.md`
- `CHANGELOG.md`
- `DEVELOPMENT_LOG.md`
- `DEVELOPMENT_ROADMAP.md`, if necessary
- Other documentation affected by the changes.

**STEP 4 — Create a Git commit**

Use a descriptive commit message following this naming convention:

`phase-XX: descriptive implementation summary`

Examples:

- `phase-01: initialize project architecture`
- `phase-02: implement authentication system`
- `phase-03: implement database layer`
- `phase-04: implement API integration`

**STEP 5 — Push to GitHub**

Push the completed phase to the designated repository and development branch.

**STEP 6 — Verify the backup**

Confirm that:

- The push completed successfully.
- The remote repository contains the latest commit.
- All intended source files are present.
- No required changes remain uncommitted.
- The commit hash matches the local version.
- The repository can be cloned or checked out successfully.

**STEP 7 — Create a version tag**

Create a Git tag for each successfully completed phase:

- `v0.1.0-phase-01`
- `v0.2.0-phase-02`
- `v0.3.0-phase-03`

Continue sequentially.

Push the tags to GitHub.

The exact version numbering can be adjusted to the project's release strategy, but every phase must have an identifiable, recoverable Git reference.

**STEP 8 — Confirm completion**

Only after verifying the remote backup, mark the phase as completed.

---

## 5. PHASE ACCEPTANCE CRITERIA

Every phase must have measurable completion criteria.

A phase cannot be marked as completed simply because the code has been written.

It must satisfy all applicable conditions:

- All planned functionality has been implemented.
- All required files are present.
- Components are integrated correctly.
- No critical implementation errors remain.
- Tests have passed.
- Existing functionality has not been broken.
- Dependencies and configurations are correct.
- Documentation has been updated.
- GitHub backup has been successfully uploaded.
- Commit and tag have been verified.

If any acceptance criterion fails, remain in the current phase.

Fix the problem, repeat the necessary tests, and create a new GitHub commit when the corrected phase is ready.

Never hide incomplete functionality or mark partially implemented features as complete.

---

## 6. FAILURE RECOVERY AND ROLLBACK

Maintain the ability to return to any previously completed phase.

For every phase:

- Preserve the previous stable Git commit.
- Never rewrite or delete verified historical commits unnecessarily.
- Avoid destructive Git operations.
- Never force-push over established project history without explicit authorization.
- Use separate feature branches when implementing risky or experimental changes.
- Merge into the main development branch only after successful testing.

If an implementation breaks existing functionality:

1. Stop further development.
2. Identify the cause.
3. Restore the last known stable state if necessary.
4. Fix the issue in the current phase.
5. Run regression tests.
6. Save the corrected state to GitHub.
7. Verify repository integrity again.

Do not continue development on top of an unstable or corrupted project state.

---

## 7. GITHUB SECURITY AND SECRET MANAGEMENT

Never upload sensitive credentials directly to GitHub.

The repository must not contain exposed:

- `.env` files with real secrets.
- API keys.
- Telegram bot tokens.
- Passwords.
- Private SSH keys.
- Database credentials.
- OAuth secrets.
- Private certificates.
- Access tokens.

Instead:

- Maintain `.env.example` with variable names and placeholder values.
- Use GitHub Secrets for CI/CD credentials.
- Use secure secret management for production environments.
- Maintain `.gitignore`.
- Scan commits for accidentally exposed credentials before pushing.

If a secret is accidentally committed, immediately stop and follow a secure credential rotation and repository remediation procedure.

Do not assume that deleting a secret from the latest commit removes it from Git history.

---

## 8. TESTING AND QUALITY CONTROL

Testing is required after every development phase.

Use the testing methods appropriate to the project:

- Unit tests.
- Integration tests.
- API tests.
- Database tests.
- End-to-end tests.
- Frontend tests.
- Security checks.
- Static code analysis.
- Dependency validation.
- Build verification.
- Regression testing.

Maintain automated tests where possible.

Every new feature must include appropriate tests.

Do not disable existing tests or weaken validation requirements simply to make a phase appear successful.

Document:

- Tests performed.
- Tests passed.
- Tests failed.
- Known limitations.
- Remaining issues.

---

## 9. DEVELOPMENT PROGRESS TRACKING

Maintain an accurate development status at all times.

Use the following phase statuses:

- `NOT_STARTED`
- `IN_PROGRESS`
- `TESTING`
- `FAILED`
- `BLOCKED`
- `COMPLETED`
- `GITHUB_BACKUP_VERIFIED`

A phase is considered fully completed only after reaching:

`GITHUB_BACKUP_VERIFIED`

Update `PROJECT_STATUS.md` after each significant development milestone.

Maintain a clear record of:

- Current phase.
- Completed phases.
- Remaining phases.
- Current implementation progress.
- Test results.
- Latest GitHub commit.
- Latest verified tag.
- Known issues.
- Next planned actions.

Never report progress based on estimated work alone. Report actual implemented and verified functionality.

---

## 10. STRICT PROHIBITION AGAINST PREMATURE PHASE TRANSITIONS

The following actions are strictly prohibited:

- Starting multiple independent development phases simultaneously without an explicit architectural dependency requiring parallel work.
- Moving to the next phase before the current phase passes testing.
- Skipping GitHub backups.
- Continuing development while the GitHub backup is unverified.
- Marking unfinished features as implemented.
- Replacing real functionality with placeholders.
- Leaving undocumented temporary code.
- Making unrelated changes outside the current phase.
- Breaking previously completed modules.
- Deleting stable versions or previous backups.
- Uploading secrets to a public or private Git repository.

If the current phase is blocked, explain the reason and continue working only on resolving that phase.

---

## 11. REQUIRED REPORT AFTER EVERY COMPLETED PHASE

After finishing each phase, provide a report using this exact structure:

```
PHASE XX — [PHASE NAME]

Implementation status: COMPLETED / FAILED / BLOCKED

Implemented functionality:
- List all implemented components and features.

Modified files:
- List important created, modified, and deleted files.

Testing results:
- Tests performed.
- Tests passed.
- Tests failed.
- Remaining issues.

Documentation:
- List updated documentation files.

GitHub backup:
- Repository name.
- Branch.
- Commit message.
- Commit hash.
- Version tag.
- Push status.
- Remote verification status.

Known issues:
- List unresolved issues or explicitly state that none remain.

Next phase:
- Phase number.
- Phase name.
- Planned objectives.

Final phase status: GITHUB_BACKUP_VERIFIED / NOT VERIFIED
```

Do not begin the next phase until the report confirms successful GitHub backup verification.

---

## 12. FINAL PROJECT COMPLETION

After all development phases have been completed:

1. Perform a comprehensive project-wide code review.
2. Verify all modules and integrations.
3. Run the complete automated test suite.
4. Perform security and dependency audits.
5. Verify configuration and deployment readiness.
6. Check that documentation is complete.
7. Verify that GitHub contains the complete final source code.
8. Create a final stable release tag.
9. Verify that the repository can be cloned and the project can be reconstructed.
10. Prepare a final implementation report.

The final repository must contain the complete project source code, documentation, tests, configurations without exposed secrets, and the full development history.

---

## 13. ABSOLUTE DEVELOPMENT RULE

ONE PHASE → COMPLETE IMPLEMENTATION → TESTING → BUG FIXES → DOCUMENTATION → GITHUB COMMIT → PUSH → REMOTE VERIFICATION → VERSION TAG → NEXT PHASE.

Repeat this cycle until the entire project is completed.

Never bypass this sequence.

Your priority is not development speed. Your priority is implementation quality, stability, recoverability, transparency, and preservation of every successfully completed development stage.

Every completed phase must exist as a verified, independently recoverable version in GitHub before any work begins on the next phase.
