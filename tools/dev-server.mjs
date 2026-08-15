import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = path.join(
  projectRoot,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const envFile = path.join(projectRoot, ".env");

if (!existsSync(python)) {
  console.error("Missing .venv Python. Create/install the project virtual environment first.");
  process.exit(1);
}

if (!existsSync(envFile)) {
  console.error("Missing .env. Copy .env.example to .env and configure it first.");
  process.exit(1);
}

const child = spawn(
  python,
  [
    "-m",
    "uvicorn",
    "hansard_annotator.web.app:create_app",
    "--factory",
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
    "--reload",
    "--env-file",
    ".env",
  ],
  { cwd: projectRoot, stdio: "inherit" },
);

child.on("error", (error) => {
  console.error(`Unable to start the development server: ${error.message}`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 1);
  }
});
