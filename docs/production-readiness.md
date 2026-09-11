# Production readiness checklist (stub — fill through G5)

- [ ] Naming/tag audit (`devops-g9`, required tags)
- [ ] Non-root, read-only containers; `/health` `/ready`
- [ ] No `latest` tags — SHA build, digest deploy
- [ ] SBOM + secret/dep/IaC/image scans; HIGH/CRITICAL policy
- [ ] SLOs wired in Grafana; burn alerts
- [ ] Runbook rehearsed; destroy/rebuild documented
