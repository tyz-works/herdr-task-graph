# Changelog

All notable changes to this project are documented here.

## 0.1.1 - 2026-09-21

- Add runtime compatibility checks for Herdr socket protocols 19 through 22.
- Reject older protocols and warn while continuing on unverified future protocols.
- Display the detected Herdr version and protocol in the dashboard.

## 0.1.0 - 2026-09-21

- Initial terminal DAG dashboard.
- Live `session.snapshot` and `pane.agent_status_changed` integration.
- Dependency-based `READY` and `WAIT` calculation.
- Agent-pane focus from the selected task.
- Responsive layout and offline demo mode.
