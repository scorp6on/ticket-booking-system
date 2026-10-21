import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The page calls /api/...; Vite forwards those to FastAPI on port 8000.
// Same origin for the browser, so no CORS setup is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000", // not localhost: on Windows it tries IPv6 first (~200 ms per connection)
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
