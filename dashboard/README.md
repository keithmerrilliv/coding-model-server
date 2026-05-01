# Qwen Multi-Agent Server Dashboard

A standalone React + TypeScript SPA for monitoring the runtime state of the Qwen Multi-Agent Server.

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

## Run

- Development server:
  ```bash
  npm run dev
  ```
- Type checking:
  ```bash
  npm run typecheck
  ```
- Production build:
  ```bash
  npm run build
  ```
- Preview production build locally:
  ```bash
  npm run preview
  ```

## Usage

- On first load, you will be prompted for the admin API key. It is stored in `localStorage["qwen.adminKey"]`.
- The dashboard polls `/health`, `/v1/models`, `/v1/autonomous/specs`, and `/v1/autonomous/specs/:id` at configured intervals.
- Navigate between Overview (`/`), Specs List (`/specs`), and Spec Detail (`/specs/:id`).
- Use the Logout button to clear the admin key from storage.

## Architecture

- Built with Vite 5, React 18, TypeScript (strict mode).
- Uses native `fetch` with a centralized client wrapper.
- State management via React Context + Hooks.
- Plain CSS global stylesheet. No external UI libraries.