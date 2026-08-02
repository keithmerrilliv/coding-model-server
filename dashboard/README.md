# Coding Model Multi-Agent Server Dashboard

A standalone React + TypeScript SPA for monitoring the Coding Model Multi-Agent
Server — and for approving the autonomous pipeline's review gates. It is not
read-only: gate approve/reject is a first-class feature (see Usage).

## Prerequisites

- Node.js 20.0.0 or newer
- npm 10+

## Setup

1. Install dependencies:
   ```bash
   npm install
   ```

2. Configure environment variables (optional):
   Copy `.env.example` to `.env.local` and adjust if your server runs on a different host/port:
   ```bash
   cp .env.example .env.local
   ```

   The API base URL defaults to `<current hostname>:5000`. Set
   `VITE_CODING_MODEL_SERVER_URL` to override it. This is a Vite variable, so it
   is **baked in at build time** — a production bundle points at whatever host it
   was built with; rebuild to change it.

## Run

- Development server (port 3000):
  ```bash
  npm run dev
  ```
- Type checking:
  ```bash
  npm run typecheck
  ```
- Production build (emits to `dashboard/dist/`):
  ```bash
  npm run build
  ```
- Preview production build locally:
  ```bash
  npm run preview
  ```

## Deploying

In production the SPA is served by `scripts/serve_dashboard.py` (stdlib static
server, SPA fallback: unknown paths return `index.html`), managed by the
`coding-model-dashboard` systemd unit.

| Variable | Default |
|----------|---------|
| `CODING_MODEL_DASHBOARD_HOST` | `127.0.0.1` (set `0.0.0.0` for LAN access) |
| `CODING_MODEL_DASHBOARD_PORT` | `3001` |
| `CODING_MODEL_DASHBOARD_ROOT` | `<repo>/dashboard/dist` |

Run `npm run build` first — the server serves `dist/`, and a stale or missing
build is the usual cause of a blank dashboard. Note the dev server is on 3000 and
the deployed one on 3001; whichever you use must be listed in the server's
`CORS_ORIGINS` (with its port).

## Usage

- On first load, you will be prompted for the admin API key. It is stored in `localStorage["codingModel.adminKey"]`.
- Pages: Overview (`/`), Specs List (`/specs`), Spec Detail (`/specs/:id`), Metrics (`/metrics`).
- **Approve or reject gates** from the spec detail view (`GateActions`), with
  markdown notes. Rejection notes feed back to the agent for its retry — this is
  the same gate queue as `coding-model-autonomous gates`.
- Use the Logout button to clear the admin key from storage.

### Endpoints polled

Intervals are hardcoded, not configurable:

| Endpoint | Interval |
|----------|----------|
| `GET /health` | 10s |
| `GET /v1/autonomous/specs`, `/v1/autonomous/specs/:id` | 10s |
| `GET /v1/admin/gpu_stats` | 1s |
| `GET /v1/admin/metrics?window_seconds=` | 1s |
| `GET /v1/admin/active_model` | 2s |
| `GET /v1/models` | on load |
| `POST /v1/autonomous/gates/:id/respond` | on user action |

## Architecture

- Built with Vite 5, React 18, TypeScript (strict mode).
- Uses native `fetch` with a centralized client wrapper (`src/api/client.ts`).
- State management via React Context + Hooks.
- Plain CSS global stylesheet — no component/UI framework. Three rendering
  libraries are used: `mermaid` (the execution DAG), `react-markdown` and
  `remark-gfm` (gate notes and artifacts).

### Components

| Component | Purpose |
|-----------|---------|
| `HealthCard` | Server status, model-loaded flag |
| `ActiveModelCard` | Which model is currently resident |
| `GpuPanel`, `Sparkline` | Live GPU stats |
| `EndpointMetrics` | Per-endpoint request metrics |
| `AgentsGrid` | The agent roster from `/v1/models` |
| `SpecsTable`, `SpecDetail` | Autonomous specs |
| `ExecutionDag` (+ `dag.ts`) | Mermaid render of the spec's task graph |
| `EventTimeline` | Spec event log (including Phase-b `adversarial_test_writer` firings) |
| `GateActions` | Approve/reject a review gate with notes |
