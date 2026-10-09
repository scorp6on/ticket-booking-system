import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The page calls /api/...; Vite forwards those to FastAPI on port 8000.
// Same origin for the browser, so no CORS setup is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
