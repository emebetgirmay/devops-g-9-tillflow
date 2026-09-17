# POS API — G1 stub (health only). Full sale API lands in G2.

Group: devops-g9 · TillFlow · DRI: Alice Moraa (`@Moraaalice`) for product; Platform wires deploy.

## Local

```bash
python app.py
curl -s localhost:8080/health
```

## Build / push (G1)

From repo root:

```bash
./scripts/build-push-pos.sh bootstrap
```
