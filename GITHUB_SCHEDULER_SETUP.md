# GitHub manual scanner diagnostics

Production scheduling and completion/Telegram checks are managed by cron-job.org.
See [deployment/CRON_DEPLOYMENT.md](deployment/CRON_DEPLOYMENT.md) for the deployed architecture, schedules, limitations and rollback.

All GitHub workflows are manual (`workflow_dispatch`) only. GitHub stores code and supports explicitly requested diagnostics; it is not a backup automatic scheduler.

The cloud diagnostic workflow uses existing GitHub secrets to request a PythonAnywhere scan and retrieve its report. The PythonAnywhere daily run claim may reject a duplicate mode/day; do not clear its ledger or retry blindly while the original run status is uncertain. Test mode is diagnostic only and does not establish trading readiness.

The separate legacy scanner workflow runs locally on a GitHub runner only when manually requested. It does not send production Telegram reports.
