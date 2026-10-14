/** Runs the mock gateway and the Vite dev server together. */
import { spawn } from 'node:child_process';

const children = [
  spawn('npx', ['tsx', 'mock/server.ts'], { stdio: 'inherit', shell: false }),
  spawn('npx', ['vite'], { stdio: 'inherit', shell: false }),
];

const stop = () => {
  for (const child of children) child.kill('SIGTERM');
};

process.on('SIGINT', stop);
process.on('SIGTERM', stop);

for (const child of children) {
  child.on('exit', (code) => {
    stop();
    process.exit(code ?? 0);
  });
}
