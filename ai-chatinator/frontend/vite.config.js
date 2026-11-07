import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const directory = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/client/" : "/",
  plugins: [react()],
  server: {
    fs: { allow: [path.resolve(directory, "..")] },
    proxy: {
      "/api": process.env.AI_CHATINATOR_API_URL ?? "http://localhost:5055",
      "/mcp": process.env.AI_CHATINATOR_API_URL ?? "http://localhost:5055"
    }
  },
  build: {
    outDir: path.resolve(directory, "../src/AiChatinator.Api/wwwroot/client"),
    emptyOutDir: true
  }
}));
