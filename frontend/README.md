# ggNet frontend

React + TypeScript (Vite), dark UI. `npm run build` outputs to `frontend/dist/`,
which the backend serves on the same port as the API.

```bash
npm ci
npm run dev        # http://localhost:5173, proxies /api to 127.0.0.1:8088
npm run build      # typecheck + production build into dist/
npm test           # vitest (jsdom), fetch is mocked; no backend needed
```

Pages:

- **Machines**: register PCs, assign/switch/remove a game disk, reset, delete.
  Status and `last_error` come from the backend; the list refreshes every 10 s.
- **Game disks**: create a draft zvol, publish it (`@base`, read-only), delete.

Destructive actions (switching or removing a disk, reset, delete) ask for
confirmation, because the client PC must be powered off and its writes are lost.
