import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    dedupe: ["react", "react-dom", "react/jsx-runtime"],
  },
  build: {
    outDir: path.resolve(__dirname, "../static"),
    emptyOutDir: true,
  },
});
