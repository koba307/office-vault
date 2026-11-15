Alembic migration history for OFFICE VAULT.

Use the repository root as the working directory:

    alembic current
    alembic upgrade head
    alembic downgrade -1

The bot applies `upgrade head` before seeding system data. For a pre-Alembic
database, startup stamps revision 0001 only after verifying the exact legacy
schema. Unsupported or partial schemas stop startup; take a backup and reconcile
them explicitly rather than allowing an automatic guess.

Never run downgrade commands against production without a verified backup and
an explicit maintenance plan.
