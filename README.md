# Low-powered Kid Pager

Using expensive smart watches with also expensive cellular plans just so kids
can message their parents is ridiculous.

This is an attempt to build a simple pager-style device that can be fitted
with an ultra-cheapo SIM card (for example a low-cost data and messaging plan). It should allow
sending very simple messages and location to a custom endpoint and hopefully run for weeks on
one charge.

What works end to end: a message typed in the web app reaches the pager over LTE-M and shows on its
screen, the pager acknowledges it, and replies typed on the pager reach the web app. A page sent to a
pager that is asleep is delivered. Pagers can use an ordinary data SIM or a Soracom SIM, which is
carried through Soracom Beam (`docs/SORACOM_DESIGN.md`).


## Repository layout

| `hardware/` | Carrier board and enclosure design files. |
| `infra/` | Terraform for GCP (Cloud Run, Firestore, Firebase Hosting and Auth, Scheduler, Tasks, Secret Manager, Workload Identity Federation). See `infra/README.md`. |
| `tools/` | `pager_client.py` (simulated device and server driver), `e2e_v2.py` (end-to-end suite against the local stack), `send.py`, `provision.py`, `emqx_setup.py`. |
| `.github/workflows/` | `ci.yml` runs the relay unit tests and the end-to-end suite on push. `deploy.yml` builds the relay image and applies Terraform on a push to `main`. |

## Getting started

Run the local stack (broker, Firebase emulators and relay) and the end-to-end suite:

```bash
cd relay
docker compose up -d --build
cd ..
python3 tools/emqx_setup.py

# Run all scenarios, in text and CBOR wire encodings
relay/.venv/bin/python tools/e2e_v2.py
relay/.venv/bin/python tools/e2e_v2.py --wire cbor

# Or run individual scenarios
relay/.venv/bin/python tools/e2e_v2.py bootstrap setup_code address_book
```

Scenarios: `bootstrap`, `text_roundtrip`, `allowlist`, `republish`, `location_periodic`,
`tools/e2e_v2.py`.

Unit tests and manual checklists are documented in `relay/README.md`, `web/README.md` and
