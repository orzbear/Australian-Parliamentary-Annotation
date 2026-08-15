import { copyFile, mkdir } from "node:fs/promises";

await mkdir("src/hansard_annotator/web/static/dist", { recursive: true });
await copyFile(
  "node_modules/htmx.org/dist/htmx.min.js",
  "src/hansard_annotator/web/static/dist/htmx-2.0.6.min.js"
);
