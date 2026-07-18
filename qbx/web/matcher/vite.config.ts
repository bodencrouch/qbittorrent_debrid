import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@wailsio/runtime": path.resolve(__dirname, "./src/wails-runtime-stub.ts"),
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8484",
      "/qbt": "http://127.0.0.1:8484",
      "/qbx": "http://127.0.0.1:8484",
    },
  },
});
