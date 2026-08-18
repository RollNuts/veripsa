# Release deployment ledger template

Use one entry per runtime-affecting release. This public template is deliberately
content-free: do not record provider service/job/deploy identifiers, secrets, webhook
delivery identifiers, database selectors, payloads, customer names, or repository
contents.

```text
UTC time:
Source commit SHA:
Release-gate run URL:
Gate conclusion:
Artifact SHA verified: yes/no
Schema generation before/after:
Pre-deploy backup verified: yes/no/not-applicable
Health verified: yes/no
Readiness verified: yes/no
Rollback artifact verified: yes/no
Operator:
Outcome:
```

Store any provider-private receipt in the provider's protected control plane, not in
this repository or a GitHub Actions log.
